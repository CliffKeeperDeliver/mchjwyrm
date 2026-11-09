"""Resumable local Cortex-M33 debugger for the stock Band 9 image."""
import argparse
import copy
import json
from pathlib import Path
import shlex
import struct
import time

from emulator import (Emulator, HERE, REGISTERS, arm, load_profile, number,
                      UcError, UC_QUERY_TIMEOUT)
from unicorn import UC_HOOK_MEM_WRITE
from system_control import SystemControl, PeripheralError
from cache_control import CacheControl
from aon_control import AonControl
from ram_execution import RamExecution
from iomux_control import IomuxControl
from timer_control import TimerBank
from cmu_control import CmuControl

SPECIAL_REGISTERS = {name.lower(): getattr(arm, 'UC_ARM_REG_' + name)
                     for name in ('MSP', 'PSP', 'CONTROL', 'PRIMASK', 'BASEPRI', 'FAULTMASK')}


class Debugger(Emulator):
    """Pause/resume with breakpoints and in-memory CPU/RAM/MMIO checkpoints.

    Snapshots are native CPU context objects, never pickle or executable files.
    A faulting instruction's CPU/RAM/modeled-register changes are rolled back.
    Optional instruction-clock timers; no asynchronous devices or interrupt delivery.
    """

    def __init__(self, profile, mmio_registers=None, trace_limit=256, system_control=False,
                 cache_control=False, aon_chip_id=None, ram_execution=False, iomux_control=False,
                 timers=False, cmu_control=False):
        self.core = None
        self.caches = {}
        self.aon = None
        self.iomux = None
        self.timers = None
        self.cmu = None
        self.ram_execution = None
        super().__init__(profile, mmio_registers, trace_limit)
        self.breakpoints = set()
        self.checkpoints = {}
        self.mmio_pages = {address & ~4095 for address in self.mmio}
        self._checkpoint_cpu = None
        self._undo_ram = []
        self._checkpoint_mmio = {}
        self._skip_break_once = None
        self._write_hook = self.cpu.hook_add(UC_HOOK_MEM_WRITE, self._before_write)
        self.cpu.reg_write(arm.UC_ARM_REG_PC, (self.base + profile['startup_offset']) | 1)
        self.status = 'ready'
        self.last_error = None
        self.last_slice_instructions = 0
        self._checkpoint_core = None
        if system_control:
            self.enable_core()
        if cache_control:
            self.enable_caches()
        if aon_chip_id is not None:
            self.enable_aon(aon_chip_id)
        if ram_execution:
            self.enable_ram_execution()
        if iomux_control:
            self.enable_iomux()
        if timers:
            self.enable_timers()
        if cmu_control:
            self.enable_cmu()

    def run(self, *args, **kwargs):
        raise ValueError('Debugger uses advance()/step(); one-shot run() belongs to Emulator')

    @property
    def pc(self):
        return self.cpu.reg_read(arm.UC_ARM_REG_PC)

    def _instruction(self, cpu, address, size, userdata):
        ram = self.ram_execution
        if ram and ram.dirty and ram.alias_base <= address < ram.alias_base + ram.size:
            self._stop('ram_code_sync')
            return
        if self.core and self.core.ready_irqs():
            self._stop('interrupt_delivery_required', irqs=self.core.ready_irqs())
            return
        if address == self._skip_break_once:
            self._skip_break_once = None
        elif address in self.breakpoints:
            self._stop('breakpoint')
            return
        super()._instruction(cpu, address, size, userdata)
        if self.status is None:
            self._checkpoint_cpu = cpu.context_save()
            self._undo_ram = []
            self._checkpoint_mmio = dict(self.mmio)
            self._checkpoint_core = self.core.snapshot() if self.core else None
            self._checkpoint_caches = {base: cache.snapshot() for base, cache in self.caches.items()}
            self._checkpoint_aon = self.aon.snapshot() if self.aon else None
            self._checkpoint_iomux = self.iomux.snapshot() if self.iomux else None
            self._checkpoint_timers = self.timers.snapshot() if self.timers else None
            self._checkpoint_cmu = self.cmu.snapshot() if self.cmu else None
            if self.timers:
                self.timers.advance()

    def _before_write(self, cpu, access, address, size, value, userdata):
        start = self.profile['ram_base']
        if start <= address and address + size <= start + self.profile['ram_size']:
            self._undo_ram.append((address, bytes(cpu.mem_read(address, size))))
            if self.ram_execution:
                self.ram_execution.invalidate(address, size)

    def advance(self, budget=10000, timeout_ms=2000, *, stepping=False):
        if not 1 <= budget <= 10000000 or not 1 <= timeout_ms <= 30000:
            raise ValueError('Invalid execution budget')
        if self.status == 'returned':
            raise ValueError('Execution returned. Restore a snapshot or set another PC.')
        self._skip_break_once = self.pc if self.status == 'breakpoint' or stepping else None
        self.status = None
        self.fault = None
        self.last_error = None
        self._checkpoint_cpu = None
        self._undo_ram = []
        start_count = self.instruction_count
        self.max_instructions = start_count + budget
        deadline = time.monotonic() + timeout_ms / 1000
        try:
            while True:
                if self.ram_execution:
                    self.ram_execution.flush_translations()
                remaining_us = int((deadline - time.monotonic()) * 1000000)
                if remaining_us <= 0:
                    self.status = 'timeout'
                    break
                self.cpu.emu_start(self.pc | 1, 0xffffffff, timeout=remaining_us)
                if self.status != 'ram_code_sync':
                    break
                self.status = None
        except UcError as error:
            self.last_error = str(error)
            if self.status is None:
                self.status = 'cpu_error'
        if self.status is None:
            self.status = 'timeout' if self.cpu.query(UC_QUERY_TIMEOUT) else 'stopped'
        if self.status == 'instruction_limit':
            self.status = 'stepped' if stepping else 'budget'
        if self.status in ('memory_fault', 'unmodeled_register', 'unsupported_exception', 'cpu_error', 'peripheral_fault'):
            if self._checkpoint_cpu is not None:
                for address, old in reversed(self._undo_ram):
                    self.write_ram(address, old)
                self.mmio = dict(self._checkpoint_mmio)
                if self.core and self._checkpoint_core is not None:
                    self.core.restore(self._checkpoint_core)
                for base, state in self._checkpoint_caches.items():
                    self.caches[base].restore(state)
                if self.aon and self._checkpoint_aon is not None:
                    self.aon.restore(self._checkpoint_aon)
                if self.iomux and self._checkpoint_iomux is not None:
                    self.iomux.restore(self._checkpoint_iomux)
                if self.timers and self._checkpoint_timers is not None:
                    self.timers.restore(self._checkpoint_timers)
                if self.cmu and self._checkpoint_cmu is not None:
                    self.cmu.restore(self._checkpoint_cmu)
                self.cpu.context_restore(self._checkpoint_cpu)
                if self.fault is None:
                    self.fault = {}
                self.fault['instruction_state_rolled_back'] = True
        self.last_slice_instructions = self.instruction_count - start_count
        return self.report()

    def step(self, count=1):
        return self.advance(count, stepping=True)

    def set_register(self, name, value):
        if name not in REGISTERS or name == 'xpsr' or not 0 <= value < 2**32:
            raise ValueError('Use r0..r12, sp, lr or pc with an unsigned 32-bit value')
        if name == 'pc':
            address = value & ~1
            if self.execution_region(address) is None:
                raise ValueError('PC must point inside an executable region')
            value = address | 1
            self.status = 'ready'
            self.fault = None
        self.cpu.reg_write(REGISTERS[name], value)

    def set_breakpoint(self, address):
        if address % 2 or self.execution_region(address) is None:
            raise ValueError('Breakpoint must be an even executable address')
        self.breakpoints.add(address)

    def write_ram(self, address, data):
        self._check_ram(address, len(data))
        self.cpu.mem_write(address, bytes(data))
        if self.ram_execution:
            self.ram_execution.invalidate(address, len(data))

    def execution_region(self, address, size=2):
        ram = self.ram_execution
        if ram and ram.alias_base <= address < address + size <= ram.alias_base + ram.size:
            return 'ram_execution', ram.alias_base, ram.size
        return super().execution_region(address, size)

    def enable_ram_execution(self):
        if self.ram_execution:
            return
        alias = self.profile.get('ram_execution_alias')
        if alias is None:
            raise ValueError('Profile has no researched RAM execution alias')
        self.ram_execution = RamExecution(self.cpu, self.profile['ram_base'],
                                          self.profile['ram_size'], number(alias))

    def read_memory(self, address, size):
        if not 0 <= size <= 4096:
            raise ValueError('Read size must be 0..4096 bytes')
        ranges = [(self.base, len(self.image)),
                  (self.profile['ram_base'], self.profile['ram_size'])]
        ranges += [(base, len(self.image)) for base in self.profile.get('image_read_aliases', [])]
        if self.ram_execution:
            ranges.append((self.ram_execution.alias_base, self.ram_execution.size))
        if not any(base <= address <= address + size <= base + length for base, length in ranges):
            raise ValueError('Inspection is limited to image/aliases and RAM; use devices for MMIO state')
        return bytes(self.cpu.mem_read(address, size))

    def model_register(self, address, value):
        if address % 4 or not 0 <= address <= 0xfffffffc or not 0 <= value < 2**32:
            raise ValueError('Expected aligned 32-bit address and value')
        page = address & ~4095
        if self.core and page == SystemControl.BASE:
            raise ValueError('System-control registers are managed by the core model')
        if page in self.caches:
            raise ValueError('Cache registers are managed by the cache model')
        if self.aon and page == AonControl.BASE:
            raise ValueError('AON registers are managed by the AON model')
        if self.iomux and page == IomuxControl.BASE:
            raise ValueError('IOMUX registers are managed by the IOMUX model')
        if self.timers and page in TimerBank.BASES:
            raise ValueError('Timer registers are managed by the timer model')
        if self.cmu and page == CmuControl.BASE:
            raise ValueError('CMU registers are managed by the CMU model')
        if page not in self.mmio_pages:
            if any(page <= end and page + 4095 >= start for start, end, _ in self.cpu.mem_regions()):
                raise ValueError('MMIO page overlaps existing image/RAM region')
            self.cpu.mmio_map(page, 4096, self._mmio_read, page, self._mmio_write, page)
            self.mmio_pages.add(page)
        self.mmio[address] = value

    def enable_core(self):
        if self.core:
            return
        page = SystemControl.BASE
        if page in self.mmio_pages or any(page <= end and page + 4095 >= start
                                          for start, end, _ in self.cpu.mem_regions()):
            raise ValueError('System-control page already mapped; restore/reset before enabling core')
        self.core = SystemControl()
        self.cpu.mmio_map(page, SystemControl.SIZE, self._mmio_read, page, self._mmio_write, page)
        self.mmio_pages.add(page)

    def enable_caches(self):
        if self.caches:
            return
        for page in CacheControl.BASES:
            if any(page <= end and page + 4095 >= start for start, end, _ in self.cpu.mem_regions()):
                raise ValueError('Cache page already mapped; restore/reset before enabling caches')
        for page in CacheControl.BASES:
            self.caches[page] = CacheControl(page)
            self.cpu.mmio_map(page, 4096, self._mmio_read, page, self._mmio_write, page)
            self.mmio_pages.add(page)

    def enable_aon(self, chip_id):
        if self.aon:
            if self.aon.chip_id != chip_id:
                raise ValueError('AON identity already selected; restore a prior snapshot first')
            return
        device = AonControl(chip_id)
        page = device.BASE
        if any(page <= end and page + 4095 >= start for start, end, _ in self.cpu.mem_regions()):
            raise ValueError('AON page already mapped; do not combine with plain storage registers')
        self.cpu.mmio_map(page, 4096, self._mmio_read, page, self._mmio_write, page)
        self.aon = device
        self.mmio_pages.add(page)

    def _device(self, page):
        if page == SystemControl.BASE:
            return self.core, 'core'
        if page == AonControl.BASE:
            return self.aon, 'aon'
        if page == IomuxControl.BASE:
            return self.iomux, 'iomux'
        if page in TimerBank.BASES:
            return self.timers, 'timer'
        if page == CmuControl.BASE:
            return self.cmu, 'cmu'
        return self.caches.get(page), 'cache'

    def enable_cmu(self):
        if self.cmu:
            return
        page = CmuControl.BASE
        if any(page <= end and page + 4095 >= start for start, end, _ in self.cpu.mem_regions()):
            raise ValueError('CMU page already mapped; restore/reset before enabling it')
        self.cpu.mmio_map(page, 4096, self._mmio_read, page, self._mmio_write, page)
        self.cmu = CmuControl()
        self.mmio_pages.add(page)

    def enable_timers(self):
        if self.timers:
            return
        for page in TimerBank.BASES:
            if any(page <= end and page + 4095 >= start for start, end, _ in self.cpu.mem_regions()):
                raise ValueError('Timer page already mapped; restore/reset before enabling timers')
        for page in TimerBank.BASES:
            self.cpu.mmio_map(page, 4096, self._mmio_read, page, self._mmio_write, page)
            self.mmio_pages.add(page)
        self.timers = TimerBank()

    def enable_iomux(self):
        if self.iomux:
            return
        page = IomuxControl.BASE
        if any(page <= end and page + 4095 >= start for start, end, _ in self.cpu.mem_regions()):
            raise ValueError('IOMUX page already mapped; restore/reset before enabling it')
        self.cpu.mmio_map(page, 4096, self._mmio_read, page, self._mmio_write, page)
        self.iomux = IomuxControl()
        self.mmio_pages.add(page)

    def _mmio_read(self, cpu, offset, size, page):
        device, name = self._device(page)
        if device:
            address = page + offset
            try:
                value = device.read(address, size)
                self._event(name + '_read', address, value)
                return value
            except PeripheralError as error:
                self._stop('peripheral_fault', access='read', address=hex(address), size=size, reason=str(error))
                return 0
        return super()._mmio_read(cpu, offset, size, page)

    def _mmio_write(self, cpu, offset, size, value, page):
        device, name = self._device(page)
        if device:
            address = page + offset
            try:
                device.write(address, size, value)
                self._event(name + '_write', address, value)
            except PeripheralError as error:
                self._stop('peripheral_fault', access='write', address=hex(address), size=size, reason=str(error))
            return
        return super()._mmio_write(cpu, offset, size, value, page)

    def save(self, name):
        if not name or (name not in self.checkpoints and len(self.checkpoints) >= 8):
            raise ValueError('Use a nonempty name; at most 8 snapshots')
        self.checkpoints[name] = dict(
            context=self.cpu.context_save(),
            ram=bytes(self.cpu.mem_read(self.profile['ram_base'], self.profile['ram_size'])),
            mmio=dict(self.mmio), pages=set(self.mmio_pages),
            status=self.status, fault=copy.deepcopy(self.fault), error=self.last_error,
            instructions=self.instruction_count, trace=list(self.trace), events=list(self.events),
            event_count=self.event_count, last_slice=self.last_slice_instructions,
            core=self.core.snapshot() if self.core else None,
            caches={base: cache.snapshot() for base, cache in self.caches.items()},
            aon=self.aon.snapshot() if self.aon else None,
            iomux=self.iomux.snapshot() if self.iomux else None,
            timers=self.timers.snapshot() if self.timers else None,
            cmu=self.cmu.snapshot() if self.cmu else None,
            ram_execution=self.ram_execution is not None,
        )

    def restore(self, name):
        if name not in self.checkpoints:
            raise ValueError('No such snapshot')
        state = self.checkpoints[name]
        if self.ram_execution and not state['ram_execution']:
            self.ram_execution.disable()
            self.ram_execution = None
        # Remove pages added after snapshot before enabling a restored RAM alias.
        for page in self.mmio_pages - state['pages']:
            self.cpu.mem_unmap(page, 4096)
        if state['ram_execution'] and not self.ram_execution:
            self.enable_ram_execution()
        for page in state['pages'] - self.mmio_pages:
            self.cpu.mmio_map(page, 4096, self._mmio_read, page, self._mmio_write, page)
        self.mmio_pages = set(state['pages'])
        self.mmio = dict(state['mmio'])
        self.core = SystemControl(state['core']['irq_count']) if state['core'] is not None else None
        if self.core:
            self.core.restore(state['core'])
        self.caches = {base: CacheControl(base) for base in state['caches']}
        for base, cache_state in state['caches'].items():
            self.caches[base].restore(cache_state)
        self.aon = AonControl(state['aon']['chip_id']) if state['aon'] is not None else None
        if self.aon:
            self.aon.restore(state['aon'])
        self.iomux = IomuxControl() if state['iomux'] is not None else None
        if self.iomux:
            self.iomux.restore(state['iomux'])
        self.timers = TimerBank() if state['timers'] is not None else None
        if self.timers:
            self.timers.restore(state['timers'])
        self.cmu = CmuControl() if state['cmu'] is not None else None
        if self.cmu:
            self.cmu.restore(state['cmu'])
        self.write_ram(self.profile['ram_base'], state['ram'])
        self.cpu.context_restore(state['context'])
        self.status, self.fault = state['status'], copy.deepcopy(state['fault'])
        self.last_error = state['error']
        self.instruction_count, self.event_count = state['instructions'], state['event_count']
        self.last_slice_instructions = state['last_slice']
        self.trace.clear()
        self.trace.extend(state['trace'])
        self.events.clear()
        self.events.extend(state['events'])
        self._checkpoint_cpu = None
        self._undo_ram = []
        self._skip_break_once = None

    def report(self):
        return dict(
            profile=self.profile['name'], image_sha256=self.sha256,
            status=self.status, fault=self.fault, error=self.last_error,
            instructions=self.instruction_count, last_slice_instructions=self.last_slice_instructions,
            registers={name: hex(self.cpu.reg_read(reg)) for name, reg in (REGISTERS | SPECIAL_REGISTERS).items()},
            breakpoints=[hex(v) for v in sorted(self.breakpoints)], snapshots=list(self.checkpoints),
            memory_regions=[dict(start=hex(start), end=hex(end), permissions=permissions)
                            for start, end, permissions in self.cpu.mem_regions()],
            modeled_registers={hex(address): hex(value) for address, value in self.mmio.items()},
            trace_tail=list(self.trace), mmio_events=list(self.events), mmio_event_count=self.event_count,
            mmio_model='Explicit plain 32-bit storage; hardware side effects are not simulated',
            full_device_emulation=False, assumptions=self.profile.get('assumptions', []),
            system_control=self.core.report() if self.core else None,
            cache_control=[cache.report() for cache in self.caches.values()],
            aon_control=self.aon.report() if self.aon else None,
            iomux_control=self.iomux.report() if self.iomux else None,
            timers=self.timers.report() if self.timers else None,
            cmu_control=self.cmu.report() if self.cmu else None,
            ram_execution=self.ram_execution.report() if self.ram_execution else None,
        )

    def disassemble(self, address=None, count=12):
        if not 1 <= count <= 64:
            raise ValueError('Disassembly count must be 1..64')
        address = self.pc if address is None else address
        region = self.execution_region(address)
        if region is None:
            raise ValueError('Address outside executable regions')
        size = min(count * 4, region[1] + region[2] - address)
        raw = self.read_memory(address, size)
        return [f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic} {i.op_str}'.rstrip()
                for i in self.decoder.disasm(raw, address, count=count)]


