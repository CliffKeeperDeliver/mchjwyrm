"""Offline ARM function harness. No ADB, network, flashing or host-code execution."""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'python_deps'))
try:
    from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
    from unicorn import (
        Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS,
        UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC, UC_HOOK_CODE,
        UC_HOOK_MEM_INVALID, UC_HOOK_INTR, UC_QUERY_TIMEOUT,
        UC_MEM_READ_UNMAPPED, UC_MEM_WRITE_UNMAPPED, UC_MEM_FETCH_UNMAPPED,
        UC_MEM_READ_PROT, UC_MEM_WRITE_PROT, UC_MEM_FETCH_PROT,
    )
    from unicorn import arm_const as arm
except ImportError as error:
    raise SystemExit('Install dependencies: python -m pip install --target '
                     'band_emulator/python_deps -r band_emulator/requirements.txt') from error

REGISTERS = {f'r{i}': getattr(arm, f'UC_ARM_REG_R{i}') for i in range(13)}
REGISTERS.update(sp=arm.UC_ARM_REG_SP, lr=arm.UC_ARM_REG_LR,
                 pc=arm.UC_ARM_REG_PC, xpsr=arm.UC_ARM_REG_XPSR)
ACCESS_NAMES = {
    UC_MEM_READ_UNMAPPED: 'unmapped_read', UC_MEM_WRITE_UNMAPPED: 'unmapped_write',
    UC_MEM_FETCH_UNMAPPED: 'unmapped_fetch', UC_MEM_READ_PROT: 'protected_read',
    UC_MEM_WRITE_PROT: 'protected_write', UC_MEM_FETCH_PROT: 'protected_fetch',
}


def number(value):
    return int(value, 0) if isinstance(value, str) else int(value)


def load_profile(path):
    path = Path(path).resolve()
    profile = json.loads(path.read_text(encoding='utf-8'))
    profile['image'] = str((path.parent / profile['image']).resolve())
    for key in ('image_base', 'ram_base', 'ram_size', 'stack_pointer',
                'return_address', 'startup_offset', 'demo_function_offset', 'demo_register'):
        profile[key] = number(profile[key])
    profile['image_read_aliases'] = [number(v) for v in profile.get('image_read_aliases', [])]
    return profile


