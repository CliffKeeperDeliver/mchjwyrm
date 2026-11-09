"""Observed CMU timer-source configuration; no clock tree or reset emulation."""
from system_control import PeripheralError


class CmuControl:
    BASE = 0x40000000

    def __init__(self):
        self.periph_clk = 0

    def _check(self, address, size):
        if address != self.BASE + 0x5c or size != 4:
            raise PeripheralError(f'Unimplemented CMU access at {address:#x}, width {size}')

    def read(self, address, size):
        self._check(address, size)
        return self.periph_clk

    def write(self, address, size, value):
        self._check(address, size)
        if value < 0 or value & ~0xc00:
            raise PeripheralError('Only CMU timer-source selection bits 10 and 11 are supported')
        self.periph_clk = value

    def snapshot(self):
        return dict(periph_clk=self.periph_clk)

    def restore(self, state):
        self.periph_clk = state['periph_clk']

    def report(self):
        return dict(periph_clk=hex(self.periph_clk), limitations=[
            'Experimental zeroed PERIPH_CLK; only observed timer-source bits 10 and 11 are retained.',
            'No physical frequencies, PLL, clock gating or reset effects; instruction-clock timers stay abstract.',
        ])
