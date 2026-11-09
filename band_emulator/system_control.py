"""Partial Cortex-M33 system-control register model (single security domain).

Register layout/masks: Arm CMSIS core_cm33.h (references/core_cm33.h).
This is configuration state, not full architectural exception/FPU/trap emulation.
Unimplemented addresses/requests are rejected rather than returning zero.
"""
class PeripheralError(ValueError):
    pass


class SystemControl:
    BASE = 0xe000e000
    SIZE = 0x1000
    # Only the configuration registers required by the observed startup path.
    WORDS = {0xd08: ('VTOR', 0xffffff80), 0xd10: ('SCR', 0x1e),
             0xd14: ('CCR', 0x7051a), 0xd24: ('SHCSR', 0xf0000),
             0xd88: ('CPACR', 0xf00000)}

    def __init__(self, irq_count=78):
        if not 1 <= irq_count <= 240:
            raise ValueError('IRQ count must be 1..240')
        self.irq_count = irq_count
        self.banks = (irq_count + 31) // 32
        self.values = {offset: 0 for offset in self.WORDS}
        self.shpr = bytearray(12)
        self.priorities = bytearray(irq_count)
        self.enabled = [0] * self.banks
        self.pending = [0] * self.banks

    def _bank_mask(self, index):
        return (1 << min(32, self.irq_count - index * 32)) - 1

    def _access(self, address, size):
        offset = address - self.BASE
        if size not in (1, 2, 4) or address % size or not 0 <= offset <= self.SIZE - size:
            raise PeripheralError('Invalid system-control access width/alignment')
        return offset

    def _priority_array(self, offset, size):
        if 0xd18 <= offset and offset + size <= 0xd24:
            return self.shpr, offset - 0xd18
        if 0x400 <= offset and offset + size <= 0x400 + self.irq_count:
            return self.priorities, offset - 0x400
        return None

    def _bank(self, offset):
        for base, kind in ((0x100, 'set_enable'), (0x180, 'clear_enable'),
                           (0x200, 'set_pending'), (0x280, 'clear_pending'),
                           (0x300, 'active')):
            if base <= offset < base + self.banks * 4 and offset % 4 == 0:
                return kind, (offset - base) // 4
        return None

    def read(self, address, size):
        offset = self._access(address, size)
        priority = self._priority_array(offset, size)
        if priority:
            data, start = priority
            return int.from_bytes(data[start:start + size], 'little')
        if size != 4:
            raise PeripheralError('This system register requires a word access')
        if offset in self.WORDS:
            return self.values[offset]
        bank = self._bank(offset)
        if bank:
            kind, index = bank
            if 'enable' in kind:
                return self.enabled[index]
            if 'pending' in kind:
                return self.pending[index]
            return 0  # No interrupt handlers have been entered in this model.
        if offset == 0x004:  # ICTR.INTLINESNUM
            return self.banks - 1
        raise PeripheralError(f'Unimplemented system register at {address:#x}')

    def write(self, address, size, value):
        offset = self._access(address, size)
        if not 0 <= value < 1 << (size * 8):
            raise PeripheralError('Value does not fit access width')
        priority = self._priority_array(offset, size)
        if priority:
            data, start = priority
            data[start:start + size] = value.to_bytes(size, 'little')
            return
        if size != 4:
            raise PeripheralError('This system register requires a word access')
        if offset in self.WORDS:
            if offset == 0xd24 and value & 0x30f000:
                raise PeripheralError('Pending system exceptions are not implemented')
            self.values[offset] = value & self.WORDS[offset][1]
            return
        bank = self._bank(offset)
        if bank:
            kind, index = bank
            value &= self._bank_mask(index)
            if kind == 'active':
                raise PeripheralError('NVIC active state is read-only')
            target = self.enabled if 'enable' in kind else self.pending
            if kind.startswith('set'):
                target[index] |= value
            else:
                target[index] &= ~value
            return
        raise PeripheralError(f'Unimplemented system register at {address:#x}')

    def raise_irq(self, irq):
        if not 0 <= irq < self.irq_count:
            raise ValueError('IRQ outside modeled range')
        self.pending[irq // 32] |= 1 << (irq % 32)

    def ready_irqs(self):
        return [irq for irq in range(self.irq_count)
                if (self.pending[irq // 32] & self.enabled[irq // 32]) & (1 << (irq % 32))]

    def snapshot(self):
        return dict(values=dict(self.values), shpr=bytes(self.shpr), priorities=bytes(self.priorities),
                    enabled=list(self.enabled), pending=list(self.pending), irq_count=self.irq_count)

    def restore(self, state):
        if state['irq_count'] != self.irq_count:
            raise ValueError('System-control snapshot configuration mismatch')
        self.values = dict(state['values'])
        self.shpr[:] = state['shpr']
        self.priorities[:] = state['priorities']
        self.enabled, self.pending = list(state['enabled']), list(state['pending'])

    def report(self):
        return dict(
            registers={self.WORDS[offset][0]: hex(value) for offset, value in self.values.items()},
            system_priorities=list(self.shpr), irq_priorities=list(self.priorities),
            irq_count=self.irq_count, enabled=[hex(v) for v in self.enabled],
            pending=[hex(v) for v in self.pending], ready_irqs=self.ready_irqs(),
            limitations=[
                'Initial configuration values are zero for this experiment, not verified chip reset values.',
                '78 external IRQs inferred from the 94-entry startup table; not a verified hardware inventory.',
                'All 8 priority bits retained; implemented priority width is not established.',
                'No exception entry/return, preemption, security banking, timer or CPU trap/FPU permission enforcement.',
                'Enabled pending interrupts stop execution before the next instruction, even if CPU-masked.',
            ],
        )