class Emulator:
    """One fresh CPU/memory state per experiment; unknown hardware is an error."""

    def __init__(self, profile, mmio_registers=None, trace_limit=128):
        self.profile = dict(profile)
        self.image = Path(profile['image']).read_bytes()
        self.sha256 = hashlib.sha256(self.image).hexdigest()
        if self.sha256 != profile['sha256']:
            raise ValueError('Firmware SHA-256 mismatch; refusing to use offsets from another build')
        if not 0 < len(self.image) <= 64 * 1024 * 1024:
            raise ValueError('Invalid image size')
        if not 0 <= trace_limit <= 10000:
            raise ValueError('Trace limit must be between 0 and 10000')
        self.base = profile['image_base']
        self.return_address = profile['return_address']
        self.mmio = dict(mmio_registers or {})
        self.trace = deque(maxlen=trace_limit)
        self.events = deque(maxlen=256)
        self.event_count = 0
        self.instruction_count = 0
        self.status = None
        self.fault = None
        self.used = False
        self.decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M33)
        regions = [(self.base, (len(self.image) + 4095) & ~4095, 'image'),
                   (profile['ram_base'], profile['ram_size'], 'ram'),
                   (self.return_address, 4096, 'return trap')]
        regions += [(base, regions[0][1], 'read-only image alias')
                    for base in profile.get('image_read_aliases', [])]
        for address, value in self.mmio.items():
            if address % 4 or not 0 <= value <= 0xffffffff:
                raise ValueError('Modeled registers must be aligned 32-bit addresses/values')
        pages = sorted({address & ~4095 for address in self.mmio})
        regions += [(page, 4096, 'mmio') for page in pages]
        ordered = sorted(regions)
        for index, (base, size, _) in enumerate(ordered):
            if base < 0 or size <= 0 or base % 4096 or size % 4096 or base + size > 2**32:
                raise ValueError('Invalid memory region')
            if index and ordered[index - 1][0] + ordered[index - 1][1] > base:
                raise ValueError('Overlapping memory regions')
        sp = profile['stack_pointer']
        if sp % 8 or not profile['ram_base'] < sp <= profile['ram_base'] + profile['ram_size']:
            raise ValueError('Stack pointer must be aligned and inside RAM')
        self.cpu.mem_map(self.base, regions[0][1], UC_PROT_READ | UC_PROT_EXEC)
        self.cpu.mem_write(self.base, self.image)
        for base in profile.get('image_read_aliases', []):
            self.cpu.mem_map(base, regions[0][1], UC_PROT_READ)
            self.cpu.mem_write(base, self.image)
        self.cpu.mem_map(profile['ram_base'], profile['ram_size'], UC_PROT_READ | UC_PROT_WRITE)
        self.cpu.mem_map(self.return_address, 4096, UC_PROT_READ | UC_PROT_EXEC)
        self.cpu.mem_write(self.return_address, bytes.fromhex('00be'))  # Never executed.
        for page in pages:
            self.cpu.mmio_map(page, 4096, self._mmio_read, page, self._mmio_write, page)
        self.cpu.reg_write(arm.UC_ARM_REG_SP, sp)
        self.cpu.reg_write(arm.UC_ARM_REG_LR, self.return_address | 1)
        self._code_hook = self.cpu.hook_add(UC_HOOK_CODE, self._instruction)
        self.cpu.hook_add(UC_HOOK_MEM_INVALID, self._invalid_memory)
        self.cpu.hook_add(UC_HOOK_INTR, self._interrupt)

    def _stop(self, status, **details):
        if self.status is None:
            self.status = status
            if details:
                self.fault = dict(pc=hex(self.cpu.reg_read(arm.UC_ARM_REG_PC)), **details)
        self.cpu.emu_stop()

    def execution_region(self, address, size=2):
        if self.base <= address < address + size <= self.base + len(self.image):
            return 'image', self.base, len(self.image)
        return None

    def _instruction(self, cpu, address, size, _):
        if address == self.return_address:
            self._stop('returned')
            return
        region = self.execution_region(address, size)
        if region is None:
            self._stop('unsupported_execution', address=hex(address))
            return
        if self.instruction_count >= self.max_instructions:
            self._stop('instruction_limit')
            return
        self.instruction_count += 1
        if self.trace.maxlen:
            raw = bytes(cpu.mem_read(address, size))
            decoded = next(self.decoder.disasm(raw, address, count=1), None)
            self.trace.append(dict(address=hex(address), offset=hex(address - region[1]), region=region[0],
                                   bytes=raw.hex(), instruction=(
                                       f'{decoded.mnemonic} {decoded.op_str}'.strip() if decoded else '?')))

    def _invalid_memory(self, cpu, access, address, size, value, _):
        self._stop('memory_fault', access=ACCESS_NAMES.get(access, str(access)),
                   address=hex(address), size=size, value=hex(value))
        return False  # Never fabricate memory or silently return success.

    def _interrupt(self, cpu, interrupt, _):
        self._stop('unsupported_exception', interrupt=interrupt)

    def _event(self, kind, address, value):
        self.event_count += 1
        self.events.append(dict(kind=kind, address=hex(address), value=hex(value),
                                pc=hex(self.cpu.reg_read(arm.UC_ARM_REG_PC))))

    def _mmio_read(self, cpu, offset, size, page):
        address = page + offset
        if size != 4 or address not in self.mmio:
            self._stop('unmodeled_register', access='read', address=hex(address), size=size)
            return 0  # Callback ABI requires a value; this experiment is already FAILED.
        value = self.mmio[address]
        self._event('read', address, value)
        return value

    def _mmio_write(self, cpu, offset, size, value, page):
        address = page + offset
        if size != 4 or address not in self.mmio:
            self._stop('unmodeled_register', access='write', address=hex(address), size=size)
            return
        self.mmio[address] = value & 0xffffffff
        self._event('write', address, self.mmio[address])

    def write_ram(self, address, data):
        """Prepare guest input buffers before running a function."""
        if self.used:
            raise ValueError('Input memory must be initialized before execution')
        self._check_ram(address, len(data))
        self.cpu.mem_write(address, bytes(data))

    def read_ram(self, address, size):
        """Read guest output buffers, without exposing arbitrary host memory."""
        self._check_ram(address, size)
        return bytes(self.cpu.mem_read(address, size))

    def _check_ram(self, address, size):
        start = self.profile['ram_base']
        if size < 0 or not start <= address <= address + size <= start + self.profile['ram_size']:
            raise ValueError('Buffer is outside modeled RAM')

    def run(self, offset, registers=None, max_instructions=10000, timeout_ms=2000):
        if self.used:
            raise ValueError('Create a fresh Emulator for each experiment')
        if offset % 2 or not 0 <= offset < len(self.image):
            raise ValueError('Entry offset must be an even offset inside the image')
        if not 1 <= max_instructions <= 10000000 or not 1 <= timeout_ms <= 30000:
            raise ValueError('Invalid execution budget')
        for name, value in (registers or {}).items():
            if name not in REGISTERS or name in ('pc', 'xpsr') or not 0 <= value <= 0xffffffff:
                raise ValueError('Invalid initial register: ' + name)
            self.cpu.reg_write(REGISTERS[name], value)
        self.used = True
        self.max_instructions = max_instructions
        error = None
        try:
            self.cpu.emu_start((self.base + offset) | 1, 0xffffffff, timeout=timeout_ms * 1000)
        except UcError as exc:
            error = str(exc)
            if self.status is None:
                self.status = 'cpu_error'
        if self.status is None:
            self.status = 'timeout' if self.cpu.query(UC_QUERY_TIMEOUT) else 'stopped'
        return dict(
            profile=self.profile['name'], image_sha256=self.sha256,
            cpu='Cortex-M33 (Unicorn)', status=self.status, error=error, fault=self.fault,
            entry_offset=hex(offset), entry_address=hex(self.base + offset),
            instructions=self.instruction_count, max_instructions=max_instructions,
            timeout_ms=timeout_ms, registers={k: hex(self.cpu.reg_read(v)) for k, v in REGISTERS.items()},
            mmio_model='explicit 32-bit read/write storage; real hardware behavior NOT validated',
            mmio_registers={hex(k): hex(v) for k, v in self.mmio.items()},
            mmio_event_count=self.event_count, mmio_events=list(self.events),
            trace_tail=list(self.trace), assumptions=self.profile.get('assumptions', []),
            full_device_emulation=False,
        )


