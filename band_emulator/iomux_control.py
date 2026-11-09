"""BES pin configuration state, with explicit experimental reset assumptions.

See references/README.md. This models register configuration, not electrical
pins, GPIO input/output, serial transfers, interrupts or connected devices.
"""
from system_control import PeripheralError


class IomuxControl:
    BASE = 0x40086000
    SELECTORS = (0x04, 0x08, 0x0c, 0x10, 0x7c, 0x80, 0x84, 0x88, 0x8c, 0x90)
    PULLS = (0x2c, 0x30, 0x94, 0x98, 0x9c)
    MASKS = dict.fromkeys(SELECTORS + PULLS, 0xffffffff) | {0x44: 0x6, 0xac: 0x3c00, 0xc4: 0}

    def __init__(self):
        self.values = dict.fromkeys(self.MASKS, 0)

    def _offset(self, address, size):
        offset = address - self.BASE
        if size != 4 or address % 4 or offset not in self.values:
            raise PeripheralError(f'Unimplemented IOMUX access at {address:#x}, width {size}')
        return offset

    def read(self, address, size):
        return self.values[self._offset(address, size)]

    def write(self, address, size, value):
        offset = self._offset(address, size)
        if value < 0 or value & ~self.MASKS[offset]:
            raise PeripheralError(f'Unsupported IOMUX bits at {address:#x}: {value:#x}')
        self.values[offset] = value

    def pin(self, number):
        if not 0 <= number < 80:
            raise ValueError('Pin number must be 0..79')
        group, bit = divmod(number, 8)
        selector = (self.values[self.SELECTORS[group]] >> (bit * 4)) & 15
        if number < 64:
            up, down, shift = (0x2c, 0x30, number) if number < 32 else (0x94, 0x98, number - 32)
        else:
            up = down = 0x9c
            shift = number - 64
        pull_up = bool(self.values[up] & (1 << shift))
        pull_down = bool(self.values[down] & (1 << (shift + (16 if number >= 64 else 0))))
        return dict(pin=f'P{group}_{bit}', selector=selector,
                    pull_up=pull_up, pull_down=pull_down)

    def snapshot(self):
        return dict(self.values)

    def restore(self, state):
        self.values = dict(state)

    def report(self):
        return dict(registers={hex(self.BASE + offset): hex(value) for offset, value in self.values.items()},
                    pins=[self.pin(i) for i in range(80)], limitations=[
                        'Registers start at zero for this experiment; physical reset values are unverified.',
                        'Selectors are numeric routes, not a verified pin-function table for this bracelet.',
                        '0xac bits 10..13 retain observed firmware values; meaning differs from the reference SDK.',
                        '0x44 models only bits 1 and 2 cleared by stock board setup; routing meaning unverified.',
                        '0xc4 supports only the observed zero write; interrupt behavior is not modeled.',
                        'No electrical pins, GPIO levels, clocks, serial transfers or connected peripherals.',
                    ])
