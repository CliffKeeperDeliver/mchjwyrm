"""Two experimental 32-bit free-running timers on a deterministic input clock.

One input tick per admitted guest instruction, rolled back on synchronous fault.
This is not a physical frequency/cycle model. Unsupported modes stop execution.
"""
from system_control import PeripheralError


class TimerBank:
    BASES = (0x40084000, 0x40003000)

    def __init__(self):
        self.ticks = 0
        self.channels = {base: dict(load=0xffffffff, value=0xffffffff, control=0, ris=0)
                         for base in self.BASES}

    def advance(self, ticks=1):
        if not isinstance(ticks, int) or ticks < 0:
            raise ValueError('Timer ticks must be a nonnegative integer')
        self.ticks += ticks
        for channel in self.channels.values():
            if channel['control'] & 0x80:
                if ticks >= (channel['value'] or (1 << 32)):
                    channel['ris'] = 1
                channel['value'] = (channel['value'] - ticks) & 0xffffffff

    def _register(self, address, size):
        base = address & ~4095
        offset = address - base
        if base not in self.channels or size != 4 or address % 4 or offset not in (0, 4, 8, 12, 16, 20):
            raise PeripheralError(f'Unimplemented timer access at {address:#x}, width {size}')
        return self.channels[base], offset

    def read(self, address, size):
        channel, offset = self._register(address, size)
        if offset == 12:
            raise PeripheralError('Timer interrupt-clear register is write-only')
        if offset == 20:
            return 0  # Interrupt-enable mode is explicitly unsupported.
        return channel[{0: 'load', 4: 'value', 8: 'control', 16: 'ris'}[offset]]

    def write(self, address, size, value):
        channel, offset = self._register(address, size)
        if not 0 <= value <= 0xffffffff:
            raise PeripheralError('Timer value does not fit a word')
        if offset == 0:
            channel['load'] = channel['value'] = value
        elif offset == 8:
            if value not in (0, 2, 0x82):
                raise PeripheralError('Only stopped/32-bit free-running timer without IRQ or prescaling is supported')
            channel['control'] = value
        elif offset == 12:
            channel['ris'] = 0
        else:
            raise PeripheralError('Timer value/status is read-only')

    def snapshot(self):
        return dict(ticks=self.ticks, channels={base: dict(c) for base, c in self.channels.items()})

    def restore(self, state):
        self.ticks = state['ticks']
        self.channels = {base: dict(c) for base, c in state['channels'].items()}

    def report(self):
        return dict(input_ticks=self.ticks,
                    channels={hex(base): {k: hex(v) for k, v in c.items()} for base, c in self.channels.items()},
                    limitations=[
                        'Both clocks receive one input tick per guest instruction; not physical cycles or elapsed time.',
                        'Disabled initial state and 0xffffffff counter/load are explicit experimental reset assumptions.',
                        'Only channel 0 per block, free-running 32-bit mode, no prescaler or interrupt enable.',
                        'No CMU clock gating, physical slow/fast frequency ratio, elapsed timer, periodic or one-shot mode.',
                    ])