def assignment(value):
    name, sep, raw = value.partition('=')
    if not sep:
        raise argparse.ArgumentTypeError('Expected NAME=VALUE')
    try:
        return name.lower(), number(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Expected a decimal or hexadecimal value') from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=HERE / 'profiles/n66gl_2.3.98.json')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--demo', action='store_true', help='default: execute real register-update function with explicit mock')
    mode.add_argument('--startup', action='store_true', help='start image; stop on first unknown dependency')
    mode.add_argument('--entry-offset', type=lambda s: int(s, 0))
    parser.add_argument('--register', action='append', type=assignment, default=[], metavar='R0=0x1234')
    parser.add_argument('--mmio-register', action='append', type=assignment, default=[], metavar='0xADDRESS=0xVALUE')
    parser.add_argument('--max-instructions', type=int, default=10000)
    parser.add_argument('--timeout-ms', type=int, default=2000)
    parser.add_argument('--trace-limit', type=int, default=128)
    parser.add_argument('--output', type=Path, default=HERE / 'results/last-run.json')
    args = parser.parse_args()
    try:
        profile = load_profile(args.profile)
        demo = not args.startup and args.entry_offset is None
        mmio = {number(name): value for name, value in args.mmio_register}
        if demo:
            mmio.setdefault(profile['demo_register'], 0xa5a5a5a5)
        offset = (profile['demo_function_offset'] if demo else
                  profile['startup_offset'] if args.startup else args.entry_offset)
        before = dict(mmio)
        emulator = Emulator(profile, mmio, args.trace_limit)
        result = emulator.run(offset, dict(args.register), args.max_instructions, args.timeout_ms)
        if demo:
            expected = (before[profile['demo_register']] & ~7) | 2
            actual = emulator.mmio[profile['demo_register']]
            result['demo_check'] = dict(expected=hex(expected), actual=hex(actual),
                                       passed=result['status'] == 'returned' and actual == expected)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(f"{result['status']}: {result['instructions']} instructions; PC={result['registers']['pc']}")
        if result['fault']:
            print(json.dumps(result['fault']))
        if demo:
            print('Real firmware function / modeled register:', result['demo_check'])
        print('Report:', args.output.resolve())
        return 0 if result['status'] == 'returned' and (not demo or result['demo_check']['passed']) else 2
    except (ValueError, OSError, KeyError, UcError) as exc:
        print('Error:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
