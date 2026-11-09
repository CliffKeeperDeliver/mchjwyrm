"""Atomic GPIO semantics and original firmware driver execution."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gpio_control import GpioControl
from system_control import PeripheralError
from boot_machine import BootMachine, HERE, load_profile


class GpioTests(unittest.TestCase):
    def test_atomic_output_direction_and_external_input(self):
        g = GpioControl()
        base = g.BASES[1]
        g.drive(46, 1)
        self.assertEqual(g.read(base + 0x50, 4), 1 << 14)
        g.write(base + 4, 4, 1 << 14)
        self.assertEqual(g.read(base + 0x50, 4), 0)
        g.write(base, 4, (1 << 14) | 1)
        self.assertEqual(g.read(base + 0x50, 4), 1 << 14)
        g.write(base + 0xc, 4, 1 << 14)
        self.assertEqual(g.read(base, 4), 1)
        g.write(base + 0x10, 4, 1 << 14)
        self.assertEqual(g.read(base + 0x50, 4), 1 << 14)

    def test_falling_edge_mask_acknowledge_and_snapshot(self):
        g = GpioControl()
        b, bit = g.BASES[1], 1 << 14
        g.write(b + 0x1c, 4, bit)
        g.write(b + 0x38, 4, bit)
        g.drive(46, 1)
        self.assertFalse(g.irq_asserted())
        g.drive(46, 0)
        self.assertTrue(g.irq_asserted())
        saved = g.snapshot()
        g.write(b + 0x34, 4, bit)
        self.assertFalse(g.irq_asserted())
        self.assertEqual(g.read(b + 0x40, 4), 0)
        self.assertEqual(g.read(b + 0x44, 4), bit)
        g.write(b + 0x30, 4, bit)
        self.assertTrue(g.irq_asserted())
        g.write(b + 0x4c, 4, bit)
        self.assertFalse(g.irq_asserted())
        g.restore(saved)
        self.assertTrue(g.irq_asserted())

    def test_level_interrupt_requires_driven_input_and_reasserts(self):
        g = GpioControl()
        b = g.BASES[0]
        g.write(b + 0x1c, 4, 1)
        self.assertFalse(g.irq_asserted())
        g.drive(0, 0)
        self.assertTrue(g.irq_asserted())
        g.write(b + 0x4c, 4, 1)
        self.assertTrue(g.irq_asserted())
        g.drive(0, 1)
        self.assertFalse(g.irq_asserted())
        g.write(b + 0x3c, 4, 1)
        self.assertTrue(g.irq_asserted())

    def test_bounds_and_wake_configuration(self):
        g = GpioControl()
        g.write(g.BASES[2], 4, 0xffffffff)
        self.assertEqual(g.read(g.BASES[2], 4), 0xffff)
        g.write(g.WAKE_BASE + 0x88, 4, 0xffffffff)
        self.assertEqual(g.read(g.WAKE_BASE + 0x88, 4), 0xffff)
        for address, size in ((g.BASES[0] + 8, 4), (g.BASES[0], 1), (g.WAKE_BASE, 4)):
            with self.assertRaises(PeripheralError):
                g.read(address, size)
        for pin, value in ((80, 0), (-1, 0), (1, 2)):
            with self.assertRaises(ValueError):
                g.drive(pin, value)

    def test_stock_direction_output_and_input_functions(self):
        d = BootMachine(load_profile(HERE / 'profiles/n66gl_2.3.98.json'), output=None)
        def call(address, pin, direction=0):
            d.set_register('pc', address)
            d.set_register('lr', d.return_address | 1)
            d.set_register('r0', pin)
            d.set_register('r1', direction)
            r = d.advance(1000)
            self.assertEqual(r['status'], 'returned', r.get('fault'))
            return d.cpu.reg_read(__import__('boot_machine').arm.UC_ARM_REG_R0)
        for pin in (0, 31, 46, 79):
            call(0x0c5161a4, pin, 1)
            call(0x0c516210, pin)
            self.assertEqual(call(0x0c5162f4, pin), 1)
            call(0x0c516270, pin)
            self.assertEqual(call(0x0c5162f4, pin), 0)
            call(0x0c5161a4, pin, 0)
            d.gpio.drive(pin, 1)
            self.assertEqual(call(0x0c5162f4, pin), 1)


if __name__ == '__main__':
    unittest.main()
