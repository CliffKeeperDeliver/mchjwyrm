"""BES GPIO atomic-register model inferred from the stock 2.3.98 driver.

Bank table: image literal 0x2c70096c. Direction, output and IRQ operations:
0x0c5161a4..0x0c5164aa. Inputs are explicitly driven, initially low; no
electrical pin mux, analog pulls or spontaneous board inputs are fabricated.
"""
import copy
from system_control import PeripheralError


class GpioControl:
    BASES = (0x40081000, 0x40089000, 0x4008a000)
    WAKE_BASE = 0x40085000
    PAGES = BASES + (WAKE_BASE,)
    IRQ = 44
    PAIRS = {0x00: ('output', True), 0x0c: ('output', False),
             0x04: ('direction', True), 0x10: ('direction', False),
             0x1c: ('enabled', True), 0x18: ('enabled', False),
             0x34: ('masked', True), 0x30: ('masked', False),
             0x38: ('edge', True), 0x20: ('edge', False),
             0x3c: ('polarity', True), 0x24: ('polarity', False),
             0x48: ('debounce', True), 0x28: ('debounce', False)}

    def __init__(self):
        self._asserted = False
        self.wake_enabled = {0x80: 0, 0x84: 0, 0x88: 0}
        self.banks = {base: dict(output=0, direction=0, enabled=0, masked=0,
                      edge=0, polarity=0, debounce=0, inputs=0, pending=0, driven=0)
                      for base in self.BASES}

    def _access(self, address, size):
        base = address & ~4095
        if base not in self.banks or size != 4 or address % 4:
            raise PeripheralError('GPIO requires an aligned word in a known bank')
        return base, self.banks[base], address - base

    @staticmethod
    def _mask(base):
        return 0xffff if base == GpioControl.BASES[2] else 0xffffffff

    def _level_pending(self, b):
        active = ~(b['inputs'] ^ b['polarity']) & b['driven'] & ~b['direction']
        return (b['pending'] & b['edge']) | (active & ~b['edge'])

    def read(self, address, size):
        if size == 4 and address - self.WAKE_BASE in self.wake_enabled:
            return self.wake_enabled[address - self.WAKE_BASE]
        base, b, offset = self._access(address, size)
        if offset in self.PAIRS:
            return b[self.PAIRS[offset][0]]
        if offset == 0x50:
            return ((b['inputs'] & ~b['direction']) | (b['output'] & b['direction'])) & self._mask(base)
        if offset in (0x40, 0x44):
            pending = self._level_pending(b) & b['enabled']
            return pending & ~b['masked'] if offset == 0x40 else pending
        raise PeripheralError(f'Unimplemented GPIO read {address:#x}')

    def write(self, address, size, value):
        if size == 4 and address - self.WAKE_BASE in self.wake_enabled and 0 <= value <= 0xffffffff:
            offset = address - self.WAKE_BASE
            self.wake_enabled[offset] = value & (0xffff if offset == 0x88 else 0xffffffff)
            return
        base, b, offset = self._access(address, size)
        if not 0 <= value <= 0xffffffff:
            raise PeripheralError('GPIO value must fit a word')
        value &= self._mask(base)
        if offset in self.PAIRS:
            field, setting = self.PAIRS[offset]
            b[field] = b[field] | value if setting else b[field] & ~value
        elif offset == 0x4c:
            b['pending'] &= ~value
        else:
            raise PeripheralError(f'Unimplemented GPIO write {address:#x}')
        self._update_irq()

    def drive(self, pin, level):
        if not isinstance(pin, int) or not 0 <= pin < 80 or level not in (0, 1):
            raise ValueError('GPIO input needs pin 0..79 and level 0/1')
        b = self.banks[self.BASES[pin // 32]]
        bit = 1 << (pin % 32)
        old = bool(b['inputs'] & bit)
        b['inputs'] = b['inputs'] | bit if level else b['inputs'] & ~bit
        b['driven'] |= bit
        if old != bool(level) and bool(b['polarity'] & bit) == bool(level):
            b['pending'] |= bit & b['edge'] & ~b['direction']
        self._update_irq()

    def irq_asserted(self):
        return self._asserted

    def _update_irq(self):
        self._asserted = any(self._level_pending(b) & b['enabled'] & ~b['masked'] for b in self.banks.values())

    def snapshot(self):
        return copy.deepcopy(dict(banks=self.banks, wake=self.wake_enabled))

    def restore(self, state):
        self.banks = copy.deepcopy(state['banks'])
        self.wake_enabled = dict(state['wake'])
        self._update_irq()

    def report(self):
        return dict(banks={hex(base): {k: hex(v) for k, v in b.items()} for base, b in self.banks.items()},
                    irq=self.IRQ, asserted=self.irq_asserted(),
                    wake_enabled={hex(k): hex(v) for k, v in self.wake_enabled.items()}, limitations=[
                        'Register roles inferred from stock driver; physical reset values unverified.',
                        'Undriven inputs read low and do not generate level IRQs.',
                        'Debounce/wake retain configuration only; no electrical pulls, sleep or pinmux routing.'])