HELP = '''Commands (numbers: decimal or 0xhex):
  step [N]                 execute N instructions (default 1)
  run [BUDGET] [TIMEOUT_MS] continue (defaults: 10000 instructions, 2000 ms)
  break ADDRESS            stop before executing this address
  delete ADDRESS           remove breakpoint
  regs                     show CPU and special registers
  set REGISTER VALUE       edit r0..r12, sp, lr, pc
  x ADDRESS [SIZE]         inspect image/RAM bytes (default 64)
  write32 ADDRESS VALUE    edit one RAM word
  disasm [ADDRESS] [COUNT] disassemble from current PC or address
  model ADDRESS VALUE      explicitly model ONE 32-bit MMIO register as storage
  devices                  show modeled registers (no real hardware connected)
  core on / core           enable/show partial SCB/NVIC register model
  cache on / cache         enable/show experimental BES cache-control model
  aon CHIP_ID / aon        enable/show AON with explicit experimental identity
  ram on / ram             enable/show shared RAM execution alias from profile
  iomux on / iomux         enable/show experimental pin configuration state
  timers on / timers       enable/show timers driven by the guest instruction clock
  cmu on / cmu             enable/show observed timer-source configuration
  irq NUMBER               set an external IRQ pending (delivery not yet implemented)
  save NAME / restore NAME save/restore CPU+RAM+MMIO in this session (max 8)
  maps                     show mapped memory
  trace                    show recent instructions
  status                   show stop reason
  report [PATH]            save JSON report
  help / quit
Unknown hardware stops execution. `model` is an experimental assumption,
not an accurate peripheral implementation. Snapshots are session-local.'''


