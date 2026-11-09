"""Real-firmware execution checks plus synthetic CPU/memory guard fixtures."""
import hashlib
from pathlib import Path
import random
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from emulator import Emulator, load_profile


class FirmwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profile = load_profile(HERE / 'profiles/n66gl_2.3.98.json')

    def test_real_function_preserves_upper_bits_sets_low_three_to_two(self):
        rng = random.Random(9)
        inputs = [0, 0xffffffff, 7, 8, 0x80000000, 0xa5a5a5a5]
        inputs += [rng.getrandbits(32) for _ in range(26)]
        address = self.profile['demo_register']
        for initial in inputs:
            with self.subTest(initial=hex(initial)):
                emu = Emulator(self.profile, {address: initial})
                result = emu.run(self.profile['demo_function_offset'], {'r4': 0x12345678})
                self.assertEqual(result['status'], 'returned')
                self.assertEqual(emu.mmio[address] & 7, 2)
                self.assertEqual(emu.mmio[address] >> 3, initial >> 3)
                self.assertEqual(result['registers']['r4'], '0x12345678')
                self.assertEqual([e['kind'] for e in result['mmio_events']], ['read', 'write'])
                self.assertEqual(result['instructions'], 6)

    def test_real_startup_stops_at_first_unknown_register(self):
        result = Emulator(self.profile).run(self.profile['startup_offset'])
        self.assertEqual(result['status'], 'memory_fault')
        self.assertEqual(result['fault']['address'], '0x4008005c')
        self.assertEqual(result['fault']['pc'], '0xc1012ce')
        self.assertEqual(result['fault']['access'], 'unmapped_read')

    def test_neighbor_in_same_mmio_page_is_not_implicitly_modeled(self):
        result = Emulator(self.profile, {0x40080058: 0}).run(self.profile['demo_function_offset'])
        self.assertEqual(result['status'], 'unmodeled_register')
        self.assertEqual(result['fault']['address'], '0x4008005c')

    def test_wrong_firmware_hash_rejected(self):
        profile = dict(self.profile, sha256='0' * 64)
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            Emulator(profile)


class HarnessGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def fixture(self, code, **kwargs):
        """Synthetic Thumb fixtures; NOT passed off as stock firmware tests."""
        image = Path(self.temp.name) / 'fixture.bin'
        image.write_bytes(bytes.fromhex(code))
        profile = dict(name='synthetic test fixture', image=str(image),
                       sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                       image_base=0x0c100000, ram_base=0x20000000, ram_size=0x10000,
                       stack_pointer=0x2000fff0, return_address=0x10000000)
        return Emulator(profile, **kwargs)

    def test_instruction_budget_and_trace_cap(self):
        emu = self.fixture('fee7', trace_limit=4)  # b .
        result = emu.run(0, max_instructions=37)
        self.assertEqual(result['status'], 'instruction_limit')
        self.assertEqual(result['instructions'], 37)
        self.assertEqual(len(result['trace_tail']), 4)

    def test_wall_clock_budget(self):
        emu = self.fixture('fee7', trace_limit=0)
        result = emu.run(0, max_instructions=10000000, timeout_ms=10)
        self.assertEqual(result['status'], 'timeout')
        self.assertLess(result['instructions'], 10000000)

    def test_flash_write_is_rejected(self):
        emu = self.fixture('01607047')  # str r1,[r0]; bx lr
        result = emu.run(0, {'r0': 0x0c100000, 'r1': 123})
        self.assertEqual(result['status'], 'memory_fault')
        self.assertEqual(result['fault']['access'], 'protected_write')

    def test_ram_buffers_and_return_value(self):
        emu = self.fixture('016842607047')  # ldr r1,[r0]; str r2,[r0,#4]; bx lr
        emu.write_ram(0x20000000, b'ABCD' + bytes(4))
        result = emu.run(0, {'r0': 0x20000000, 'r2': 0x87654321})
        self.assertEqual(result['status'], 'returned')
        self.assertEqual(result['registers']['r1'], '0x44434241')
        self.assertEqual(emu.read_ram(0x20000004, 4), bytes.fromhex('21436587'))
        with self.assertRaises(ValueError):
            emu.write_ram(0x20000000, b'A')
        with self.assertRaises(ValueError):
            emu.read_ram(0x1fffffff, 1)

    def test_ram_cannot_execute(self):
        emu = self.fixture('0047')  # bx r0
        emu.write_ram(0x20000000, bytes.fromhex('7047'))
        result = emu.run(0, {'r0': 0x20000001})
        self.assertEqual(result['status'], 'memory_fault')
        self.assertEqual(result['fault']['access'], 'protected_fetch')

    def test_page_padding_is_not_code(self):
        emu = self.fixture('00bf')  # nop; next bytes are page padding
        result = emu.run(0)
        self.assertEqual(result['status'], 'unsupported_execution')

    def test_exceptions_are_reported(self):
        emu = self.fixture('00df')  # svc #0
        result = emu.run(0)
        self.assertEqual(result['status'], 'unsupported_exception')

    def test_fresh_state_required(self):
        emu = self.fixture('7047', trace_limit=0)
        result = emu.run(0)
        self.assertEqual(result['status'], 'returned')
        self.assertEqual(result['trace_tail'], [])
        with self.assertRaises(ValueError):
            emu.run(0)

    def test_bad_entry_and_mmio_overlap_rejected(self):
        emu = self.fixture('7047')
        with self.assertRaises(ValueError):
            emu.run(1)
        with self.assertRaises(ValueError):
            emu.run(2)
        with self.assertRaisesRegex(ValueError, 'Overlapping'):
            self.fixture('7047', mmio_registers={0x20000000: 0})


if __name__ == '__main__':
    unittest.main(verbosity=2)
