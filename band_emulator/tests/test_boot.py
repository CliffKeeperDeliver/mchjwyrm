"""Whole-image boot, exception frames, clock delivery and complete checkpoints."""
from pathlib import Path
import hashlib
import struct
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from boot_machine import BootMachine, BootSystemControl, load_profile, arm
from boot_timer import BootTimers
from system_control import PeripheralError


def machine():
    return BootMachine(load_profile(HERE / 'profiles/n66gl_2.3.98.json'), output=None)


class BootUnitTests(unittest.TestCase):
    def test_virtual_clock_profiles_and_guest_request_bookkeeping(self):
        d = machine()
        # Relocate the first stock text/data range, then call the original
        # multi-client clock arbiter. Only its hardware leaf functions use HLE.
        d.write_memory(0x2005c1c0, bytes(d.cpu.mem_read(0x2c1019a4, 0xaa58)))
        def call(address, r0, r1=0):
            d.set_register('pc', address)
            d.set_register('r0', r0)
            d.set_register('r1', r1)
            d.set_register('lr', 0x0c100085)
            d.set_breakpoint(0x0c100084)
            return d.advance(5000)
        self.assertEqual(call(0x00260e88, 4, 4)['status'], 'breakpoint')
        self.assertEqual((d.clock_profile, d.power_profile), (4, 4))
        self.assertEqual(call(0x00260e88, 5, 6)['status'], 'breakpoint')
        self.assertEqual(d.clock_profile, 6)
        self.assertEqual(call(0x00260e88, 5, 0)['status'], 'breakpoint')
        self.assertEqual(d.clock_profile, 4)  # other client's request survives
        self.assertEqual(call(0x00260e88, 43, 4)['status'], 'breakpoint')
        self.assertEqual(d.cpu.reg_read(arm.UC_ARM_REG_R0), 1)
        self.assertEqual(d.clock_profile, 4)
        self.assertEqual(call(0x0025ecd0, 8)['status'], 'hle_contract_fault')

    def test_adc_stimulus_dispatch_and_unknown_channel(self):
        d = machine()
        d.set_adc_input(4, 720)
        d.set_register('pc', 0x0c39a098)
        d.set_register('lr', 0x0c100085)
        d.set_breakpoint(0x0c100084)
        d.set_register('r0', 4)
        self.assertEqual(d.advance()['status'], 'breakpoint')
        self.assertEqual(d.cpu.reg_read(arm.UC_ARM_REG_R0), 720)
        d.set_register('pc', 0x0c39a098)
        d.set_register('r0', 5)
        self.assertEqual(d.advance()['status'], 'hle_contract_fault')
        for channel, sample in ((-1, 0), (256, 0), (4, -1), (4, 65536)):
            with self.assertRaises(ValueError):
                d.set_adc_input(channel, sample)

    def test_factory_sector_is_explicitly_erased_readonly_and_bounded(self):
        d = machine()
        self.assertEqual(d.read_memory(0x287fe000, 16), b'\xff' * 16)
        with self.assertRaises(ValueError):
            d.write_memory(0x287fe000, b'test')
        with self.assertRaises(ValueError):
            d.read_memory(0x287fdffc, 4)
        # Guest store must fault too (host Unicorn writes ignore page permissions).
        d.write_memory(0x2005c1c0, bytes.fromhex('08607047'))  # str r0,[r1]; bx lr
        d.set_register('pc', 0x0025c1c0)
        d.set_register('r1', 0x287fe000)
        self.assertEqual(d.advance()['status'], 'memory_fault')
        self.assertEqual(d.read_memory(0x287fe000, 4), b'\xff' * 4)

    def test_firmware_assert_stops_with_source_and_line(self):
        d = machine()
        d.write_memory(0x20002000, b'test.c\0')
        d.set_register('pc', 0x0c1610c8)
        d.set_register('r0', 0x20002000)
        d.set_register('r1', 310)
        r = d.advance()
        self.assertEqual(r['status'], 'firmware_assert')
        self.assertEqual(r['fault']['source'], 'test.c')
        self.assertEqual(r['fault']['line'], 310)

    def test_profile_is_bound_to_stock_build_and_mapping(self):
        p = load_profile(HERE / 'profiles/n66gl_2.3.98.json')
        p['image_base'] += 4096
        with self.assertRaises(ValueError):
            BootMachine(p, output=None)

    def test_psram_aliases_and_bounds(self):
        d = machine()
        d.write_memory(0x38fffffc, b'ABCD')
        self.assertEqual(d.read_memory(0x3cfffffc, 4), b'ABCD')
        d.write_memory(0x3c000000, b'EFGH')
        self.assertEqual(d.read_memory(0x38000000, 4), b'EFGH')
        with self.assertRaises(ValueError):
            d.write_memory(0x38fffffc, b'OVERFLOW')
        with self.assertRaises(ValueError):
            d.read_memory(0x39000000, 4)

    def test_hle_rejects_invalid_return_and_rtc_buffer(self):
        for lr, pointer in ((0x0c101610, 0x20001000), (0x0c100085, 0x0c100000)):
            d = machine()
            d.set_register('pc', 0x00262a80 if pointer == 0x0c100000 else 0x0c101610)
            d.set_register('lr', lr)
            d.set_register('r0', pointer)
            # RAM HLE addresses are tied to relocated bytes; test contract dispatch.
            result = d.advance(50)
            self.assertEqual(result['status'], 'hle_contract_fault')

    def test_breakpoint_on_hle_then_step_resumes(self):
        d = machine()
        d.set_register('pc', 0x0c101610)
        d.set_register('lr', 0x0c100085)
        d.set_breakpoint(0x0c101610)
        self.assertEqual(d.advance()['status'], 'breakpoint')
        # Running counts the replaced call as one instruction, so a budget of
        # one ends on the return address with the service performed.
        self.assertEqual(d.advance(1)['status'], 'budget')
        self.assertEqual(d.pc, 0x0c100084)
        self.assertEqual(d.replacements[-1]['service'], 'bootmode_read')
        self.assertEqual(d.advance(1, stepping=True)['status'], 'stepped')
        self.assertEqual(d.pc, 0x0c100086)

    def test_terminal_fault_cannot_be_blindly_resumed(self):
        d = machine()
        d.status = 'memory_fault'
        with self.assertRaises(ValueError):
            d.advance()
        for budget, timeout in ((0, 1), (1, 0), (10000001, 1), (1, 30001)):
            with self.assertRaises(ValueError):
                machine().advance(budget, timeout)

    def test_exception_roundtrip_msp_and_psp_with_fp_and_alignment(self):
        for psp, fp in ((False, False), (False, True), (True, True)):
            with self.subTest(psp=psp, fp=fp):
                d = machine()
                cpu = d.cpu
                d.core.values[0xd08] = 0x20001000
                d.write_memory(0x20001000 + 44, struct.pack('<I', 0x0c4ee35d))
                spreg = arm.UC_ARM_REG_PSP if psp else arm.UC_ARM_REG_MSP
                cpu.reg_write(spreg, 0x20010004)
                cpu.reg_write(arm.UC_ARM_REG_CONTROL, (2 if psp else 0) | (4 if fp else 0))
                cpu.reg_write(arm.UC_ARM_REG_R0, 0x12345678)
                cpu.reg_write(arm.UC_ARM_REG_S7, 0x3f800000)
                cpu.reg_write(arm.UC_ARM_REG_XPSR, 0x61000000)
                d.exceptions.enter(11, 0x0c100084)
                self.assertEqual(cpu.reg_read(arm.UC_ARM_REG_IPSR), 11)
                self.assertEqual(cpu.reg_read(spreg) % 8, 0)
                cpu.reg_write(arm.UC_ARM_REG_R0, 0)
                cpu.reg_write(arm.UC_ARM_REG_S7, 0)
                d.exceptions.leave()
                self.assertEqual(cpu.reg_read(arm.UC_ARM_REG_IPSR), 0)
                self.assertEqual(cpu.reg_read(spreg), 0x20010004)
                self.assertEqual(cpu.reg_read(arm.UC_ARM_REG_R0), 0x12345678)
                self.assertEqual(d.pc, 0x0c100084)
                if fp:
                    self.assertEqual(cpu.reg_read(arm.UC_ARM_REG_S7), 0x3f800000)

    def test_invalid_exception_return_is_rejected_without_register_changes(self):
        d = machine()
        d.exceptions.active = 11
        d.cpu.reg_write(arm.UC_ARM_REG_LR, 0xfffffff1)  # handler return/nesting
        before = d.report()['registers']
        with self.assertRaises(ValueError):
            d.exceptions.leave()
        self.assertEqual(d.report()['registers'], before)

    def test_systick_and_irq_masking(self):
        d = machine()
        c = d.core
        c.write(0xe000e014, 4, 2)
        c.write(0xe000e010, 4, 7)
        c.tick(); c.tick(); c.tick()
        self.assertTrue(c.systick['pending'])
        self.assertEqual(c.read(0xe000e010, 4), 0x10007)
        self.assertEqual(c.read(0xe000e010, 4), 7)
        # Bulk ticks (chunked execution) equal the same number of single ticks.
        for clock, n in ((7, 1000), (3, 50000), (7, 1)):
            one, bulk = BootSystemControl(), BootSystemControl()
            for x in (one, bulk):
                x.write(0xe000e014, 4, 37)
                x.write(0xe000e010, 4, clock)
            for _ in range(n):
                one.tick()
            bulk.tick(n)
            self.assertEqual((one.systick, one.systick_phase), (bulk.systick, bulk.systick_phase))
        c.shpr[11] = 0xa0
        d.cpu.reg_write(arm.UC_ARM_REG_BASEPRI, 0x80)
        self.assertIsNone(d.exceptions.pending_irq())
        d.cpu.reg_write(arm.UC_ARM_REG_BASEPRI, 0)
        self.assertEqual(d.exceptions.pending_irq(), -1)
        d.cpu.reg_write(arm.UC_ARM_REG_PRIMASK, 1)
        self.assertIsNone(d.exceptions.pending_irq())

    def test_oneshot_timer_latches_and_clears_interrupt(self):
        t = BootTimers()
        t.write(t.SECONDARY, 4, 3)
        t.write(t.SECONDARY + 8, 4, 0xa3)
        t.advance(2)
        self.assertFalse(t.irq_asserted())
        t.advance()
        self.assertEqual(t.read(t.SECONDARY + 4, 4), 0)
        self.assertEqual(t.read(t.SECONDARY + 20, 4), 1)
        state = t.snapshot()
        t.advance(20)
        self.assertEqual(t.read(t.SECONDARY + 4, 4), 0)
        t.write(t.SECONDARY + 12, 4, 1)
        self.assertFalse(t.irq_asserted())
        t.restore(state)
        self.assertTrue(t.irq_asserted())
        with self.assertRaises(PeripheralError):
            t.write(t.SECONDARY + 8, 4, 0xe2)

    def test_snapshot_covers_psram_devices_and_exception_state(self):
        d = machine()
        d.write_memory(0x3c001000, b'RAM!')
        d.core.systick['pending'] = True
        d.core.systick_phase = 123
        d.exceptions.active = 11
        d.pending_exception = (15, 0x0c100084)
        d.console.extend(b'hello')
        d.timers.oneshot['value'] = 77
        d.gpio.drive(46, 1)
        d.set_adc_input(4, 720)
        d.clock_profile, d.power_profile = 4, 4
        d.save('all')
        d.write_memory(0x38001000, b'xxxx')
        d.core.systick_phase = 999
        d.exceptions.active = None
        d.console.clear()
        d.timers.oneshot['value'] = 0
        d.gpio.drive(46, 0)
        d.set_adc_input(4, 100)
        d.clock_profile, d.power_profile = 6, 6
        d.restore('all')
        self.assertEqual(d.read_memory(0x38001000, 4), b'RAM!')
        self.assertEqual(d.core.systick_phase, 123)
        self.assertTrue(d.core.systick['pending'])
        self.assertEqual(d.exceptions.active, 11)
        self.assertEqual(d.pending_exception, (15, 0x0c100084))
        self.assertEqual(d.console, b'hello')
        self.assertEqual(d.timers.oneshot['value'], 77)
        self.assertEqual(d.gpio.read(0x40089050, 4), 1 << 14)
        self.assertEqual(d.adc_samples, {4: 720})
        self.assertEqual((d.clock_profile, d.power_profile), (4, 4))


