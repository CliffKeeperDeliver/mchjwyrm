"""Instruction-clock one-shot channel used by the stock OS timer driver."""
from timer_control import TimerBank
from system_control import PeripheralError


class BootTimers(TimerBank):
    SECONDARY = 0x40084020
    IRQ = 46  # Literal written by stock timer initialization at 0x0c50165c.

    def __init__(self):
        super().__init__()
        self.oneshot = dict(load=0xffffffff, value=0xffffffff, control=0, ris=0)

    def _register(self, address, size):
        if self.SECONDARY <= address < self.SECONDARY + 0x20:
            offset = address - self.SECONDARY
            if size != 4 or offset not in (0, 4, 8, 12, 16, 20):
                raise PeripheralError('Unsupported OS timer register')
            return self.oneshot, offset
        return super()._register(address, size)

    def read(self, address, size):
        channel, offset = self._register(address, size)
        if channel is self.oneshot and offset == 20:
            return int(bool(channel['ris'] and channel['control'] & 0x20))
        return super().read(address, size)

    def write(self, address, size, value):
        channel, offset = self._register(address, size)
        if channel is self.oneshot and offset == 8:
            if value not in (0, 2, 3, 0x23, 0x83, 0xa3):
                raise PeripheralError('OS timer supports only 32-bit one-shot, no prescaler')
            channel['control'] = value
            return
        super().write(address, size, value)

    def advance(self, ticks=1):
        super().advance(ticks)
        c = self.oneshot
        if c['control'] & 0x80:
            if ticks >= (c['value'] or 1 << 32):
                c['value'] = 0
                c['ris'] = 1
                c['control'] &= ~0x80
            else:
                c['value'] -= ticks

    def irq_asserted(self):
        return bool(self.oneshot['ris'] and self.oneshot['control'] & 0x20)

    def snapshot(self):
        return dict(super().snapshot(), oneshot=dict(self.oneshot))

    def restore(self, state):
        super().restore(state)
        self.oneshot = dict(state['oneshot'])

    def report(self):
        r = super().report()
        r['os_timer'] = dict(base=hex(self.SECONDARY), irq=self.IRQ,
                             **{k: hex(v) for k, v in self.oneshot.items()})
        r['limitations'] = ['Instruction ticks, not physical time. Channel 0 free-running; channel 1 one-shot only.']
        return r
