"""Resumption, fault retry, CPU/RAM/device snapshots and real startup tests."""
import hashlib
import contextlib
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from debugger import Debugger, load_profile, command


class DebuggerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def fixture(self, code):
        p = Path(self.temp.name) / 'fixture.bin'
        p.write_bytes(bytes.fromhex(code))
        profile = dict(name='synthetic debugger test', image=str(p),
                       sha256=hashlib.sha256(p.read_bytes()).hexdigest(),
                       image_base=0x0c100000, image_read_aliases=[0x2c100000],
                       ram_base=0x20000000, ram_size=0x10000, stack_pointer=0x2000fff0,
                       ram_execution_alias=0x00200000,
                       startup_offset=0, return_address=0x10000000)
        return Debugger(profile)

    def test_step_and_resume_preserve_registers(self):
        d = self.fixture('0120013001307047')  # movs r0,1; adds r0,1; adds r0,1; bx lr
        self.assertEqual(d.step()['status'], 'stepped')
        self.assertEqual(d.pc, d.base + 2)
        self.assertEqual(d.report()['registers']['r0'], '0x1')
        d.step()
        self.assertEqual(d.report()['registers']['r0'], '0x2')
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r0'], '0x3')

    def test_run_command_has_bounded_optional_timeout(self):
        d = self.fixture('7047')
        with self.assertRaises(ValueError):
            command(d, 'run 100 30001')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(command(d, 'run 100 20000'))
        self.assertEqual(d.status, 'returned')

    def test_timer_instruction_clock_is_identical_across_steps_and_snapshots(self):
        d = self.fixture('016802687047')  # ldr r1,[r0]; ldr r2,[r0]; bx lr
        d.enable_timers()
        d.timers.write(0x40084000, 4, 10)
        d.timers.write(0x40084008, 4, 0x82)
        d.set_register('r0', 0x40084004)
        d.save('before')
        self.assertEqual(d.advance()['status'], 'returned')
        expected = d.timers.snapshot()
        self.assertEqual(d.report()['registers']['r1'], '0x9')
        self.assertEqual(d.report()['registers']['r2'], '0x8')
        self.assertEqual(expected['ticks'], 3)
        d.restore('before')
        d.step()
        d.step()
        d.advance()
        self.assertEqual(d.timers.snapshot(), expected)

    def test_guest_timer_polling_loop_finishes_on_virtual_time(self):
        d = self.fixture('01680529fcd87047')  # ldr r1,[r0]; cmp r1,5; bhi loop; bx lr
        d.enable_timers()
        d.timers.write(0x40084000, 4, 10)
        d.timers.write(0x40084008, 4, 0x82)
        d.set_register('r0', 0x40084004)
        self.assertEqual(d.advance(100)['status'], 'returned')
        self.assertEqual(d.report()['registers']['r1'], '0x3')
        self.assertEqual(d.timers.ticks, 10)

    def test_cmu_snapshot_and_partial_guest_write_rollback(self):
        d = self.fixture('06c07047')
        d.save('bare')
        d.enable_cmu()
        d.cmu.write(0x4000005c, 4, 0x400)
        d.save('configured')
        d.set_register('r0', 0x4000005c)
        d.set_register('r1', 0x800)
        d.set_register('r2', 1)  # SYS_CLK_ENABLE is unsupported after valid PERIPH_CLK write.
        self.assertEqual(d.advance()['status'], 'peripheral_fault')
        self.assertEqual(d.cmu.read(0x4000005c, 4), 0x400)
        with self.assertRaises(ValueError):
            d.model_register(0x4000005c, 0)
        d.restore('bare')
        self.assertIsNone(d.cmu)
        d.restore('configured')
        self.assertEqual(d.cmu.read(0x4000005c, 4), 0x400)

    def test_faulting_instruction_rolls_back_timer_ticks_and_iomux(self):
        d = self.fixture('06c07047')  # stmia r0!,{r1,r2}; second word unsupported.
        d.enable_iomux()
        d.enable_timers()
        d.timers.write(0x40084008, 4, 0x82)
        d.set_register('r0', 0x40086010)
        d.set_register('r1', 0x1234)
        d.set_register('r2', 0)
        before = d.timers.snapshot()
        self.assertEqual(d.advance()['status'], 'peripheral_fault')
        self.assertEqual(d.iomux.read(0x40086010, 4), 0)
        self.assertEqual(d.timers.snapshot(), before)
        self.assertEqual(d.pc, d.base)

    def test_iomux_timer_snapshot_remaps_guest_access_and_rejects_overlap(self):
        d = self.fixture('01687047')
        d.save('bare')
        d.enable_iomux()
        d.enable_timers()
        d.iomux.write(0x40086008, 4, 0xffffffff)
        d.timers.write(0x40084000, 4, 42)
        d.save('devices')
        for address in (0x40086008, 0x40084000):
            with self.assertRaises(ValueError):
                d.model_register(address, 0)
        d.restore('bare')
        self.assertIsNone(d.iomux)
        self.assertIsNone(d.timers)
        self.assertEqual(d.mmio_pages, set())
        d.restore('devices')
        d.set_register('r0', 0x40084004)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r1'], '0x2a')
        d.restore('devices')
        d.set_register('r0', 0x40086008)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r1'], '0xffffffff')
        d.restore('bare')
        d.model_register(0x40003000, 0)
        with self.assertRaises(ValueError):
            d.enable_timers()
        self.assertNotIn(0x40084000, d.mmio_pages)
        self.assertIsNone(d.timers)

    def test_breakpoint_stops_before_instruction_and_resumes_once(self):
        d = self.fixture('0130fde7')  # adds r0,1; b back
        d.set_breakpoint(d.base)
        self.assertEqual(d.advance()['status'], 'breakpoint')
        self.assertEqual(d.report()['registers']['r0'], '0x0')
        self.assertEqual(d.advance()['status'], 'breakpoint')
        self.assertEqual(d.report()['registers']['r0'], '0x1')

    def test_snapshot_restores_ram_registers_and_mmio_mapping(self):
        d = self.fixture('012001607047')  # movs r0,1; str r1,[r0] -- not run
        d.write_ram(0x20000000, b'ABCD')
        d.model_register(0x40000000, 7)
        d.set_register('r0', 123)
        d.save('before')
        d.write_ram(0x20000000, b'ZZZZ')
        d.model_register(0x40000000, 9)
        d.model_register(0x40001000, 11)
        d.set_register('r0', 456)
        d.restore('before')
        self.assertEqual(d.read_ram(0x20000000, 4), b'ABCD')
        self.assertEqual(d.report()['registers']['r0'], '0x7b')
        self.assertEqual(d.mmio, {0x40000000: 7})
        self.assertNotIn(0x40001000, d.mmio_pages)

    def test_fault_retry_reads_new_device_value(self):
        d = self.fixture('01687047')  # ldr r1,[r0]; bx lr
        d.set_register('r0', 0x40000000)
        self.assertEqual(d.advance()['status'], 'memory_fault')
        self.assertEqual(d.pc, d.base)
        d.model_register(0x40000000, 0x12345678)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r1'], '0x12345678')

    def test_unmodeled_same_page_read_is_rolled_back_and_retried(self):
        d = self.fixture('01687047')
        d.set_register('r0', 0x40000004)
        d.set_register('r1', 0x99)
        d.model_register(0x40000000, 0)
        self.assertEqual(d.advance()['status'], 'unmodeled_register')
        self.assertEqual(d.pc, d.base)
        self.assertEqual(d.report()['registers']['r1'], '0x99')
        d.model_register(0x40000004, 0x42)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r1'], '0x42')

    def test_partial_multiword_store_rollback(self):
        d = self.fixture('06c07047')  # stmia r0!, {r1,r2}; bx lr
        address = 0x2000fffc  # First word valid, second lies outside RAM.
        d.write_ram(address, bytes.fromhex('aabbccdd'))
        d.set_register('r0', address)
        d.set_register('r1', 1)
        d.set_register('r2', 2)
        self.assertEqual(d.advance()['status'], 'memory_fault')
        self.assertEqual(d.read_ram(address, 4), bytes.fromhex('aabbccdd'))
        self.assertEqual(d.report()['registers']['r0'], hex(address))
        self.assertEqual(d.pc, d.base)

    def test_read_alias_is_identical_and_immutable(self):
        d = self.fixture('01607047')
        self.assertEqual(d.read_memory(0x2c100000, 4), d.read_memory(d.base, 4))
        d.set_register('r0', 0x2c100000)
        d.set_register('r1', 0)
        self.assertEqual(d.advance()['status'], 'memory_fault')
        self.assertEqual(d.fault['access'], 'protected_write')

    def test_invalid_controls_are_rejected(self):
        d = self.fixture('7047')
        with self.assertRaises(ValueError):
            d.set_register('pc', 0x20000000)
        with self.assertRaises(ValueError):
            d.model_register(0x2c100000, 1)
        with self.assertRaises(ValueError):
            d.read_memory(0x40000000, 4)
        with self.assertRaises(ValueError):
            d.restore('missing')
        with self.assertRaises(ValueError):
            d.advance(0)

    def test_core_guest_word_read_and_byte_write(self):
        d = self.fixture('016802707047')  # ldr r1,[r0]; strb r2,[r0]; bx lr
        d.enable_core()
        d.set_register('r0', 0xe000ed18)
        d.set_register('r2', 0xa0)
        d.core.write(0xe000ed18, 4, 0x44332211)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r1'], '0x44332211')
        self.assertEqual(d.core.read(0xe000ed18, 4), 0x443322a0)

    def test_core_unknown_read_rolls_back_cpu_and_can_retry(self):
        d = self.fixture('01687047')
        d.enable_core()
        d.set_register('r0', 0xe000ed00)
        d.set_register('r1', 0x99)
        self.assertEqual(d.advance()['status'], 'peripheral_fault')
        self.assertEqual(d.report()['registers']['r1'], '0x99')
        self.assertEqual(d.pc, d.base)
        d.set_register('r0', 0xe000ed08)
        d.core.write(0xe000ed08, 4, 0x20008000)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r1'], '0x20008000')

    def test_peripheral_partial_store_is_transactional(self):
        # First write changes VTOR, second touches unsupported AIRCR.
        d = self.fixture('06c07047')  # stmia r0!, {r1,r2}; bx lr
        d.enable_core()
        d.set_register('r0', 0xe000ed08)
        d.set_register('r1', 0x20008000)
        self.assertEqual(d.advance()['status'], 'peripheral_fault')
        self.assertEqual(d.core.read(0xe000ed08, 4), 0)
        self.assertEqual(d.pc, d.base)

    def test_cache_partial_store_rolls_back_configuration(self):
        d = self.fixture('06c07047')
        d.enable_caches()
        d.set_register('r0', 0x07ffa000)
        d.set_register('r1', 1)
        d.set_register('r2', 2)  # Unsupported invalidate command after valid enable.
        self.assertEqual(d.advance()['status'], 'peripheral_fault')
        self.assertEqual(d.caches[0x07ffa000].read(0x07ffa000, 4), 0)
        self.assertEqual(d.caches[0x07ffa000].commands['invalidate_all'], 0)
        self.assertEqual(d.pc, d.base)

    def test_ram_guest_copy_execute_patch_and_restore(self):
        d = self.fixture('01607047')  # str r1,[r0]; bx lr
        d.save('bare')
        d.enable_ram_execution()
        d.set_register('r0', 0x20000000)
        d.set_register('r1', 0x47702005)  # movs r0,5; bx lr
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.read_memory(0x200000, 4), bytes.fromhex('05207047'))
        d.set_register('pc', 0x200000)
        d.save('code')
        self.assertIn('movs r0, #5', d.disassemble(0x200000, 1)[0])
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r0'], '0x5')
        self.assertEqual(d.trace[-1]['region'], 'ram_execution')
        d.write_ram(0x20000000, bytes.fromhex('09207047'))
        d.set_register('pc', 0x200000)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r0'], '0x9')
        d.restore('code')
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r0'], '0x5')
        d.restore('bare')
        self.assertIsNone(d.ram_execution)
        with self.assertRaises(ValueError):
            d.read_memory(0x200000, 4)
        d.restore('code')
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r0'], '0x5')

    def test_ram_guest_patch_invalidates_previously_executed_translation(self):
        d = self.fixture('7047')
        d.enable_ram_execution()
        d.write_ram(0x20000100, bytes.fromhex('03207047'))
        d.set_register('pc', 0x200100)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r0'], '0x3')
        # Executing RAM code replaces another translated RAM routine, then enters it.
        d.write_ram(0x20000000, bytes.fromhex('11601847'))  # str r1,[r2]; bx r3
        d.set_register('r1', 0x47702009)
        d.set_register('r2', 0x20000100)
        d.set_register('r3', 0x200101)
        d.set_register('pc', 0x200000)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r0'], '0x9')

    def test_ram_breakpoint_step_and_protection(self):
        d = self.fixture('01607047')
        d.enable_ram_execution()
        d.write_ram(0x20000000, bytes.fromhex('01307047'))
        d.set_register('pc', 0x200000)
        d.set_breakpoint(0x200000)
        self.assertEqual(d.advance()['status'], 'breakpoint')
        self.assertEqual(d.step()['status'], 'stepped')
        self.assertEqual(d.report()['registers']['r0'], '0x1')
        d.set_register('pc', d.base)
        d.set_register('r0', 0x200000)  # Attempt to write read/execute alias.
        self.assertEqual(d.advance()['status'], 'memory_fault')
        self.assertEqual(d.fault['access'], 'protected_write')
        self.assertEqual(d.read_memory(0x200000, 4), bytes.fromhex('01307047'))
        with self.assertRaises(ValueError):
            d.set_register('pc', 0x20000000)  # Data view stays non-executable.

    def test_ram_alias_reflects_fault_rollback(self):
        d = self.fixture('06c07047')
        d.enable_ram_execution()
        d.write_ram(0x2000fffc, b'ABCD')
        d.set_register('r0', 0x2000fffc)
        d.set_register('r1', 1)
        self.assertEqual(d.advance()['status'], 'memory_fault')
        self.assertEqual(d.read_memory(0x20fffc, 4), b'ABCD')

    def test_ram_alias_overlap_fails_preserving_ram(self):
        d = self.fixture('7047')
        d.write_ram(0x20000000, b'ABCD')
        d.model_register(0x200000, 0)
        with self.assertRaises(ValueError):
            d.enable_ram_execution()
        self.assertIsNone(d.ram_execution)
        self.assertEqual(d.read_ram(0x20000000, 4), b'ABCD')

    def test_aon_snapshot_readonly_fault_and_overlap(self):
        d = self.fixture('01607047')
        d.save('bare')
        d.enable_aon(0xe1234)
        d.aon.write(0x4008005c, 4, 2)
        d.save('aon')
        d.set_register('r0', 0x40080000)
        d.set_register('r1', 0)
        self.assertEqual(d.advance()['status'], 'peripheral_fault')
        self.assertEqual(d.aon.read(0x40080000, 4), 0xe1234)
        with self.assertRaises(ValueError):
            d.model_register(0x4008005c, 0)
        d.restore('bare')
        self.assertIsNone(d.aon)
        d.restore('aon')
        self.assertEqual(d.aon.read(0x4008005c, 4), 2)
        self.assertEqual(d.aon.read(0x40080000, 4), 0xe1234)

    def test_peripheral_snapshots_restore_mapping_and_state(self):
        d = self.fixture('01687047')
        d.save('bare')
        d.enable_core()
        d.enable_caches()
        d.core.write(0xe000ed08, 4, 0x20008000)
        d.caches[0x07ffa000].write(0x07ffa004, 4, 1)
        d.save('devices')
        d.restore('bare')
        self.assertIsNone(d.core)
        self.assertEqual(d.caches, {})
        self.assertEqual(d.mmio_pages, set())
        d.restore('devices')
        self.assertEqual(d.core.read(0xe000ed08, 4), 0x20008000)
        self.assertEqual(d.caches[0x07ffa000].commands['invalidate_all'], 1)
        d.set_register('r0', 0xe000ed08)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r1'], '0x20008000')
        d.restore('bare')
        d.restore('devices')
        d.set_register('r0', 0x07ffa000)
        self.assertEqual(d.advance()['status'], 'returned')

    def test_pending_enabled_irq_stops_before_next_instruction(self):
        d = self.fixture('01307047')  # adds r0,1; bx lr
        d.enable_core()
        d.core.write(0xe000e100, 4, 8)
        d.core.raise_irq(3)
        self.assertEqual(d.step()['status'], 'interrupt_delivery_required')
        self.assertEqual(d.pc, d.base)
        self.assertEqual(d.instruction_count, 0)
        d.core.write(0xe000e280, 4, 8)
        self.assertEqual(d.advance()['status'], 'returned')
        self.assertEqual(d.report()['registers']['r0'], '0x1')

    def test_model_overlap_rejected_without_partial_cache_mapping(self):
        d = self.fixture('7047')
        d.enable_core()
        with self.assertRaises(ValueError):
            d.model_register(0xe000ed08, 1)
        d.model_register(0x07ffc000, 0)
        with self.assertRaises(ValueError):
            d.enable_caches()
        self.assertEqual(d.caches, {})
        self.assertNotIn(0x07ffa000, d.mmio_pages)


