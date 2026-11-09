"""BES internal SPI (ISPI, PL022-derived) and analog-interface register file.

Controller layout: atc1441 reg_spi.h (SSPCR0..SSPRXCR). Frame protocol is
what the stock driver encodes (hal_analogif with 2 arbitration padding bits):
    read  = ((1 << 24) | reg << 16) << 2, reply value in bits 2..17
    write = (reg << 16 | value) << 2
Slave (chip select) is SSPCR1 bits 7..10. Writing register 0 selects a page
(stock table 0x20065e38: 0xa000 + page * 0x10); reading register 0 of the PMU
returns CHIP_ID. Only reset values listed in RESET are known; reading any
other never-written register reads 0 and is listed in assumed_zero.
"""
import copy
from system_control import PeripheralError


class IspiControl:
    BASE = 0x40009000
    PAD = 2
    FIFO = 8
    # (cs, page, reg) -> 16-bit value.
    RESET = {
        # PMU CHIP_ID: ANA_VAL_CHIP_ID 0x153 (best1503) in bits 4..15
        # (hal_analogif_best1503.c). Metal bits 0..3 unmeasured; stock reads
        # the metal revision from AON CHIP_ID instead and ignores this value.
        (3, 0, 0x00): 0x1530,
    }
    WORDS = {0x00: 'cr0', 0x04: 'cr1', 0x10: 'cpsr', 0x14: 'imsc', 0x24: 'dmacr',
             0x28: 'srcnt', 0x2c: 'dlycr', 0x88: 'rxcr'}

    def __init__(self):
        self.regs = {name: 0 for name in self.WORDS.values()}
        self.rx = []
        self.analog = {}   # (cs, page, reg) -> value
        self.page = {}     # cs -> selected page index (default 0)
        self.log = []      # recent analog accesses for reports
        self.frames = 0
        self.assumed_zero = set()  # never-written registers that read as 0
        self.overrun = False

    def _offset(self, address, size):
        if size != 4 or address % 4:
            raise PeripheralError('ISPI requires aligned word accesses')
        return address - self.BASE

    @property
    def cs(self):
        return (self.regs['cr1'] >> 7) & 0xf

    def read(self, address, size):
        offset = self._offset(address, size)
        if offset in self.WORDS:
            return self.regs[self.WORDS[offset]]
        if offset == 0x0c:  # SSPSR: transfers complete instantly.
            return 0x3 | (4 if self.rx else 0) | (8 if len(self.rx) >= self.FIFO else 0)
        if offset == 0x08:
            if not self.rx:
                raise PeripheralError('ISPI RX FIFO read while empty')
            return self.rx.pop(0)
        if offset in (0x18, 0x1c):  # RIS/MIS: TX always has room, RX per FIFO.
            ris = 8 | (4 if self.rx else 0) | int(self.overrun)
            return ris if offset == 0x18 else ris & self.regs['imsc']
        raise PeripheralError(f'Unimplemented ISPI read at {address:#x}')

    def write(self, address, size, value):
        offset = self._offset(address, size)
        if offset in self.WORDS:
            self.regs[self.WORDS[offset]] = value
            return
        if offset == 0x20:  # SSPICR: RORIC clears the overrun flag.
            if value & 1:
                self.overrun = False
            return
        if offset == 0x08:
            if not self.regs['cr1'] & 2:
                raise PeripheralError('ISPI frame written while SSE is disabled')
            reply = self._frame(value)
            # PL022: a full RX FIFO drops the frame and raises RORRIS.
            if len(self.rx) >= self.FIFO:
                self.overrun = True
            else:
                self.rx.append(reply)
            return
        raise PeripheralError(f'Unimplemented ISPI write at {address:#x}: {value:#x}')

    def _frame(self, frame):
        bits = ((self.regs['cr0'] >> 16) & 0x1f) + 1
        # Stock configures 27-bit frames: R flag, 8-bit reg, 16-bit data, padding.
        if not 25 + self.PAD <= bits <= 32 or frame >> bits or frame & ((1 << self.PAD) - 1):
            raise PeripheralError(f'Unsupported ISPI frame {frame:#x} ({bits} bits)')
        word = frame >> self.PAD
        cs, reg = self.cs, (word >> 16) & 0xff
        if word >> 25:
            raise PeripheralError(f'Unknown ISPI command bits in {frame:#x}')
        self.frames += 1
        if word >> 24:
            value = self.read_register(cs, reg)
            self._note('r', cs, reg, value)
            return value << self.PAD
        value = word & 0xffff
        self._note('w', cs, reg, value)
        if reg == 0 and value & 0xff0f == 0xa000:
            self.page[cs] = (value >> 4) & 0xf
        else:  # Other register-0 writes (e.g. 0xcafe on cs 4) are plain data.
            self.analog[cs, self.page.get(cs, 0), reg] = value
        return 0

    def read_register(self, cs, reg):
        key = (cs, self.page.get(cs, 0), reg)
        if key in self.analog:
            return self.analog[key]
        if key in self.RESET:
            return self.RESET[key]
        self.assumed_zero.add(key)
        return 0

    def _note(self, kind, cs, reg, value):
        self.log.append((kind, cs, self.page.get(cs, 0), reg, value))
        del self.log[:-64]

    def snapshot(self):
        return copy.deepcopy(dict(regs=self.regs, rx=self.rx, analog=self.analog, assumed_zero=self.assumed_zero, overrun=self.overrun,
                                  page=self.page, log=self.log, frames=self.frames))

    def restore(self, state):
        s = copy.deepcopy(state)
        self.regs, self.rx, self.analog, self.page = s['regs'], s['rx'], s['analog'], s['page']
        self.log, self.frames, self.assumed_zero = s['log'], s['frames'], s['assumed_zero']
        self.overrun = s['overrun']

    def report(self):
        return dict(frames=self.frames, chip_select=self.cs,
                    registers={f'cs{c} page{p} {r:#04x}': hex(v) for (c, p, r), v in sorted(self.analog.items())},
                    assumed_zero=[f'cs{c} page{p} {r:#04x}' for c, p, r in sorted(self.assumed_zero)],
                    recent=[f'{k} cs{c} p{p} {r:#04x}={v:#06x}' for k, c, p, r, v in self.log[-16:]],
                    limitations=['Instant transfers; no clock, DMA or interrupt timing.',
                                 'Analog registers are storage only: no PMU, RF or clock side effects.',
                                 'Reset values are known only where listed; others read 0 (see assumed_zero).'])
