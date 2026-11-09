"""Minimal BES AON configuration; identity is an explicit experimental input."""
from system_control import PeripheralError


class AonControl:
    BASE = 0x40080000

    def __init__(self, chip_id_register):
        if not 0 <= chip_id_register <= 0xffffffff:
            raise ValueError('CHIP_ID must be an unsigned 32-bit experimental value')
        self.chip_id = chip_id_register
        self.ram_cfg = 0
        self.clock_out = 0

    def _offset(self, address, size):
        if size != 4 or address % 4:
            raise PeripheralError('AON control requires aligned word accesses')
        return address - self.BASE

    def read(self, address, size):
        offset = self._offset(address, size)
        if offset == 0:
            return self.chip_id
        if offset == 0x5c:
            return self.ram_cfg
        if offset == 0x1c:
            return self.clock_out
        raise PeripheralError(f'Unimplemented AON read at {address:#x}')

    def write(self, address, size, value):
        offset = self._offset(address, size)
        if offset == 0:
            raise PeripheralError('AON CHIP_ID is read-only')
        if offset == 0x5c and 0 <= value <= 7:
            self.ram_cfg = value
            return
        if offset == 0x1c and value in (0, 0x40000):
            self.clock_out = value
            return
        raise PeripheralError(f'Unimplemented AON write at {address:#x}: {value:#x}')

    def snapshot(self):
        return dict(chip_id=self.chip_id, ram_cfg=self.ram_cfg, clock_out=self.clock_out)

    def restore(self, state):
        if state['chip_id'] != self.chip_id:
            raise ValueError('AON identity does not match snapshot')
        self.ram_cfg = state['ram_cfg']
        self.clock_out = state['clock_out']

    def report(self):
        return dict(chip_id_register=hex(self.chip_id), revision=self.chip_id >> 16,
                    ram_cfg=hex(self.ram_cfg), clock_out=hex(self.clock_out), identity_verified=False, limitations=[
                        'CHIP_ID is supplied explicitly for branch exploration, not read from the bracelet.',
                        'RAM_CFG starts at zero and only observed EMA bits 0..2 are supported.',
                        'CLK_OUT starts at zero; only observed timer-source bit 18 is supported as configuration.',
                        'No electrical RAM effects, clock signals, resets, timers or other AON registers.',
                    ])