class RealStartupTests(unittest.TestCase):
    def test_stock_revision_branches_copy_and_execute_ram(self):
        for revision, config, count in ((13, 0xa, 83984), (14, 2, 83985)):
            with self.subTest(revision=revision):
                d = Debugger(load_profile(HERE / 'profiles/n66gl_2.3.98.json'),
                             system_control=True, cache_control=True,
                             aon_chip_id=revision << 16, ram_execution=True)
                r = d.advance(100000, 20000)
                self.assertEqual(r['status'], 'memory_fault')
                self.assertEqual(r['fault']['address'], '0x40086008')
                self.assertEqual(d.pc, 0x260092)
                self.assertEqual(d.instruction_count, count)
                self.assertEqual(d.caches[0x07ffc000].values[8], config)
                self.assertEqual(d.caches[0x07ffc000].values[0], 1)
                # Startup relocation: flash [0x19a4,0xc3fc) -> RAM 0x2005c1c0.
                size = 0xc3fc - 0x19a4
                self.assertEqual(d.read_ram(0x2005c1c0, size), d.image[0x19a4:0xc3fc])
                self.assertEqual(d.read_memory(0x260088, 4), d.read_ram(0x20060088, 4))
                self.assertTrue(any(item['region'] == 'ram_execution' for item in d.trace))
                # Continue the same real boot, retaining the previous fault in the attempt count.
                d.enable_iomux()
                d.enable_timers()
                r = d.advance(10000, 20000)
                self.assertEqual(r['status'], 'memory_fault')
                self.assertEqual(r['fault']['address'], '0x4000005c')
                self.assertEqual(d.pc, 0x264f5a)
                self.assertEqual(d.instruction_count, 85894 + (revision == 14))
                for pin in range(8, 16):
                    self.assertEqual(d.iomux.pin(pin)['selector'], 15)
                    self.assertTrue(d.iomux.pin(pin)['pull_down'])
                    self.assertFalse(d.iomux.pin(pin)['pull_up'])
                for pin in range(32, 38):
                    self.assertEqual(d.iomux.pin(pin)['selector'], 7)
                self.assertEqual(d.iomux.read(0x400860ac, 4), 0x3c00)
                self.assertEqual(d.timers.read(0x40084008, 4), 0x82)
                self.assertEqual(d.timers.read(0x40084004, 4), 0xfffffff5)
                self.assertEqual(d.timers.read(0x40003008, 4), 0)
                d.enable_cmu()
                r = d.advance(10000, 20000)
                self.assertEqual(r['status'], 'peripheral_fault')
                self.assertEqual(r['fault']['address'], '0x40080038')
                self.assertEqual(d.pc, 0xc1015fc)
                self.assertEqual(d.instruction_count, 85927 + (revision == 14))
                self.assertEqual(d.cmu.read(0x4000005c, 4), 0xc00)
                self.assertEqual(d.timers.read(0x40084004, 4), 0xffffffd5)
                self.assertEqual(d.timers.read(0x40003004, 4), 0xffffffe9)
                self.assertEqual(d.timers.read(0x40003008, 4), 0x82)

    def test_stock_cache_enable_cold_and_already_enabled_paths(self):
        d = Debugger(load_profile(HERE / 'profiles/n66gl_2.3.98.json'), cache_control=True)
        for cache_id, base in enumerate((0x07ffa000, 0x07ffc000)):
            for attempt in range(2):
                d.set_register('pc', 0x0c10167c)
                d.set_register('r0', cache_id)
                self.assertEqual(d.advance()['status'], 'returned')
                self.assertEqual(d.caches[base].read(base, 4), 1)
                self.assertEqual(d.caches[base].commands['invalidate_all'], 1)
                self.assertEqual(d.caches[base].commands['clean_invalidate_all'], attempt)

    def test_stock_system_configuration_reaches_cache_controller(self):
        d = Debugger(load_profile(HERE / 'profiles/n66gl_2.3.98.json'),
                     {0x4008005c: 0}, system_control=True)
        r = d.advance()
        self.assertEqual(r['status'], 'memory_fault')
        self.assertEqual(r['fault']['address'], '0x7ffa000')
        self.assertEqual(d.instruction_count, 722)
        self.assertEqual(d.core.read(0xe000ed08, 4), 0x2005c000)
        self.assertEqual(d.core.read(0xe000ed14, 4), 0x410)
        self.assertEqual(d.core.read(0xe000ed24, 4), 0x70000)
        self.assertEqual(d.core.read(0xe000ed88, 4), 0xf00000)

    def test_stock_cache_setup_reaches_unknown_chip_register(self):
        d = Debugger(load_profile(HERE / 'profiles/n66gl_2.3.98.json'),
                     {0x4008005c: 0}, system_control=True, cache_control=True)
        r = d.advance()
        self.assertEqual(r['status'], 'unmodeled_register')
        self.assertEqual(r['fault']['address'], '0x40080000')
        self.assertEqual(d.pc, 0x0c10154a)
        self.assertEqual(d.instruction_count, 743)
        self.assertEqual(d.caches[0x07ffa000].read(0x07ffa000, 4), 1)
        self.assertEqual(d.caches[0x07ffa000].commands['invalidate_all'], 1)

    def test_resume_stock_startup_and_restore(self):
        d = Debugger(load_profile(HERE / 'profiles/n66gl_2.3.98.json'))
        d.save('power_on')
        d.step()
        self.assertEqual(d.pc, 0x0c100012)
        self.assertEqual(d.report()['registers']['r0'], '0xc100015')
        self.assertEqual(d.advance()['status'], 'memory_fault')
        self.assertEqual(d.fault['address'], '0x4008005c')
        d.model_register(0x4008005c, 0)
        self.assertEqual(d.advance()['status'], 'memory_fault')
        self.assertEqual(d.fault['address'], '0xe000ed08')
        self.assertEqual(d.pc, 0x0c1000b2)
        # The stock vector initializer copies its first 16 entries from the
        # literal-backed table, then fills the remaining entries with one handler.
        table = d.read_ram(0x2005c000, 94 * 4)
        words = struct.unpack('<94I', table)
        self.assertEqual(len(set(words[16:])), 1)
        self.assertNotEqual(words[16], 0)
        d.restore('power_on')
        self.assertEqual(d.pc, 0x0c100010)
        self.assertEqual(d.read_ram(0x2005c000, 94 * 4), bytes(94 * 4))
        self.assertEqual(d.mmio, {})
        self.assertEqual(d.instruction_count, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