def summary(debugger):
    print(f'{debugger.status}: PC=0x{debugger.pc:08x}; attempted instructions={debugger.instruction_count}')
    if debugger.fault:
        print(json.dumps(debugger.fault))


def command(debugger, line):
    words = shlex.split(line, comments=True, posix=True)
    if not words:
        return True
    op, *args = words
    if op in ('quit', 'exit') and not args:
        return False
    if op == 'help' and not args:
        print(HELP)
    elif op in ('step', 'run') and len(args) <= (1 if op == 'step' else 2):
        count = number(args[0]) if args else (1 if op == 'step' else 10000)
        debugger.step(count) if op == 'step' else debugger.advance(
            count, timeout_ms=number(args[1]) if len(args) > 1 else 2000)
        summary(debugger)
    elif op in ('break', 'delete') and len(args) == 1:
        address = number(args[0])
        debugger.set_breakpoint(address) if op == 'break' else debugger.breakpoints.discard(address)
    elif op == 'set' and len(args) == 2:
        debugger.set_register(args[0], number(args[1]))
    elif op == 'write32' and len(args) == 2:
        debugger.write_ram(number(args[0]), struct.pack('<I', number(args[1])))
    elif op == 'model' and len(args) == 2:
        debugger.model_register(number(args[0]), number(args[1]))
        print('Explicit experimental storage register added; no hardware side effects modeled.')
    elif op == 'core' and args in ([], ['on']):
        if args:
            debugger.enable_core()
        print(json.dumps(debugger.core.report() if debugger.core else {'enabled': False}, indent=2))
    elif op == 'irq' and len(args) == 1:
        if not debugger.core:
            raise ValueError('Enable core first')
        debugger.core.raise_irq(number(args[0]))
    elif op == 'cache' and args in ([], ['on']):
        if args:
            debugger.enable_caches()
        print(json.dumps([cache.report() for cache in debugger.caches.values()], indent=2))
    elif op == 'aon' and len(args) <= 1:
        if args:
            debugger.enable_aon(number(args[0]))
        print(json.dumps(debugger.aon.report() if debugger.aon else {'enabled': False}, indent=2))
    elif op == 'ram' and args in ([], ['on']):
        if args:
            debugger.enable_ram_execution()
        print(json.dumps(debugger.ram_execution.report() if debugger.ram_execution else {'enabled': False}, indent=2))
    elif op == 'iomux' and args in ([], ['on']):
        if args:
            debugger.enable_iomux()
        print(json.dumps(debugger.iomux.report() if debugger.iomux else {'enabled': False}, indent=2))
    elif op == 'timers' and args in ([], ['on']):
        if args:
            debugger.enable_timers()
        print(json.dumps(debugger.timers.report() if debugger.timers else {'enabled': False}, indent=2))
    elif op == 'cmu' and args in ([], ['on']):
        if args:
            debugger.enable_cmu()
        print(json.dumps(debugger.cmu.report() if debugger.cmu else {'enabled': False}, indent=2))
    elif op == 'x' and 1 <= len(args) <= 2:
        address = number(args[0])
        data = debugger.read_memory(address, number(args[1]) if len(args) == 2 else 64)
        for start in range(0, len(data), 16):
            print(f'{address + start:08x}: {data[start:start+16].hex(" ")}')
    elif op == 'disasm' and len(args) <= 2:
        print('\n'.join(debugger.disassemble(number(args[0]) if args else None,
                                             number(args[1]) if len(args) == 2 else 12)))
    elif op in ('save', 'restore') and len(args) == 1:
        getattr(debugger, op)(args[0])
        summary(debugger)
    elif op == 'status' and not args:
        summary(debugger)
    elif op in ('regs', 'devices', 'maps', 'trace') and not args:
        field = dict(regs='registers', devices='modeled_registers', maps='memory_regions', trace='trace_tail')[op]
        print(json.dumps(debugger.report()[field], indent=2))
    elif op == 'report' and len(args) <= 1:
        path = Path(args[0]) if args else HERE / 'results/debug-session.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(debugger.report(), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('Saved:', path)
    else:
        raise ValueError('Unknown command or wrong arguments. Type help.')
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path, default=HERE / 'profiles/n66gl_2.3.98.json')
    parser.add_argument('--script', type=Path, help='execute debugger commands from a text file; no Python/eval')
    parser.add_argument('--core', action='store_true', help='enable partial SCB/NVIC configuration model')
    parser.add_argument('--cache', action='store_true', help='enable experimental BES cache-control model')
    parser.add_argument('--aon-chip-id', type=number, help='explicit experimental CHIP_ID register, not device detection')
    parser.add_argument('--ram-execution', action='store_true', help='enable shared SRAM execution alias from profile')
    parser.add_argument('--iomux', action='store_true', help='enable experimental pin configuration state')
    parser.add_argument('--timers', action='store_true', help='enable experimental timers (one input tick per instruction)')
    parser.add_argument('--cmu', action='store_true', help='enable observed CMU timer-source configuration')
    args = parser.parse_args()
    debugger = Debugger(load_profile(args.profile), system_control=args.core, cache_control=args.cache,
                        aon_chip_id=args.aon_chip_id, ram_execution=args.ram_execution,
                        iomux_control=args.iomux, timers=args.timers, cmu_control=args.cmu)
    print('Band 9 / Cortex-M33 debugger. Stock image loaded. Type help.')
    summary(debugger)
    if args.script:
        for index, line in enumerate(args.script.read_text(encoding='utf-8').splitlines(), 1):
            try:
                if not command(debugger, line):
                    break
            except (ValueError, UcError, OSError, struct.error) as error:
                print(f'Script line {index}: {error}')
                return 1
        return 0  # A debugging script may intentionally stop at a firmware fault.
    while True:
        try:
            if not command(debugger, input('band> ')):
                break
        except (EOFError, KeyboardInterrupt):
            print('\nStopped. Snapshots were session-local.')
            break
        except (ValueError, UcError, OSError, struct.error) as error:
            print('Error:', error)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
