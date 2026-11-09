"""Fast experimental whole-image runner with explicit high-level board bring-up.

Stock bytes stay immutable. Early board/PSRAM hardware initialization is replaced
by named host services. Every replacement is reported; this is NOT a boot proof
for physical hardware. Strict instruction debugger remains available separately.
"""
from collections import deque
import argparse
import copy
import ctypes
import json
import re
import struct
import time

from debugger import Debugger, HERE, load_profile, arm, UcError, UC_QUERY_TIMEOUT
from system_control import SystemControl, PeripheralError
from iomux_control import IomuxControl
from boot_timer import BootTimers
from gpio_control import GpioControl
from ispi_control import IspiControl
from register_file import RegisterFile
from i2c_control import I2cController
from nand_control import FlashController, SpiNand
from exception_control import ExceptionFrames
from unicorn import UC_PROT_READ, UC_PROT_WRITE, UC_HOOK_MEM_WRITE, UC_HOOK_CODE, UC_HOOK_BLOCK


class BootSystemControl(SystemControl):
    # Configuration only: debug monitor enable and tracing enable. No debugger.
    WORDS = dict(SystemControl.WORDS)
    WORDS[0xdfc] = ('DEMCR', (1 << 16) | (1 << 24))
    WORDS[0xd4c] = ('DFSR_no_debug_events', 0)

    def __init__(self):
        super().__init__()
        self.systick = dict(control=0, reload=0, value=0, countflag=0, pending=False)
        self.systick_phase = 0
        self.systick_divider = 1024  # HLE external clock, not physical cycles.

    def read(self, address, size):
        offset = self._access(address, size)
        if offset in (0x10, 0x14, 0x18) and size == 4:
            s = self.systick
            if offset == 0x10:
                value = s['control'] | (s['countflag'] << 16)
                s['countflag'] = 0
                return value
            return s['reload' if offset == 0x14 else 'value']
        return super().read(address, size)

    def write(self, address, size, value):
        offset = self._access(address, size)
        if offset in (0x10, 0x14, 0x18) and size == 4:
            s = self.systick
            if offset == 0x10:
                s['control'] = value & 7
            elif offset == 0x14:
                s['reload'] = value & 0xffffff
            else:
                s['value'] = s['countflag'] = 0
            return
        return super().write(address, size, value)

    def tick(self, n=1):
        s = self.systick
        before = self.systick_phase
        self.systick_phase += n
        if not s['control'] & 1:
            return
        if s['control'] & 4:
            steps = n
        else:
            steps = self.systick_phase // self.systick_divider - before // self.systick_divider
        # Same per-step rule as one tick at a time: 0 reloads, reaching 0 flags.
        while steps > 0:
            if s['value'] == 0:
                s['value'] = s['reload']
                steps -= 1
                if not s['reload']:
                    break
                continue
            step = min(steps, s['value'])
            s['value'] -= step
            steps -= step
            if s['value'] == 0:
                s['countflag'] = 1
                if s['control'] & 2:
                    s['pending'] = True

    def snapshot(self):
        return dict(super().snapshot(), systick=dict(self.systick), phase=self.systick_phase)

    def restore(self, state):
        super().restore(state)
        self.systick = dict(state['systick'])
        self.systick_phase = state['phase']

    def report(self):
        r = super().report()
        r['limitations'] = ['Single security domain; eager FP frames; no nested exceptions or fault escalation.',
                            'SysTick external clock: 1 tick per 1024 admitted instructions; not physical time.',
                            'Debug events absent; configuration registers only.']
        return r


class BootGpio(GpioControl):
    """GPIO plus the rest of the AON_PSC page 0x40085000 (holds GPIO wake config).

    Offsets other than the modeled wake words are kept in a register file;
    stock early bring-up configures them before the GPIO driver runs.
    """

    def __init__(self):
        super().__init__()
        self.aon_page = RegisterFile('AON_PSC', self.WAKE_BASE)

    def _other(self, address):
        return address & ~4095 == self.WAKE_BASE and address - self.WAKE_BASE not in self.wake_enabled

    def read(self, address, size):
        return self.aon_page.read(address, size) if self._other(address) else super().read(address, size)

    def write(self, address, size, value):
        if self._other(address):
            return self.aon_page.write(address, size, value)
        return super().write(address, size, value)

    def snapshot(self):
        return dict(super().snapshot(), aon_page=self.aon_page.snapshot())

    def restore(self, state):
        super().restore(state)
        self.aon_page.restore(state['aon_page'])


class BootIomux(IomuxControl):
    """IOMUX with every word stored (e.g. 0x44 ISPI BT/RF access gates).

    The strict debugger model keeps its observed-bits masks; the boot runner
    lets stock drivers configure any pin/route word without side effects.
    """

    def _offset(self, address, size):
        offset = address - self.BASE
        if size != 4 or address % 4 or not 0 <= offset < 0x1000:
            raise PeripheralError(f'Unaligned IOMUX access at {address:#x}, width {size}')
        self.values.setdefault(offset, 0)
        return offset

    def write(self, address, size, value):
        self.values[self._offset(address, size)] = value