class StockBootTests(unittest.TestCase):
    def test_stock_kernel_creates_tasks_switches_context_and_replays(self):
        d = machine()
        d.set_breakpoint(0x0c1404e8)
        for _ in range(6):
            r = d.advance(2000000, 30000)
            if r['status'] != 'timeout':
                break
        self.assertEqual(r['status'], 'breakpoint', r.get('fault'))
        self.assertEqual({t['name'] for t in r['tasks']}, {'Idle_Task', 'hpwork', 'lpwork', 'AppBringUp'})
        self.assertEqual(r['console'], 'system power on....\n')
        self.assertTrue(any(e['kind'] == 'return' and e['pc'] == '0xc140fcc' for e in r['exceptions']['events']))
        self.assertIn('system_heaps_initialized', {m['stage'] for m in r['milestones']})
        # Stock copied the second SRAM text range exactly, and the PSRAM aliases share it.
        self.assertEqual(bytes(d.cpu.mem_read(0x20068700, 0x2638)),
                         bytes(d.cpu.mem_read(0x2c10c3fc, 0x2638)))
        self.assertNotEqual(int.from_bytes(d.cpu.mem_read(0x20081fcc, 4), 'little'), 0)
        d.save('bringup')
        a = d.advance(120000, 30000)
        digest = hashlib.sha256(d.psram.raw).hexdigest()
        d.restore('bringup')
        b = d.advance(120000, 30000)
        self.assertEqual(a['status'], 'budget')
        self.assertEqual(a['registers'], b['registers'])
        self.assertEqual(a['exceptions'], b['exceptions'])
        self.assertEqual(a['systick'], b['systick'])
        self.assertEqual(a['tasks'], b['tasks'])
        self.assertEqual(hashlib.sha256(d.psram.raw).hexdigest(), digest)
        # The actual guest initializes GPIO IRQs and the ADC device nodes.
        d.set_breakpoint(0x0c51ac82)
        for _ in range(4):
            r = d.advance(2000000, 30000)
            if r['status'] not in ('budget', 'timeout'):
                break
        self.assertEqual(r['status'], 'breakpoint', r.get('fault'))
        self.assertEqual(int.from_bytes(d.read_memory(0x200af50c, 4), 'little'), 4)
        self.assertEqual(d.gpio.read(0x4008901c, 4), 1 << 14)
        d.save('board')
        d.gpio.drive(46, 1)
        d.gpio.drive(46, 0)
        d.set_breakpoint(0x0c4ee35c)
        irq = d.advance(5000, 30000)
        self.assertEqual(irq['status'], 'breakpoint', irq.get('fault'))
        self.assertEqual(d.exceptions.active, 60)  # GPIO IRQ44 + Cortex exception offset
        self.assertEqual(irq['exceptions']['events'][-1]['exception'], 60)
        d.restore('board')
        d.breakpoints.discard(0x0c4ee35c)
        # Continue to the real next unsupported board dependency; no fake success.
        for _ in range(12):
            r = d.advance(2000000, 30000)
            if r['status'] not in ('budget', 'timeout'):
                break
        self.assertEqual(r['status'], 'memory_fault', r.get('fault'))
        self.assertEqual(r['fault']['address'], '0x402001a8')  # LCD controller (display init)
        stages = {m['stage'] for m in r['milestones']}
        self.assertTrue({'board_storage_init_returned', 'board_init_returned'} <= stages)
        self.assertTrue({'bes_main', 'audio_flinger'} <= {t['name'] for t in r['tasks']})
        # Stock hal_analogif_open read PMU CHIP_ID over ISPI and accepted 0x153.
        self.assertGreater(r['ispi']['frames'], 400)
        self.assertNotIn('cs3 page0 0x00', r['ispi']['assumed_zero'])
        self.assertIn('bt_controller_absent', {x['service'] for x in r['hle_replacements']})
        # The stock NAND driver identified the chip and configured it.
        self.assertIn('9f tx=00 rx=3', r['nand']['recent'] + [x for x in d.nand.chip.log])
        self.assertGreater(r['nand']['commands'], 10)
        self.assertEqual(r['register_blocks']['AON_CMU']['base'], '0x40080000')
        self.assertTrue(any(e['exception'] == 62 for e in r['exceptions']['events']))
        with self.assertRaises(ValueError):
            d.advance()

    def test_result_does_not_depend_on_run_slicing(self):
        # IRQs pending while masked are taken right after the unmask
        # instruction; previously the boot outcome depended on slice sizes.
        results = []
        for budget, timeout_ms in ((5000000, 30000), (777777, 30000), (5000000, 50)):
            d = machine()
            for _ in range(400):
                r = d.advance(budget, timeout_ms)
                if r['status'] not in ('budget', 'timeout'):
                    break
            results.append((r['status'], r['fault']['address'], r['instructions'],
                            r['exceptions']['total_events'], r['registers']['pc']))
        self.assertEqual(results[0][:2], ('memory_fault', '0x402001a8'))
        self.assertEqual(results.count(results[0]), 3, results)


if __name__ == '__main__':
    unittest.main()
