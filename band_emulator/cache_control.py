"""Experimental BES cache-control model with coherent, uncached backing memory.

Addresses and startup operations come from the stock image. Register meanings
are corroborated by the BES HAL published in atc1441's research repository.
See references/README.md. This does not model physical cache lines or timing.
"""
from system_control import PeripheralError


class CacheControl:
    BASES = (0x07ffa000, 0x07ffc000)
    # Only operations used by the observed startup are supported.
    CONFIG = {0x00: 0x3, 0x08: 0xa}  # Write-back plus revision-dependent no-write-allocate.
    COMMANDS = {0x04: 'invalidate_all', 0x38: 'clean_invalidate_all'}

    def __init__(self, base):
        if base not in self.BASES:
            raise ValueError('Unknown cache controller base')
        self.base = base
        self.values = {offset: 0 for offset in self.CONFIG}
        self.commands = {name: 0 for name in self.COMMANDS.values()}

    def _offset(self, address, size):
        if size != 4 or address % 4 or not self.base <= address < self.base + 4096:
            raise PeripheralError('Cache control requires aligned word accesses')
        return address - self.base

    def read(self, address, size):
        offset = self._offset(address, size)
        if offset in self.CONFIG:
            return self.values[offset]
        raise PeripheralError(f'Unimplemented cache read at {address:#x}')

    def write(self, address, size, value):
        offset = self._offset(address, size)
        if offset in self.CONFIG:
            if value < 0 or value & ~self.CONFIG[offset]:
                raise PeripheralError('Unsupported cache configuration bits')
            self.values[offset] = value
        elif offset in self.COMMANDS and value == 1:
            # Memory is immediately coherent: no dirty/invalid lines to process.
            # A command is an event, not a readable/sticky storage register.
            self.commands[self.COMMANDS[offset]] += 1
        else:
            raise PeripheralError(f'Unimplemented cache write at {address:#x}: {value:#x}')

    def snapshot(self):
        return dict(base=self.base, values=dict(self.values), commands=dict(self.commands))

    def restore(self, state):
        if state['base'] != self.base:
            raise ValueError('Cache snapshot base mismatch')
        self.values, self.commands = dict(state['values']), dict(state['commands'])

    def report(self):
        return dict(base=hex(self.base), registers={hex(k): hex(v) for k, v in self.values.items()},
                    commands=dict(self.commands), limitations=[
                        'Experimental cold start with zeroed control registers.',
                        'Coherent backing memory; no cache lines, dirty data, bus effects or timing.',
                        'Only observed configuration and whole-cache commands are implemented.',
                        'BES-family layout corroborates firmware analysis; chip identity/variant not proven.',
                    ])