class BootMachine(Debugger):
    SHA256 = '7c0115d92f7aa5d22dcb3f9a1ff0614904d736609d2d29eebc32f9ab3f7ebab1'
    RAM_CODE = ((0x2005c1c0, 0x20066c18), (0x20068700, 0x2006ad38))
    # Logged register files: (name, base, size, reset {offset: value}, read-only
    # offsets). Bases from atc1441 plat_addr_map_best1503.h unless noted.
    # Writes are stored; never-written registers read reset or 0 and the
    # zero reads are reported as assumed_zero. No hardware side effects.
    REGISTER_BLOCKS = (
        # MEMSC hardware semaphores: CMU 0xc0..0xcc + status 0xd0 (reg_cmu_best1503.h),
        # AON_CMU 0x24..0x30 + status 0x34 (reg_aoncmu_best1503.h).
        ('CMU', 0x40000000, 0x1000, {}, (), (0xc0, 0xd0)),
        # CHIP_ID 0x000e0000 is the existing experimental revision input.
        ('AON_CMU', 0x40080000, 0x1000, {0x00: 0xe0000}, (0x00,), (0x24, 0x34)),
        # PSRAM ULP memory controller (reg_psram_mc_v4.h): REG_400
        # INIT_COMPLETE; REG_404 idle, RX FIFO empty, write buffer drained.
        # The PSRAM itself is host memory, so the controller is always idle.
        ('PSRAM_MC', 0x40160000, 0x1000, {0x400: 1, 0x404: 0x2}, (0x400, 0x404), None),
        # PSRAM PHY = PSRAM_CTRL_BASE + 0x8000 (hal_psram_v4.c; the reference
        # header's DSI guess for 0x40168000 is wrong). REG_060: DLL locked,
        # mid-range delay 0x20 (assumed; only used for timing calibration).
        ('PSRAM_PHY', 0x40168000, 0x1000, {0x060: 0x40 | 0x20}, (0x060,), None),
    )
    I2C_BUSES = (('I2C0', 0x40005000, 14), ('I2C1', 0x40006000, 29), ('I2C2', 0x40060000, 64),
                 ('I2C3', 0x40061000, 65), ('I2C4', 0x40062000, 53), ('I2C5', 0x40063000, 54))
    FAULTS = {'memory_fault', 'unmodeled_register', 'unsupported_exception', 'cpu_error',
              'peripheral_fault', 'exception_contract_fault', 'hle_contract_fault',
              'acceleration_contract_fault', 'unsupported_execution', 'firmware_assert'}

    def __init__(self, profile, *, output=print):
        expected = dict(sha256=self.SHA256, image_base=0x0c100000,
                        ram_base=0x20000000, ram_size=0x160000, startup_offset=0x10)
        if any(profile.get(key) != value for key, value in expected.items()):
            raise ValueError('HLE services are bound to the stock Band 9 Global 2.3.98 profile')
        self.output = output
        self.replacements = []
        self.milestones = []
        self.recent = deque(maxlen=64)
        self.console = bytearray()
        self.accelerations = []
        # Explicit virtual stimulus: channel 4, 600 selects table entry hwver=4.
        # Not measured on the bracelet. Other channels require an explicit input.
        self.adc_samples = {4: 600}
        self.clock_profile = 3  # Virtual boot preset; enum, not Hz or physical cycles.
        self.power_profile = 3
        # Early bring-up 0x0c10170a now runs stock code (ISPI/PMU, timers, AON);
        # only its NOR-controller and PLL-lock leaves are replaced. BT
        # controller bring-up needs the BT CPU, which is not emulated.
        self.hle = {0x0c1018e0: 'norflash_controller_idle', 0x0c101898: 'pll_source_select',
                    0x0c1018d8: 'norflash_reinit_52mhz', 0x0c510390: 'bt_controller_absent',
                    0x00261bf4: 'psram_training',
                    0x0c519bdc: 'debug_uart_init', 0x0c519bf4: 'debug_uart_write',
                    0x0025fd10: 'iomux_uart_setup', 0x0c51a81c: 'board_driver_early_init',
                    0x0c165074: 'debug_cycle_counter_setup',
                    0x002620f8: 'hal_trace_write', 0x00262a80: 'rtc_read_calendar',
                    0x0c519cd4: 'watchdog_disable', 0x0c519c64: 'watchdog_reload',
                    0x0c519c98: 'watchdog_enable', 0x0c101610: 'bootmode_read',
                    0x0025fe90: 'board_pinmux_apply', 0x0c39a098: 'adc_sample',
                    0x00264fea: 'clock_profile_read', 0x00264e58: 'power_profile_apply',
                    0x0025ecd0: 'clock_profile_apply', 0x0025eecc: 'power_transition_up',
                    0x0025eeac: 'power_transition_down'}
        self.markers = {0x0c1002f2: 'internal_runtime_relocation',
                        0x0c10024c: 'external_runtime_relocation',
                        0x0c51aa30: 'c_runtime_entry',
                        0x0c13fa14: 'kernel_entry',
                        0x0c13faa0: 'system_heaps_initialized',
                        0x0c13fbdc: 'kernel_exception_handlers_configured',
                        0x0c13fd7e: 'system_clock_initialized',
                        0x0c140fcc: 'task_trampoline', 0x0c1404e8: 'bringup_task_entry',
                        0x0c1406a4: 'board_gpio_configured',
                        0x0c51ac82: 'board_adc_devices_registered'}
        self.markers.update({0x0c51a8dc: 'flash_geometry_verified',
                             0x0c51a964: 'board_storage_init_returned',
                             0x0c510390: 'bluetooth_driver_init_entry',
                             0x0c51ac86: 'board_init_returned'})
        self.seen_markers = set()
        super().__init__(profile, system_control=True, cache_control=True,
                         aon_chip_id=0xe0000, ram_execution=True,
                         iomux_control=True, timers=True, cmu_control=True)
        self.core = BootSystemControl()
        self.timers = BootTimers()
        # Stock bring-up runs natively, so control blocks are logged register
        # files (see REGISTER_BLOCKS). The strict debugger AON/CMU models stay
        # detached; their pages were mapped by Debugger and now route here.
        self.aon = self.cmu = None
        self.blocks = {}
        for name, base, size, reset, read_only, memsc in self.REGISTER_BLOCKS:
            block = RegisterFile(name, base, size, reset, read_only, memsc)
            for page in range(base, base + size, 4096):
                self.blocks[page] = block
                if page not in self.mmio_pages:
                    self.cpu.mmio_map(page, 4096, self._mmio_read, page, self._mmio_write, page)
                    self.mmio_pages.add(page)
        self.iomux = BootIomux()
        self.gpio = BootGpio()
        self.ispi = IspiControl()
        # DesignWare I2C buses (bases: plat_addr_map_best1503.h, IRQs: best1503.h).
        self.i2c = {base: I2cController(name, base, irq) for name, base, irq in self.I2C_BUSES}
        self.i2c_lines = []  # buses asserting IRQ; recomputed on I2C register access
        # Second flash controller (REAL_FLASH1_CTRL_BASE) drives the SPI-NAND.
        self.nand = FlashController('FLASH1_NAND', 0x40500000, SpiNand())
        for base in self.gpio.PAGES + (IspiControl.BASE, self.nand.base) + tuple(self.i2c):
            self.cpu.mmio_map(base, 4096, self._mmio_read, base, self._mmio_write, base)
            self.mmio_pages.add(base)
        self.exceptions = ExceptionFrames(self)
        self.pending_exception = None
        # Unicorn 2.1.4 memory hooks can leak ITSTATE across Thumb blocks (#2309).
        # Observe only relocated executable SRAM, never stack/data/PSRAM writes.
        self.cpu.hook_del(self._write_hook)
        self._write_hook = self.cpu.hook_add(UC_HOOK_MEM_WRITE, self._before_write,
                                            begin=0x2005c1c0, end=0x20066c17)
        self._runtime_write_hook = self.cpu.hook_add(UC_HOOK_MEM_WRITE, self._before_write,
                                            begin=0x20068700, end=0x2006ad37)
        # Startup tables reference cached and uncached views of the same PSRAM.
        self.psram_size = 16 * 1024 * 1024
        self.psram = ctypes.create_string_buffer(self.psram_size)
        for base in (0x38000000, 0x3c000000):
            self.cpu.mem_map_ptr(base, self.psram_size, UC_PROT_READ | UC_PROT_WRITE,
                                ctypes.addressof(self.psram))
        # A deliberately erased virtual factory sector. Stock validates its
        # header and takes its own "no factory data" branch. It is not copied
        # from the user's band and never supplies fabricated calibration/MACs.
        self.cpu.mem_map(0x287fe000, 0x2000, UC_PROT_READ)
        self.cpu.mem_write(0x287fe000, b'\xff' * 0x2000)
        self.cpu.hook_del(self._code_hook)  # see advance() and _special
        self._code_hook = None
        self._exact = False
        self._address_hooks = {}
        self._special_set = set()
        self._trace_hook = None
        self.trace_blocks = False  # debugging aid: record executed blocks (slower)
        for address in self._unmask_points():
            self.cpu.hook_add(UC_HOOK_BLOCK, self._after_unmask, begin=address, end=address)

    def _before_write(self, cpu, access, address, size, value, userdata):
        # Boot runner deliberately omits per-instruction CPU/RAM checkpoints.
        if self.ram_execution and any(address < end and address + size > start for start, end in self.RAM_CODE):
            self.ram_execution.invalidate(address, size)

    def _device(self, page):
        if page in getattr(self, 'blocks', {}):
            return self.blocks[page], 'block'
        if page in GpioControl.PAGES and hasattr(self, 'gpio'):
            return self.gpio, 'gpio'
        if page == IspiControl.BASE and hasattr(self, 'ispi'):
            return self.ispi, 'ispi'
        if page == 0x40500000 and hasattr(self, 'nand'):
            return self.nand, 'nand'
        if page in getattr(self, 'i2c', {}):
            return self.i2c[page], 'i2c'
        return super()._device(page)

    def _mmio_read(self, cpu, offset, size, page):
        value = super()._mmio_read(cpu, offset, size, page)
        if page in self.i2c:
            self.i2c_lines = [b for b in self.i2c.values() if b.irq_asserted()]
        return value

    def _mmio_write(self, cpu, offset, size, value, page):
        super()._mmio_write(cpu, offset, size, value, page)
        if page in self.i2c:
            self.i2c_lines = [b for b in self.i2c.values() if b.irq_asserted()]

    def write_ram(self, address, data):
        self._check_ram(address, len(data))
        self.cpu.mem_write(address, bytes(data))
        self._before_write(self.cpu, None, address, len(data), 0, None)

    def execution_region(self, address, size=2):
        if 0x00200000 <= address < 0x00360000:
            data_address = address + 0x1fe00000
            if not any(start <= data_address < data_address + size <= end for start, end in self.RAM_CODE):
                return None
        return super().execution_region(address, size)

    # Running executes count-limited chunks with no per-instruction or
    # per-block Python: timers advance and IRQs are delivered between chunks,
    # and address-filtered code hooks serve every special address. SVC entry
    # and exception return are performed inside their hooks, so a chunk only
    # ends early on a terminal stop (breakpoint, fault, assert) and normal
    # chunks are counted exactly. Stepping adds the per-instruction hook.
    QUANTUM = 2048
    ASSERT_TRAPS = (0x002653e0, 0x0c1610c8)
    EXCEPTION_RETURN = 0x0c4ee3dc
    PSRAM_LOOPS = {0xc100282: 0xc100252, 0xc10028c: 0xc100266, 0xc100292: 0xc100270, 0xc10029c: 0xc10027c}

    def _special_addresses(self):
        return (set(self.hle) | set(self.markers) | set(self.breakpoints) | set(self.ASSERT_TRAPS)
                | set(self.PSRAM_LOOPS) | {self.EXCEPTION_RETURN, self.return_address})

    def _sync_address_hooks(self):
        # Unicorn's instruction counter scans every UC_HOOK_CODE hook on every
        # instruction, so addresses that always start a translation block
        # (function entries, returns after BL) use block hooks instead. Only
        # the few mid-block addresses and user breakpoints stay code hooks.
        self._special_set = self._special_addresses()
        code = set(self.breakpoints) | set(self.PSRAM_LOOPS) | {self.EXCEPTION_RETURN, self.return_address}
        wanted = {(a, UC_HOOK_CODE) for a in code}
        wanted |= {(a, UC_HOOK_BLOCK) for a in self._special_set - code}
        for key in set(self._address_hooks) - wanted:
            self.cpu.hook_del(self._address_hooks.pop(key))
        for address, kind in wanted - set(self._address_hooks):
            self._address_hooks[address, kind] = self.cpu.hook_add(
                kind, self._special, begin=address, end=address)

    def _raise_irq_lines(self):
        if self.timers.irq_asserted() and self.exceptions.active != self.timers.IRQ + 16:
            self.core.raise_irq(self.timers.IRQ)
        if self.gpio.irq_asserted() and self.exceptions.active != self.gpio.IRQ + 16:
            self.core.raise_irq(self.gpio.IRQ)
        for bus in self.i2c_lines:
            if self.exceptions.active != bus.irq + 16:
                self.core.raise_irq(bus.irq)

    def _gate(self, cpu, address):
        """Exact-mode checks before executing `address`; True when stopping."""
        if self.instruction_count >= self.max_instructions:
            self._stop('instruction_limit')
            return True
        self._raise_irq_lines()
        irq = self.exceptions.pending_irq()
        if irq is not None:
            self.pending_exception = (irq + 16, address)
            self._stop('exception_entry')
            return True
        ram = self.ram_execution
        if ram and ram.dirty and ram.alias_base <= address < ram.alias_base + ram.size:
            self._stop('ram_code_sync')
            return True
        return False

    def _trace_block(self, cpu, address, size, userdata):
        self.recent.append((address, size))

    def _special(self, cpu, address, size, userdata):
        if not self._exact:
            self._breakpoint_or_return(address) or self._address_services(cpu, address)

    def _breakpoint_or_return(self, address):
        if address == self.return_address:
            self._stop('returned')
            return True
        if address == self._skip_break_once:
            self._skip_break_once = None
        elif address in self.breakpoints:
            self._stop('breakpoint')
            return True
        return False

    def _instruction(self, cpu, address, size, userdata):
        """Exact per-instruction path, active only while stepping."""
        if self._breakpoint_or_return(address) or self._gate(cpu, address):
            return
        if address in self._special_set and self._address_services(cpu, address) is not False:
            return
        if self.execution_region(address, size) is None:
            self._stop('unsupported_execution', address=hex(address))
            return
        self.instruction_count += 1
        self.recent.append((address, size))
        self.timers.advance()
        self.core.tick()

    def _address_services(self, cpu, address):
        """Address-specific services. Returns False to continue normally."""
        if address in self.ASSERT_TRAPS:
            pointer = cpu.reg_read(arm.UC_ARM_REG_R0)
            try:
                source = self.read_memory(pointer, 160).split(b'\0', 1)[0].decode('utf-8', errors='replace')
            except ValueError:
                source = None
            self._stop('firmware_assert', address=hex(address),
                       message_pointer=hex(pointer), source=source,
                       line=cpu.reg_read(arm.UC_ARM_REG_R1),
                       caller=hex(cpu.reg_read(arm.UC_ARM_REG_LR)))
            return
        if address == self.EXCEPTION_RETURN and cpu.reg_read(arm.UC_ARM_REG_LR) >= 0xffffffe0:
            try:
                self.exceptions.leave()  # redirects PC; the bx lr itself does not run
            except ValueError as error:
                self._stop('exception_contract_fault', reason=str(error))
                return
            if not self._exact:  # tail-chain a pending IRQ, as the NVIC would
                self._take_irq_now(self.pc & ~1)
            return
        # Bulk acceleration of stock PSRAM copy/zero loops; the original final
        # compare/exit instructions still execute, preserving flags/registers.
        if address in self.PSRAM_LOOPS:
            r1, r2, r3 = [cpu.reg_read(r) for r in (arm.UC_ARM_REG_R1, arm.UC_ARM_REG_R2, arm.UC_ARM_REG_R3)]
            copying = address in (0x0c100282, 0x0c100292)
            end = r1 if copying else r2
            length = end - r3
            dest = r2 if copying else r3
            if length <= 0 or length % 4 or not (0x38000000 <= dest < dest + length <= 0x38800000 or 0x3c000000 <= dest < dest + length <= 0x3c800000):
                self._stop('acceleration_contract_fault', address=hex(address))
                return
            data = bytes(cpu.mem_read(r3, length)) if copying else r1.to_bytes(4, 'little') * (length // 4)
            cpu.mem_write(dest, data)
            cpu.reg_write(arm.UC_ARM_REG_R3, end)
            if copying:
                cpu.reg_write(arm.UC_ARM_REG_R0, int.from_bytes(data[-4:], 'little'))
                cpu.reg_write(arm.UC_ARM_REG_R2, r2 + length)
            self.accelerations.append(dict(pc=hex(address), destination=hex(dest), bytes=length, operation='copy' if copying else 'fill'))
            cpu.reg_write(arm.UC_ARM_REG_PC, self.PSRAM_LOOPS[address] | 1)
            return
        if address in self.markers and address not in self.seen_markers:
            self.seen_markers.add(address)
            item = dict(stage=self.markers[address], pc=hex(address), instructions=self.instruction_count)
            self.milestones.append(item)
            if self.output:
                self.output(f"Stage: {item['stage']} {item['pc']}")
        if address in self.hle:
            name = self.hle[address]
            result = 0
            lr = cpu.reg_read(arm.UC_ARM_REG_LR)
            if not lr & 1 or lr & ~1 == address or self.execution_region(lr & ~1) is None:
                self._stop('hle_contract_fault', service=name, reason='Invalid HLE return address')
                return
            self.replacements.append(dict(service=name, pc=hex(address), lr=hex(cpu.reg_read(arm.UC_ARM_REG_LR))))
            if name in ('debug_uart_write', 'hal_trace_write'):
                pointer, length = cpu.reg_read(arm.UC_ARM_REG_R0), cpu.reg_read(arm.UC_ARM_REG_R1)
                if length > 65536:
                    self._stop('hle_contract_fault', service=name, length=length)
                    return
                try:
                    data = b''.join(self.read_memory(pointer + i, min(4096, length - i)) for i in range(0, length, 4096))
                except ValueError as error:
                    self._stop('hle_contract_fault', service=name, reason=str(error))
                    return
                self.console.extend(data)
                if self.output:
                    self.output(data.decode('utf-8', errors='replace').rstrip('\n'))
            elif name == 'board_driver_early_init':
                cpu.mem_write(0x200686f4, b'\x01')
                # Virtual NOR descriptor consumed by the unmodified geometry
                # queries at 0x25d060/0x25cc14. This is NOT a full flash dump:
                # only the supplied application image remains readable.
                descriptor = bytearray(24)
                descriptor[0] = 1
                # bes_flash.c asserts page=512, sector=8192. The stock
                # geometry query doubles the sector size when flag 8 is set.
                struct.pack_into('<III', descriptor, 8, 8 * 1024 * 1024, 512, 8)
                cpu.mem_write(0x20066c18, bytes(descriptor))
                self.replacements[-1]['virtual_flash_geometry'] = dict(
                    capacity=8 * 1024 * 1024, page_size=512, sector_size=8192, jedec_id='unknown/zero',
                    descriptor='0x20066c18', complete_flash_dump=False)
            elif name == 'rtc_read_calendar':
                # Deterministic initial date, not the host or bracelet's clock.
                try:
                    self.write_memory(cpu.reg_read(arm.UC_ARM_REG_R0), struct.pack('<H6B', 2026, 1, 1, 4, 0, 0, 0))
                except ValueError as error:
                    self._stop('hle_contract_fault', service=name, reason=str(error))
                    return
            elif name in ('watchdog_disable', 'watchdog_enable'):
                pointer = int.from_bytes(cpu.mem_read(0x0c519cd0, 4), 'little')
                cpu.mem_write(pointer, int(name == 'watchdog_enable').to_bytes(4, 'little'))
            elif name == 'bootmode_read':
                cpu.mem_write(0x200bd474, bytes(4))
            elif name == 'adc_sample':
                channel = cpu.reg_read(arm.UC_ARM_REG_R0)
                if channel not in self.adc_samples:
                    self._stop('hle_contract_fault', service=name, reason='No virtual ADC input configured', channel=channel)
                    return
                result = self.adc_samples[channel]
                self.replacements[-1].update(channel=channel, sample=result)
            elif name == 'clock_profile_read':
                result = self.clock_profile
            elif name.startswith(('clock_profile_', 'power_profile_', 'power_transition_')):
                old = cpu.reg_read(arm.UC_ARM_REG_R0)
                new = cpu.reg_read(arm.UC_ARM_REG_R1) if name.startswith('power_transition_') else old
                if old > 7 or new > 7:
                    self._stop('hle_contract_fault', service=name, reason='Unknown clock/power enum', old=old, new=new)
                    return
                if name == 'clock_profile_apply':
                    self.clock_profile = new
                elif name == 'power_profile_apply':
                    self.power_profile = new
                self.replacements[-1].update(old_profile=old, requested_profile=new, physical_timing=False)
            cpu.reg_write(arm.UC_ARM_REG_R0, result)
            cpu.reg_write(arm.UC_ARM_REG_PC, cpu.reg_read(arm.UC_ARM_REG_LR))
            return
        return False

    def _interrupt(self, cpu, interrupt, userdata):
        if interrupt == 2 and bytes(cpu.mem_read(self.pc - 2, 2))[1] == 0xdf:
            try:  # SVC: stack the frame and enter the stock handler in place.
                self.exceptions.enter(11, self.pc)
            except ValueError as error:
                self._stop('exception_contract_fault', reason=str(error))
        else:
            super()._interrupt(cpu, interrupt, userdata)

    def read_memory(self, address, size):
        if 0 <= size <= 4096 and 0x287fe000 <= address <= address + size <= 0x28800000:
            return bytes(self.cpu.mem_read(address, size))
        if 0 <= size <= 4096 and any(base <= address <= address + size <= base + self.psram_size for base in (0x38000000, 0x3c000000)):
            return bytes(self.cpu.mem_read(address, size))
        return super().read_memory(address, size)

    def write_memory(self, address, data):
        if any(base <= address <= address + len(data) <= base + self.psram_size for base in (0x38000000, 0x3c000000)):
            self.cpu.mem_write(address, bytes(data))
        else:
            self.write_ram(address, data)

    def set_adc_input(self, channel, value):
        if type(channel) is not int or not 0 <= channel <= 255 or type(value) is not int or not 0 <= value <= 65535:
            raise ValueError('ADC requires channel 0..255 and sample 0..65535')
        self.adc_samples[channel] = value

    def advance(self, budget=10000, timeout_ms=2000, *, stepping=False):
        if not isinstance(budget, int) or not 1 <= budget <= 10000000 or not 1 <= timeout_ms <= 30000:
            raise ValueError('Invalid execution budget')
        if self.status in self.FAULTS:
            raise ValueError('HLE faults do not roll back: restore a snapshot or reset before continuing')
        deadline = time.monotonic() + timeout_ms / 1000
        start = self.instruction_count
        self.max_instructions = start + budget
        self._skip_break_once = self.pc if self.status == 'breakpoint' or stepping else None
        self.status = self.fault = self.last_error = None
        self._sync_address_hooks()
        self._exact = stepping
        if not stepping and not self.trace_blocks:
            self.recent.clear()  # a stepping trace would be stale after a run
        if stepping:
            self._code_hook = self.cpu.hook_add(UC_HOOK_CODE, self._instruction)
        if self.trace_blocks and self._trace_hook is None:
            self._trace_hook = self.cpu.hook_add(UC_HOOK_BLOCK, self._trace_block)
        try:
            while self.status is None:
                if self.ram_execution:
                    self.ram_execution.flush_translations()
                remaining = self.max_instructions - self.instruction_count
                left_us = int((deadline - time.monotonic()) * 1e6)
                if remaining <= 0:
                    self.status = 'stepped' if stepping else 'budget'
                elif left_us <= 0:
                    self.status = 'timeout'
                elif stepping:
                    self._emulate_exact(min(left_us, 30_000_000))
                else:
                    self._emulate_chunk(remaining)
                try:
                    if self.status == 'exception_entry':
                        self.exceptions.enter(*self.pending_exception)
                        self.pending_exception = None
                        self.status = None
                    elif self.status == 'ram_code_sync':
                        self.status = None
                except ValueError as error:
                    self.status = 'exception_contract_fault'
                    self.fault = dict(reason=str(error), pc=hex(self.pc))
        finally:
            if self._code_hook is not None:
                self.cpu.hook_del(self._code_hook)
                self._code_hook = None
            if self._trace_hook is not None and not self.trace_blocks:
                self.cpu.hook_del(self._trace_hook)
                self._trace_hook = None
            self._exact = False
        self.last_slice_instructions = self.instruction_count - start
        return self.report()

    def _emulate_exact(self, timeout_us):
        try:
            self.cpu.emu_start(self.pc | 1, 0xffffffff, timeout=timeout_us)
        except UcError as error:
            self.last_error = str(error)
            if self.status is None:
                self.status = 'cpu_error'
        if self.status == 'instruction_limit':
            self.status = 'stepped'
        elif self.status is None:
            self.status = 'timeout' if self.cpu.query(UC_QUERY_TIMEOUT) else 'stopped'

    def _emulate_chunk(self, remaining):
        # Boundaries sit at absolute multiples of QUANTUM, so how a caller
        # slices advance() never changes where IRQs can be taken.
        count = min(remaining, self.QUANTUM - self.instruction_count % self.QUANTUM)
        self._raise_irq_lines()
        irq = self.exceptions.pending_irq()
        if irq is not None:  # IRQs are taken between chunks
            self.pending_exception = (irq + 16, self.pc)
            self.status = 'exception_entry'
            return
        try:
            self.cpu.emu_start(self.pc | 1, 0xffffffff, count=count)
        except UcError as error:
            self.last_error = str(error)
            if self.status is None:
                self.status = 'cpu_error'
        # Only terminal stops end a chunk early; the whole chunk is charged.
        self.instruction_count += count
        self.timers.advance(count)
        self.core.tick(count)

    def _unmask_points(self):
        """Addresses right after every instruction that can unmask IRQs.

        msr basepri/primask (F38n 8811/8810) and cpsie i (B662), found by
        pattern in the image and mapped to the relocated SRAM aliases too.
        """
        points = set()
        for pattern, size in ((rb'[\x80-\x8e]\xf3[\x10\x11]\x88', 4), (rb'\x62\xb6', 2)):
            for match in re.finditer(pattern, self.image):
                offset = match.start()
                if offset % 2:
                    continue
                points.add(self.base + offset + size)
                for file_start, file_end, ram in ((0x19a4, 0xc3fc, 0x0025c1c0), (0xc3fc, 0xea34, 0x00268700)):
                    if file_start <= offset < file_end:
                        points.add(ram + offset - file_start + size)
        return points

    def _take_irq_now(self, address):
        """Enter a deliverable IRQ before `address` runs (inside a hook)."""
        if self.exceptions.active is not None:
            return False
        self._raise_irq_lines()
        irq = self.exceptions.pending_irq()
        if irq is None:
            return False
        try:
            self.exceptions.enter(irq + 16, address)
        except ValueError as error:
            self._stop('exception_contract_fault', reason=str(error))
        return True

    def _after_unmask(self, cpu, address, size, userdata):
        # Hardware takes a pending IRQ as soon as PRIMASK/BASEPRI allow it;
        # chunk boundaries alone would mostly miss short unmasked windows.
        if not self._exact:
            self._take_irq_now(address)

    def save(self, name):
        super().save(name)
        self.checkpoints[name]['boot'] = dict(psram=self.psram.raw[:self.psram_size],
            gpio=self.gpio.snapshot(), ispi=self.ispi.snapshot(),
            blocks={b.name: b.snapshot() for b in set(self.blocks.values())},
            i2c={base: bus.snapshot() for base, bus in self.i2c.items()}, nand=self.nand.snapshot(),
            adc_samples=dict(self.adc_samples),
            clock_profile=self.clock_profile, power_profile=self.power_profile,
            replacements=copy.deepcopy(self.replacements), milestones=copy.deepcopy(self.milestones),
            recent=list(self.recent), console=bytes(self.console), accelerations=copy.deepcopy(self.accelerations),
            seen_markers=set(self.seen_markers), pending_exception=self.pending_exception,
            exception_active=self.exceptions.active, exception_events=copy.deepcopy(self.exceptions.events),
            exception_total=self.exceptions.total_events)

    def restore(self, name):
        super().restore(name)
        state = self.checkpoints[name]
        for block in set(self.blocks.values()):
            block.restore(state['boot']['blocks'][block.name])
        for base, bus in self.i2c.items():
            bus.restore(state['boot']['i2c'][base])
        self.nand.restore(state['boot']['nand'])
        self.i2c_lines = [b for b in self.i2c.values() if b.irq_asserted()]
        self.iomux = BootIomux()
        self.iomux.restore(state['iomux'])
        self.core = BootSystemControl()
        self.core.restore(state['core'])
        self.timers = BootTimers()
        self.timers.restore(state['timers'])
        b = state['boot']
        self.gpio.restore(b['gpio'])
        self.ispi.restore(b['ispi'])
        self.adc_samples = dict(b['adc_samples'])
        self.clock_profile, self.power_profile = b['clock_profile'], b['power_profile']
        ctypes.memmove(ctypes.addressof(self.psram), b['psram'], self.psram_size)
        for key in ('replacements', 'milestones', 'accelerations', 'seen_markers', 'pending_exception'):
            setattr(self, key, copy.deepcopy(b[key]))
        self.console = bytearray(b['console'])
        self.recent = deque(b['recent'], maxlen=64)
        self.exceptions.active = b['exception_active']
        self.exceptions.events = copy.deepcopy(b['exception_events'])
        self.exceptions.total_events = b['exception_total']

    def report(self):
        r = super().report()
        r['mode'] = 'experimental HLE boot; no per-instruction rollback'
        r['hle_replacements'] = list(self.replacements)
        r['milestones'] = list(self.milestones)
        r['trace_tail'] = []
        for address, size in self.recent:  # blocks while running, instructions while stepping
            raw = bytes(self.cpu.mem_read(address, size))
            for decoded in self.decoder.disasm(raw, address):
                r['trace_tail'].append(dict(address=hex(decoded.address), bytes=decoded.bytes.hex(),
                                            instruction=f'{decoded.mnemonic} {decoded.op_str}'))
        r['trace_tail'] = r['trace_tail'][-64:]
        # Running records no trace (see trace_blocks); show what runs next.
        raw = b''
        try:
            raw = self.read_memory(self.pc & ~1, 48) if self.pc else b''
        except ValueError:
            pass
        r['next_instructions'] = [dict(address=hex(i.address), instruction=f'{i.mnemonic} {i.op_str}')
                                  for i in self.decoder.disasm(raw, self.pc & ~1, count=12)]
        r['hardware_accuracy'] = False
        r['bulk_accelerations'] = list(self.accelerations)
        r['console'] = self.console.decode('utf-8', errors='replace')
        r['exceptions'] = dict(active=self.exceptions.active, total_events=self.exceptions.total_events,
                                events=list(self.exceptions.events))
        r['systick'] = dict(self.core.systick)
        r['systick_external_clock_divider'] = self.core.systick_divider
        r['psram'] = dict(size=self.psram_size, aliases=['0x38000000', '0x3c000000'], shared_backing=True)
        r['limitations'] = ['Experimental HLE board services; see hle_replacements.',
            'No display, Bluetooth, sensors, storage controller or full application boot.',
            'No nested interrupts, security banking, MPU, lazy FP stacking or physical timing.',
            'Fault state may be partially committed; reset or restore is required.']
        r['tasks'] = self.tasks()
        r['gpio'] = self.gpio.report()
        r['ispi'] = self.ispi.report()
        r['register_blocks'] = {b.name: b.report() for b in sorted(set(self.blocks.values()), key=lambda b: b.base)}
        r['i2c'] = {bus.name: bus.report() for bus in self.i2c.values()}
        r['nand'] = self.nand.report()
        r['virtual_adc_inputs'] = dict(self.adc_samples)
        r['virtual_clock'] = dict(clock_profile=self.clock_profile, power_profile=self.power_profile,
                                 units='stock enum 0..7, not Hz', physical_timing=False)
        r['virtual_factory_sector'] = dict(address='0x287fe000', size=8192,
            contents='erased (0xff), synthetic, read-only', calibrated=False)
        return r

    def tasks(self):
        tasks = []
        table = int.from_bytes(self.cpu.mem_read(0x20081fe4, 4), 'little')
        count = int.from_bytes(self.cpu.mem_read(0x20081fd0, 4), 'little')
        if not table or not 1 <= count <= 4096:
            return tasks
        try:
            for index in range(count):
                tcb = int.from_bytes(self.read_memory(table + index * 4, 4), 'little')
                if tcb:
                    data = self.read_memory(tcb, 0xb0)
                    tasks.append(dict(tcb=hex(tcb), pid=int.from_bytes(data[12:16], 'little'),
                        name=data[0x90:0xb0].split(b'\0', 1)[0].decode('utf-8', errors='replace'),
                        state=data[0x1c], entry=hex(int.from_bytes(data[0x18:0x1c], 'little'))))
        except ValueError:
            pass
        return tasks


def main(argv=None):
    parser = argparse.ArgumentParser(description='Stock Band 9 OS boot with explicit HLE hardware services')
    parser.add_argument('--until', choices=('hardware', 'bringup', 'storage'), default='hardware')
    parser.add_argument('--adc', action='append', default=[], metavar='CHANNEL=VALUE', help='Explicit virtual ADC input; repeatable')
    parser.add_argument('--gpio', action='append', default=[], metavar='PIN=LEVEL', help='Initial virtual GPIO input; repeatable')
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--instructions', type=int, default=100000000)
    parser.add_argument('--report', type=str, default=str(HERE / 'results/boot-machine.json'))
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 600 or not 1 <= args.instructions <= 100000000:
        parser.error('seconds: 1..600; instructions: 1..100000000')
    d = BootMachine(load_profile(HERE / 'profiles/n66gl_2.3.98.json'))
    try:
        for value in args.adc:
            channel, sample = (int(item, 0) for item in value.split('='))
            d.set_adc_input(channel, sample)
        for value in args.gpio:
            pin, level = (int(item, 0) for item in value.split('='))
            d.gpio.drive(pin, level)
    except ValueError as error:
        parser.error(str(error))
    target_pc = {'bringup': 0x0c1404e8, 'storage': 0x0c51a964}.get(args.until)
    if target_pc is not None:
        d.set_breakpoint(target_pc)
    started = time.monotonic()
    while time.monotonic() - started < args.seconds and d.instruction_count < args.instructions:
        r = d.advance(min(2000000, args.instructions - d.instruction_count),
                      max(1, min(10000, int((args.seconds - (time.monotonic() - started)) * 1000))))
        print(r['status'], r['instructions'], hex(d.pc), r.get('fault'), flush=True)
        if r['status'] not in ('timeout', 'budget'):
            break
    from pathlib import Path
    target = Path(args.report)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(r, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    for task in r['tasks']:
        print(f"PID {task['pid']}: {task['name']} (state {task['state']})")
    print('Elapsed seconds:', round(time.monotonic() - started, 2))
    print('Report:', target.resolve())
    return 0 if target_pc is not None and r['status'] == 'breakpoint' and d.pc == target_pc else 2


if __name__ == '__main__':
    raise SystemExit(main())
