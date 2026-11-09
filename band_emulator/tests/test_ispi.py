"""ISPI (PL022 + BES analog interface) and logged register-file semantics."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from system_control import PeripheralError
from ispi_control import IspiControl
from register_file import RegisterFile


def read_cmd(reg):
    return ((1 << 24) | reg << 16) << 2


def write_cmd(reg, value):
    return (reg << 16 | value) << 2


class IspiTests(unittest.TestCase):
    def setUp(self):
        self.s = IspiControl()
        b = self.b = self.s.BASE
        self.s.write(b + 0x00, 4, 26 << 16)          # 27-bit frames, as stock configures
        self.s.write(b + 0x04, 4, (3 << 7) | 2)      # PMU chip select, SSE

    def transfer(self, frame):
        self.s.write(self.b + 8, 4, frame)
        self.assertTrue(self.s.read(self.b + 0x0c, 4) & 4)  # RNE
        return self.s.read(self.b + 8, 4)

    def test_pmu_chip_id_matches_stock_check(self):
        value = self.transfer(read_cmd(0)) >> 2
        self.assertEqual(value >> 4, 0x153)   # hal_analogif_open compares this field

    def test_paged_registers_and_unknown_reads_are_logged(self):
        self.transfer(write_cmd(0x47, 0x4000))
        self.transfer(write_cmd(0, 0xa010))       # page 1
        self.assertEqual(self.transfer(read_cmd(0x47)), 0)
        self.transfer(write_cmd(0x47, 0x1234))
        self.transfer(write_cmd(0, 0xa000))       # back to page 0
        self.assertEqual(self.transfer(read_cmd(0x47)) >> 2, 0x4000)
        self.assertEqual(self.s.report()['assumed_zero'], ['cs3 page1 0x47'])
        self.transfer(write_cmd(0, 0xcafe))       # not a page select: plain data
        self.assertEqual(self.transfer(read_cmd(0)) >> 2, 0xcafe)

    def test_rx_overflow_drops_and_flags_like_pl022(self):
        for _ in range(self.s.FIFO + 2):
            self.s.write(self.b + 8, 4, write_cmd(0x10, 1))
        self.assertEqual(len(self.s.rx), self.s.FIFO)
        self.assertEqual(self.s.read(self.b + 0x18, 4) & 1, 1)
        self.s.write(self.b + 0x20, 4, 1)
        self.assertEqual(self.s.read(self.b + 0x18, 4) & 1, 0)

    def test_snapshot_and_rejected_accesses(self):
        state = self.s.snapshot()
        self.transfer(write_cmd(0x20, 7))
        self.s.restore(state)
        self.assertEqual(self.s.analog, {})
        with self.assertRaises(PeripheralError):
            self.s.read(self.b + 8, 4)                     # empty FIFO
        with self.assertRaises(PeripheralError):
            self.s.write(self.b + 8, 4, read_cmd(0) | 1)   # padding bits set
        self.s.write(self.b + 4, 4, 0)
        with self.assertRaises(PeripheralError):
            self.s.write(self.b + 8, 4, read_cmd(0))       # SSE disabled


class RegisterFileTests(unittest.TestCase):
    def test_storage_reset_read_only_and_assumed_zero(self):
        r = RegisterFile('AON', 0x40080000, reset={0: 0xe0000}, read_only={0})
        self.assertEqual(r.read(0x40080000, 4), 0xe0000)
        with self.assertRaises(PeripheralError):
            r.write(0x40080000, 4, 1)
        self.assertEqual(r.read(0x4008004c, 4), 0)
        r.write(0x4008004c, 4, 0x800000)
        self.assertEqual(r.read(0x4008004c, 4), 0x800000)
        state = r.snapshot()
        r.write(0x4008004c, 4, 0)
        r.restore(state)
        self.assertEqual(r.report()['registers'], {'0x4008004c': '0x800000'})
        self.assertEqual(r.report()['assumed_zero'], ['0x4008004c'])
        with self.assertRaises(PeripheralError):
            r.read(0x40080002, 4)


if __name__ == '__main__':
    unittest.main()
