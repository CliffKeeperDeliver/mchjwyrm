"""Logged register-file model for BES control blocks used by the boot runner.

Writes are stored; reads return the stored value. A register that was never
written reads its RESET entry, or zero. Every zero read of a never-written
register is recorded (`assumed_zero`) so a report shows exactly which inputs
were invented. No clock, power or reset side effects are modeled.
"""
import copy
from system_control import PeripheralError


class RegisterFile:
    def __init__(self, name, base, size=0x1000, reset=None, read_only=(), memsc=None):
        self.name, self.base, self.size = name, base, size
        self.reset = dict(reset or {})
        self.read_only = set(read_only)
        self.values = {}
        self.assumed_zero = set()
        self.writes = 0
        # Hardware semaphores (hal_memsc.c): (first MEMSC offset, STATUS offset).
        # Reading MEMSC[i] takes lock i (1 = acquired, 0 = busy), writing 1
        # releases it, STATUS bit i is set while lock i is free.
        self.memsc = memsc
        self.locks = 0

    def _semaphore(self, offset):
        if self.memsc and self.memsc[0] <= offset < self.memsc[0] + 16:
            return (offset - self.memsc[0]) // 4
        return None

    def _offset(self, address, size):
        offset = address - self.base
        if size != 4 or address % 4 or not 0 <= offset < self.size:
            raise PeripheralError(f'{self.name} requires aligned word accesses')
        return offset

    def read(self, address, size):
        offset = self._offset(address, size)
        lock = self._semaphore(offset)
        if lock is not None:
            taken = self.locks >> lock & 1
            self.locks |= 1 << lock
            return 0 if taken else 1
        if self.memsc and offset == self.memsc[1]:
            return ~self.locks & 0xf
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
        lock = self._semaphore(offset)
        if lock is not None:
            if value & 1:
                self.locks &= ~(1 << lock)
            return
        self.values[offset] = value
        self.writes += 1

    def snapshot(self):
        return copy.deepcopy(dict(values=self.values, assumed_zero=self.assumed_zero, writes=self.writes,
                                  locks=self.locks))

    def restore(self, state):
        s = copy.deepcopy(state)
        self.values, self.assumed_zero, self.writes = s['values'], s['assumed_zero'], s['writes']
        self.locks = s.get('locks', 0)

    def report(self):
        return dict(base=hex(self.base), writes=self.writes,
                    registers={hex(self.base + k): hex(v) for k, v in sorted(self.values.items())},
                    assumed_zero=[hex(self.base + k) for k in sorted(self.assumed_zero)],
                    reset={hex(self.base + k): hex(v) for k, v in sorted(self.reset.items())})
