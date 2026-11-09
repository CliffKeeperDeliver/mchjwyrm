"""Logged register-file model for BES control blocks used by the boot runner.

Writes are stored; reads return the stored value. A register that was never
written reads its RESET entry, or zero. Every zero read of a never-written
register is recorded (`assumed_zero`) so a report shows exactly which inputs
were invented. No clock, power or reset side effects are modeled.
"""
import copy
from system_control import PeripheralError


class RegisterFile:
    def __init__(self, name, base, size=0x1000, reset=None, read_only=()):
        self.name, self.base, self.size = name, base, size
        self.reset = dict(reset or {})
        self.read_only = set(read_only)
        self.values = {}
        self.assumed_zero = set()
        self.writes = 0

    def _offset(self, address, size):
        offset = address - self.base
        if size != 4 or address % 4 or not 0 <= offset < self.size:
            raise PeripheralError(f'{self.name} requires aligned word accesses')
        return offset

    def read(self, address, size):
        offset = self._offset(address, size)
        if offset in self.values:
            return self.values[offset]
        if offset in self.reset:
            return self.reset[offset]
        self.assumed_zero.add(offset)
        return 0

    def write(self, address, size, value):
        offset = self._offset(address, size)
        if offset in self.read_only:
            raise PeripheralError(f'{self.name} register {address:#x} is read-only')
        self.values[offset] = value
        self.writes += 1

    def snapshot(self):
        return copy.deepcopy(dict(values=self.values, assumed_zero=self.assumed_zero, writes=self.writes))

    def restore(self, state):
        s = copy.deepcopy(state)
        self.values, self.assumed_zero, self.writes = s['values'], s['assumed_zero'], s['writes']

    def report(self):
        return dict(base=hex(self.base), writes=self.writes,
                    registers={hex(self.base + k): hex(v) for k, v in sorted(self.values.items())},
                    assumed_zero=[hex(self.base + k) for k in sorted(self.assumed_zero)],
                    reset={hex(self.base + k): hex(v) for k, v in sorted(self.reset.items())})
