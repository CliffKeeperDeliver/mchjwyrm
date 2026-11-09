"""DesignWare I2C, BES flash controller + SPI-NAND, and MEMSC semaphores."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from system_control import PeripheralError
from i2c_control import I2cController, I2cDevice, TX_ABRT, STOP_DET, RX_FULL
from nand_control import FlashController, SpiNand, PAGE, SPARE, PAGES_PER_BLOCK
from register_file import RegisterFile


class Echo(I2cDevice):
    """Register-pointer device: first written byte selects, reads return it +1."""
    def __init__(self):
        self.pointer = None

    def start(self, read):
        if not read:
            self.pointer = None
        return True

    def write(self, byte):
        if self.pointer is None:
            self.pointer = byte
        return True

    def read(self):
        self.pointer = (self.pointer + 1) & 0xff
        return self.pointer


class I2cTests(unittest.TestCase):
    def setUp(self):
        self.bus = I2cController('I2C0', 0x40005000, 14, {0x38: Echo()})
        self.b = self.bus.base
        self.bus.write(self.b + 0x6c, 4, 1)          # enable
        self.bus.write(self.b + 0x30, 4, TX_ABRT | STOP_DET)

    def target(self, addr):
        self.bus.write(self.b + 0x04, 4, addr)

    def test_absent_device_aborts_with_addr_noack(self):
        self.target(0x15)
        self.bus.write(self.b + 0x10, 4, 0x200 | 0x10)
        self.assertTrue(self.bus.read(self.b + 0x34, 4) & TX_ABRT)
        self.assertEqual(self.bus.read(self.b + 0x80, 4), 1)  # ABRT_7B_ADDR_NOACK
        self.assertTrue(self.bus.irq_asserted())
        self.bus.write(self.b + 0x10, 4, 0x200 | 0x11)      # ignored until cleared
        self.assertEqual(self.bus.transactions, 1)
        self.bus.read(self.b + 0x54, 4)                      # CLR_TX_ABRT
        self.bus.read(self.b + 0x60, 4)                      # CLR_STOP_DET
        self.assertEqual(self.bus.read(self.b + 0x80, 4), 0)
        self.assertFalse(self.bus.irq_asserted())
        self.assertEqual(self.bus.log[-1]['result'], 'nack')

    def test_write_then_restart_read(self):
        self.target(0x38)
        self.bus.write(self.b + 0x10, 4, 0x41)
        self.bus.write(self.b + 0x10, 4, 0x100 | 0x400)      # RESTART read
        self.bus.write(self.b + 0x10, 4, 0x100 | 0x200)      # read + STOP
        self.assertEqual(self.bus.read(self.b + 0x78, 4), 2)
        self.assertEqual([self.bus.read(self.b + 0x10, 4) for _ in range(2)], [0x42, 0x43])
        self.assertTrue(self.bus.read(self.b + 0x34, 4) & STOP_DET)
        self.assertEqual(self.bus.log[-1], dict(addr='0x38', result='ack', data='w:41 r:4243'))
        state = self.bus.snapshot()
        self.bus.write(self.b + 0x10, 4, 0x100 | 0x200)
        self.bus.restore(state)
        self.assertEqual(self.bus.transactions, 1)

    def test_rx_full_level_and_disabled_writes(self):
        self.target(0x38)
        self.bus.write(self.b + 0x10, 4, 0x00)
        self.bus.write(self.b + 0x10, 4, 0x100 | 0x400 | 0x200)
        self.assertTrue(self.bus.read(self.b + 0x34, 4) & RX_FULL)  # RX_TL 0, 1 byte
        self.bus.write(self.b + 0x6c, 4, 0)
        with self.assertRaises(PeripheralError):
            self.bus.write(self.b + 0x10, 4, 0)


class NandTests(unittest.TestCase):
    def setUp(self):
        self.c = FlashController('NAND', 0x40500000, SpiNand())

    def command(self, cmd, tx=b'', rx=0):
        b = self.c.base
        for byte in tx:
            self.c.write(b + 0x08, 1, byte)
        self.c.write(b + 0x04, 4, (len(tx) << 12) | 1)
        self.c.write(b + 0x00, 4, cmd | ((0x100 | rx << 13) if rx else 0))
        status = self.c.read(b + 0x0c, 4)
        self.assertEqual(bool(rx), not status & 8)
        return bytes(self.c.read(b + 0x10, 4) for _ in range(rx))

    def test_id_and_features(self):
        self.assertEqual(self.command(0x9f, b'\0', 3), bytes.fromhex('c882c8'))
        self.command(0x1f, bytes([0xa0, 0x00]))
        self.command(0x06)
        self.assertEqual(self.command(0x0f, b'\xc0', 1), b'\x02')  # WEL, not busy

    def test_program_read_erase_page(self):
        row = (5 * PAGES_PER_BLOCK + 3).to_bytes(3, 'big')
        self.command(0x06)
        self.command(0x02, b'\x00\x10' + b'hello')
        self.command(0x10, row)
        self.command(0x13, row)
        self.assertEqual(self.command(0x03, b'\x00\x10\x00', 5), b'hello')
        self.assertEqual(self.command(0x03, b'\x00\x00\x00', 2), b'\xff\xff')
        self.assertEqual(self.c.chip.counts['program'], 1)
        self.command(0x10, row)  # without WREN: program fails
        self.assertTrue(self.command(0x0f, b'\xc0', 1)[0] & 0x08)
        state = self.c.snapshot()
        self.command(0x06)
        self.command(0xd8, row)
        self.command(0x13, row)
        self.assertEqual(self.command(0x03, b'\x00\x10\x00', 5), b'\xff' * 5)
        self.c.restore(state)
        self.assertEqual(len(self.c.chip.page(5 * PAGES_PER_BLOCK + 3)), PAGE + SPARE)
        self.assertEqual(bytes(self.c.chip.page(5 * PAGES_PER_BLOCK + 3)[0x10:0x15]), b'hello')

    def test_unknown_command_and_empty_fifo_stop(self):
        with self.assertRaises(PeripheralError):
            self.command(0x77)
        with self.assertRaises(PeripheralError):
            self.c.read(self.c.base + 0x10, 4)


class MemscTests(unittest.TestCase):
    def test_lock_acquire_release_and_status(self):
        r = RegisterFile('AON_CMU', 0x40080000, memsc=(0x24, 0x34))
        self.assertEqual(r.read(0x40080034, 4), 0xf)
        self.assertEqual(r.read(0x40080028, 4), 1)   # acquired lock 1
        self.assertEqual(r.read(0x40080028, 4), 0)   # busy
        self.assertEqual(r.read(0x40080034, 4), 0xd)
        state = r.snapshot()
        r.write(0x40080028, 4, 1)                     # release
        self.assertEqual(r.read(0x40080028, 4), 1)
        r.restore(state)
        self.assertEqual(r.read(0x40080034, 4), 0xd)


if __name__ == '__main__':
    unittest.main()
