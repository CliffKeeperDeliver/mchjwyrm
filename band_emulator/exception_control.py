"""Single-domain Cortex-M exception frames for stock SVC/timer dispatch.

Eager FP stacking. No nesting, security transitions, tail chaining or fault
escalation. The firmware's vector handler and scheduler execute on the CPU.
"""
import struct
from emulator import arm


class ExceptionFrames:
    CORE_REGS = tuple(getattr(arm, 'UC_ARM_REG_' + n)
                      for n in ('R0', 'R1', 'R2', 'R3', 'R12', 'LR', 'PC', 'XPSR'))

    def __init__(self, machine):
        self.machine = machine
        self.active = None
        self.events = []
        self.total_events = 0

    def _event(self, **event):
        self.total_events += 1
        self.events.append(event)
        self.events[:] = self.events[-128:]

    def enter(self, exception, return_pc):
        m, cpu = self.machine, self.machine.cpu
        if self.active is not None or cpu.reg_read(arm.UC_ARM_REG_IPSR):
            raise ValueError('Nested exception entry is not supported')
        vector = m.core.values[0xd08] + exception * 4
        handler = int.from_bytes(m.read_memory(vector, 4), 'little')
        if not handler & 1 or m.execution_region(handler & ~1) is None:
            raise ValueError('Exception vector is not a mapped Thumb handler')
        control = cpu.reg_read(arm.UC_ARM_REG_CONTROL)
        fp = bool(control & 4)
        spreg = arm.UC_ARM_REG_PSP if control & 2 else arm.UC_ARM_REG_MSP
        sp = cpu.reg_read(spreg)
        size = 104 if fp else 32
        aligned_sp = (sp - size) & ~7
        words = [cpu.reg_read(r) for r in self.CORE_REGS]
        words[6] = return_pc & ~1
        words[7] = (words[7] & ~(1 << 9)) | ((sp & 4) << 7)
        if fp:
            words += [cpu.reg_read(getattr(arm, f'UC_ARM_REG_S{i}')) for i in range(16)]
            words += [cpu.reg_read(arm.UC_ARM_REG_FPSCR), 0]
        m.write_memory(aligned_sp, struct.pack('<' + 'I' * len(words), *words))
        cpu.reg_write(spreg, aligned_sp)
        # IPSR selects handler mode (MSP), independent of thread SPSEL.
        cpu.reg_write(arm.UC_ARM_REG_XPSR, (words[7] & ~0x0600ffff) | exception)
        exc_return = 0xffffffed if control & 2 else 0xffffffe9
        if not fp:
            exc_return |= 0x10
        cpu.reg_write(arm.UC_ARM_REG_LR, exc_return)
        cpu.reg_write(arm.UC_ARM_REG_PC, handler)
        self.active = exception
        if exception >= 16:
            irq = exception - 16
            m.core.pending[irq // 32] &= ~(1 << (irq % 32))
        elif exception == 15:
            m.core.systick['pending'] = False
        self._event(kind='enter', exception=exception, handler=hex(handler),
                    frame=hex(aligned_sp), return_pc=hex(return_pc), fp=fp)

    def leave(self):
        m, cpu = self.machine, self.machine.cpu
        exc_return = cpu.reg_read(arm.UC_ARM_REG_LR)
        if self.active is None or exc_return not in (0xffffffe9, 0xffffffed, 0xfffffff9, 0xfffffffd):
            raise ValueError(f'Unsupported EXC_RETURN {exc_return:#x}')
        fp = not bool(exc_return & 0x10)
        spreg = arm.UC_ARM_REG_PSP if exc_return & 4 else arm.UC_ARM_REG_MSP
        sp = cpu.reg_read(spreg)
        size = 104 if fp else 32
        words = struct.unpack('<' + 'I' * (size // 4), m.read_memory(sp, size))
        if words[7] & 0x1ff or not words[7] & (1 << 24) or m.execution_region(words[6] & ~1) is None:
            raise ValueError('Return frame must select mapped Thumb code in thread mode')
        for reg, value in zip(self.CORE_REGS[:6], words[:6]):
            cpu.reg_write(reg, value)
        if fp:
            for i in range(16):
                cpu.reg_write(getattr(arm, f'UC_ARM_REG_S{i}'), words[8 + i])
            cpu.reg_write(arm.UC_ARM_REG_FPSCR, words[24])
        cpu.reg_write(spreg, sp + size + (4 if words[7] & (1 << 9) else 0))
        control = cpu.reg_read(arm.UC_ARM_REG_CONTROL) & ~6
        cpu.reg_write(arm.UC_ARM_REG_CONTROL, control | (2 if exc_return & 4 else 0) | (4 if fp else 0))
        cpu.reg_write(arm.UC_ARM_REG_XPSR, words[7] & ~(1 << 9))
        cpu.reg_write(arm.UC_ARM_REG_PC, words[6] | 1)
        self._event(kind='return', exception=self.active, frame=hex(sp), pc=hex(words[6]), fp=fp)
        self.active = None

    def pending_irq(self):
        m, cpu = self.machine, self.machine.cpu
        if self.active is not None or not (m.core.systick['pending'] or any(p & e for p, e in zip(m.core.pending, m.core.enabled))):
            return None
        if cpu.reg_read(arm.UC_ARM_REG_PRIMASK) or cpu.reg_read(arm.UC_ARM_REG_FAULTMASK):
            return None
        basepri = cpu.reg_read(arm.UC_ARM_REG_BASEPRI)
        ready = [i for i in m.core.ready_irqs() if not basepri or m.core.priorities[i] < basepri]
        priority = lambda i: m.core.shpr[11] if i == -1 else m.core.priorities[i]
        if m.core.systick['pending'] and (not basepri or m.core.shpr[11] < basepri):
            ready.append(-1)
        return min(ready, key=lambda i: (priority(i), i)) if ready else None
