"""Register semantics and explicit limits of the modeled peripherals."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from system_control import SystemControl, PeripheralError
from cache_control import CacheControl
from aon_control import AonControl
from iomux_control import IomuxControl
from timer_control import TimerBank
from cmu_control import CmuControl


class SystemControlTests(unittest.TestCase):
    def setUp(self):
        self.c = SystemControl()
        self.b = self.c.BASE

    def test_config_masks_and_alignment(self):
        self.c.write(self.b + 0xd08, 4, 0x2005c07f)
        self.assertEqual(self.c.read(self.b + 0xd08, 4), 0x2005c000)
        self.c.write(self.b + 0xd88, 4, 0xffffffff)
        self.assertEqual(self.c.read(self.b + 0xd88, 4), 0xf00000)
        self.c.write(self.b + 0xd14, 4, 0x410)
        self.assertEqual(self.c.read(self.b + 0xd14, 4), 0x410)

    def test_byte_halfword_and_word_priorities_are_little_endian(self):
        self.c.write(self.b + 0xd18, 4, 0x44332211)
        self.c.write(self.b + 0xd19, 1, 0xaa)
        self.assertEqual(self.c.read(self.b + 0xd18, 2), 0xaa11)
        self.assertEqual(self.c.read(self.b + 0xd18, 4), 0x4433aa11)
        self.c.write(self.b + 0x44c, 2, 0x1234)  # Last two implemented IRQs.
        self.assertEqual(self.c.read(self.b + 0x44d, 1), 0x12)

    def test_nvic_write_one_semantics_and_last_bank_mask(self):
        self.c.write(self.b + 0x100, 4, 3)
        self.c.write(self.b + 0x100, 4, 4)
        self.c.write(self.b + 0x180, 4, 2)
        self.assertEqual(self.c.read(self.b + 0x180, 4), 5)
        self.c.write(self.b + 0x200, 4, 7)
        self.c.write(self.b + 0x280, 4, 1)
        self.assertEqual(self.c.ready_irqs(), [2])
        self.c.write(self.b + 0x108, 4, 0xffffffff)
        self.c.write(self.b + 0x208, 4, 0xffffffff)
        self.assertEqual(self.c.read(self.b + 0x108, 4), 0x3fff)
        self.assertEqual(self.c.ready_irqs(), [2] + list(range(64, 78)))
        self.assertEqual(self.c.read(self.b + 4, 4), 2)

    def test_snapshot_is_independent_and_restores_irq_state(self):
        self.c.raise_irq(77)
        state = self.c.snapshot()
        self.c.write(self.b + 0x208, 4, 1)
        self.c.write(self.b + 0xd18, 1, 0xff)
        self.c.restore(state)
        self.assertEqual(self.c.read(self.b + 0x208, 4), 1 << 13)
        self.assertEqual(self.c.read(self.b + 0xd18, 1), 0)
        self.c.raise_irq(1)
        self.assertEqual(state['pending'][0], 0)

    def test_unsupported_requests_fail_without_mutation(self):
        before = self.c.snapshot()
        for offset, size, value in [(0xd24, 4, 0x1000), (0xd08, 2, 0),
                                    (0xd19, 2, 0), (0x300, 4, 1),
                                    (0x004, 4, 1), (0x010, 4, 0),
                                    (0x44e, 1, 1)]:
            with self.subTest(offset=offset), self.assertRaises(PeripheralError):
                self.c.write(self.b + offset, size, value)
        with self.assertRaises(PeripheralError):
            self.c.read(self.b + 0xd00, 4)  # CPUID not invented.
        with self.assertRaises(ValueError):
            self.c.raise_irq(78)
        self.assertEqual(self.c.snapshot(), before)


class CacheControlTests(unittest.TestCase):
    def test_configuration_and_command_events(self):
        c = CacheControl(0x07ffa000)
        c.write(c.base + 4, 4, 1)
        c.write(c.base, 4, 1)
        c.write(c.base + 0x38, 4, 1)
        c.write(c.base + 0x38, 4, 1)
        self.assertEqual(c.read(c.base, 4), 1)
        self.assertEqual(c.commands, {'invalidate_all': 1, 'clean_invalidate_all': 2})
        with self.assertRaises(PeripheralError):
            c.read(c.base + 4, 4)  # No fabricated command readback.

    def test_snapshot_and_strict_accesses(self):
        c = CacheControl(0x07ffc000)
        state = c.snapshot()
        c.write(c.base + 8, 4, 2)
        c.write(c.base + 4, 4, 1)
        c.restore(state)
        self.assertEqual(c.read(c.base + 8, 4), 0)
        self.assertEqual(c.commands['invalidate_all'], 0)
        for offset, size, value in [(0, 4, 4), (0, 1, 1), (4, 4, 2), (0x30, 4, 0)]:
            with self.subTest(offset=offset), self.assertRaises(PeripheralError):
                c.write(c.base + offset, size, value)
        self.assertEqual(c.snapshot(), state)


class AonControlTests(unittest.TestCase):
    def test_timer_source_configuration_is_restored_and_strict(self):
        c = AonControl(0xe0000)
        c.write(c.BASE + 0x1c, 4, 0x40000)
        state = c.snapshot()
        c.write(c.BASE + 0x1c, 4, 0)
        c.restore(state)
        self.assertEqual(c.read(c.BASE + 0x1c, 4), 0x40000)
        with self.assertRaises(PeripheralError):
            c.write(c.BASE + 0x1c, 4, 0x80000)
    def test_identity_and_observed_ram_bits(self):
        c = AonControl(0xe1234)
        self.assertEqual(c.read(c.BASE, 4), 0xe1234)
        self.assertEqual(c.report()['revision'], 14)
        c.write(c.BASE + 0x5c, 4, 2)
        state = c.snapshot()
        c.write(c.BASE + 0x5c, 4, 7)
        c.restore(state)
        self.assertEqual(c.read(c.BASE + 0x5c, 4), 2)

    def test_unsupported_accesses_do_not_invent_identity_or_configuration(self):
        with self.assertRaises(ValueError):
            AonControl(-1)
        c = AonControl(0xd0000)
        for offset, size, value in ((0, 4, 0), (0x5c, 4, 8), (0x5c, 1, 2), (4, 4, 0)):
            with self.subTest(offset=offset), self.assertRaises(PeripheralError):
                c.write(c.BASE + offset, size, value)
        with self.assertRaises(PeripheralError):
            c.read(c.BASE + 4, 4)
        self.assertEqual(c.read(c.BASE, 4), 0xd0000)
        self.assertEqual(c.read(c.BASE + 0x5c, 4), 0)


class IomuxControlTests(unittest.TestCase):
    def test_pin_routes_and_pull_fields_across_all_banks(self):
        c = IomuxControl()
        for number in range(80):
            with self.subTest(pin=number):
                c = IomuxControl()
                group, bit = divmod(number, 8)
                c.write(c.BASE + c.SELECTORS[group], 4, 15 << (4 * bit))
                if number < 32:
                    up, down, shift = 0x2c, 0x30, number
                elif number < 64:
                    up, down, shift = 0x94, 0x98, number - 32
                else:
                    up = down = 0x9c
                    shift = number - 64
                c.write(c.BASE + up, 4, 1 << shift)
                self.assertEqual(c.pin(number), dict(pin=f'P{group}_{bit}', selector=15,
                                                    pull_up=True, pull_down=False))
                c.write(c.BASE + down, 4, c.read(c.BASE + down, 4) |
                        (1 << (shift + (16 if number >= 64 else 0))))
                self.assertTrue(c.pin(number)['pull_down'])
                self.assertFalse(c.pin((number + 1) % 80)['pull_down'])

    def test_snapshot_and_unknown_bits_are_not_silently_accepted(self):
        c = IomuxControl()
        c.write(c.BASE + 8, 4, 0xffffffff)
        state = c.snapshot()
        c.write(c.BASE + 8, 4, 0)
        c.restore(state)
        self.assertEqual(c.read(c.BASE + 8, 4), 0xffffffff)
        for offset, size, value in ((0, 4, 0), (8, 1, 0), (0xac, 4, 1), (0xc4, 4, 1)):
            with self.subTest(offset=offset), self.assertRaises(PeripheralError):
                c.write(c.BASE + offset, size, value)
        self.assertEqual(c.snapshot(), state)


class CmuControlTests(unittest.TestCase):
    def test_timer_source_bits_preserve_each_other_and_snapshot(self):
        c = CmuControl()
        address = c.BASE + 0x5c
        c.write(address, 4, c.read(address, 4) | 0x400)
        state = c.snapshot()
        c.write(address, 4, c.read(address, 4) | 0x800)
        self.assertEqual(c.read(address, 4), 0xc00)
        c.restore(state)
        self.assertEqual(c.read(address, 4), 0x400)

    def test_unknown_clock_and_reset_requests_fail(self):
        c = CmuControl()
        for address, size, value in ((c.BASE + 0x5c, 4, 1), (c.BASE + 0x5c, 1, 0),
                                     (c.BASE + 0x34, 4, 1), (c.BASE, 4, 1)):
            with self.subTest(address=address), self.assertRaises(PeripheralError):
                c.write(address, size, value)
        self.assertEqual(c.read(c.BASE + 0x5c, 4), 0)


class TimerControlTests(unittest.TestCase):
    def test_explicit_ticks_not_reads_drive_independent_counters(self):
        t = TimerBank()
        a, b = t.BASES
        t.write(a, 4, 10)
        t.write(b, 4, 20)
        t.write(a + 8, 4, 0x82)
        t.advance(3)
        self.assertEqual(t.read(a + 4, 4), 7)
        self.assertEqual(t.read(a + 4, 4), 7)
        self.assertEqual(t.read(b + 4, 4), 20)
        t.write(a + 8, 4, 2)
        t.write(b + 8, 4, 0x82)
        t.advance(4)
        self.assertEqual(t.read(a + 4, 4), 7)
        self.assertEqual(t.read(b + 4, 4), 16)

    def test_zero_event_wrap_clear_and_snapshot(self):
        t = TimerBank()
        a = t.BASES[0]
        t.write(a, 4, 2)
        t.write(a + 8, 4, 0x82)
        t.advance(2)
        self.assertEqual(t.read(a + 4, 4), 0)
        self.assertEqual(t.read(a + 16, 4), 1)
        self.assertEqual(t.read(a + 20, 4), 0)
        state = t.snapshot()
        t.write(a + 12, 4, 0)
        t.advance()
        self.assertEqual(t.read(a + 4, 4), 0xffffffff)
        self.assertEqual(t.read(a + 16, 4), 0)
        t.restore(state)
        self.assertEqual(t.ticks, 2)
        self.assertEqual(t.read(a + 4, 4), 0)
        self.assertEqual(t.read(a + 16, 4), 1)

    def test_unsupported_modes_channels_and_status_writes_fail(self):
        t = TimerBank()
        state = t.snapshot()
        a = t.BASES[0]
        for offset, value in ((8, 0xa2), (8, 0xc2), (8, 0x86), (8, 0x83), (4, 1), (16, 0), (0x28, 0x82)):
            with self.subTest(offset=offset, value=value), self.assertRaises(PeripheralError):
                t.write(a + offset, 4, value)
        with self.assertRaises(PeripheralError):
            t.read(a + 12, 4)
        self.assertEqual(t.snapshot(), state)


if __name__ == '__main__':
    unittest.main(verbosity=2)
