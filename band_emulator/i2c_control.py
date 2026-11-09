"""DesignWare APB I2C master (BES hal_i2c, atc1441 reg_i2cip.h) with devices.

Transfers complete instantly when IC_DATA_CMD is written. The HAL marks the
last byte with STOP and direction changes with RESTART, so each command can
be processed on write. An address with no attached device NACKs: TX_ABRT with
ABRT_7B_ADDR_NOACK, FIFO flushed until the abort is cleared, STOP_DET.
Every transaction is logged, which is how the firmware's expected chips are
discovered. No bus timing, clock stretching, DMA or slave mode.
"""
import copy
from system_control import PeripheralError

RX_UNDER, RX_OVER, RX_FULL, TX_OVER, TX_EMPTY, RD_REQ, TX_ABRT, RX_DONE, \
    ACTIVITY, STOP_DET, START_DET, GEN_CALL = (1 << n for n in range(12))
CLEARABLE = {0x44: RX_UNDER, 0x48: RX_OVER, 0x4c: TX_OVER, 0x50: RD_REQ, 0x54: TX_ABRT,
             0x58: RX_DONE, 0x5c: ACTIVITY, 0x60: STOP_DET, 0x64: START_DET, 0x68: GEN_CALL}
ALL_CLEARABLE = sum(CLEARABLE.values())
FIFO = 16


class I2cDevice:
    """Override in device models. start() returns False to NACK the address."""
    def start(self, read):
        return True

    def write(self, byte):
        return True

    def read(self):
        return 0xff

    def stop(self):
        pass

    def snapshot(self):
        return {}

    def restore(self, state):
        pass


class I2cController:
    def __init__(self, name, base, irq, devices=None):
        self.name, self.base, self.irq = name, base, irq
        self.devices = devices if devices is not None else {}  # 7-bit addr -> I2cDevice
        self.regs = {0x00: 0x7f, 0x30: 0x8ff}  # DW reset IC_CON, IC_INTR_MASK
        self.raw = 0
        self.abort_source = 0
        self.rx = []
        self.current = None  # dict(addr, read, data) of the open transaction
        self.log = []
        self.transactions = 0

    @property
    def enabled(self):
        return bool(self.regs.get(0x6c, 0) & 1)

    def _offset(self, address, size):
        offset = address - self.base
        if size != 4 or address % 4 or not 0 <= offset < 0x100:
            raise PeripheralError(f'{self.name} requires aligned word accesses')
        return offset

    def _raw(self):
        raw = self.raw
        if self.enabled:
            raw |= TX_EMPTY  # TX FIFO drains instantly, so it is always at/below TX_TL.
            if len(self.rx) > self.regs.get(0x38, 0):
                raw |= RX_FULL
        return raw

    def irq_asserted(self):
        return self.enabled and bool(self._raw() & self.regs[0x30])

    def read(self, address, size):
        offset = self._offset(address, size)
        if offset == 0x10:
            if not self.rx:
                self.raw |= RX_UNDER
                return 0
            return self.rx.pop(0)
        if offset == 0x2c:
            return self._raw() & self.regs[0x30]
        if offset == 0x34:
            return self._raw()
        if offset == 0x40:
            value = self._raw() & self.regs[0x30]
            self.raw &= ~ALL_CLEARABLE
            self.abort_source = 0
            return value
        if offset in CLEARABLE:
            bit = CLEARABLE[offset]
            value = int(bool(self.raw & bit))
            self.raw &= ~bit
            if bit == TX_ABRT:
                self.abort_source = 0
            return value
        if offset == 0x70:  # IC_STATUS: TFNF | TFE | RFNE | RFF
            return 0x6 | (8 if self.rx else 0) | (0x10 if len(self.rx) >= FIFO else 0)
        if offset == 0x74:
            return 0
        if offset == 0x78:
            return len(self.rx)
        if offset == 0x80:
            return self.abort_source
        if offset == 0x9c:
            return int(self.enabled)
        if offset == 0xf4:  # COMP_PARAM_1: 16-deep FIFOs, 32-bit APB
            return ((FIFO - 1) << 16) | ((FIFO - 1) << 8) | 0x6e
        if offset == 0xfc:
            return 0x44570140  # DW_apb_i2c component type
        return self.regs.get(offset, 0)

    def write(self, address, size, value):
        offset = self._offset(address, size)
        if offset == 0x10:
            return self._command(value)
        if offset == 0x6c:
            if value & 2 and self.current:  # ABORT: master abort with STOP.
                self._abort(1 << 16)
            if not value & 1:
                self._finish(stopped=False)
                self.rx.clear()
                self.raw = 0
            self.regs[0x6c] = value & 1
            return
        if offset in (0x2c, 0x34, 0x70, 0x74, 0x78, 0x80, 0x9c) or offset in CLEARABLE or offset == 0x40:
            return  # read-only; DW ignores writes
        self.regs[offset] = value

    def _command(self, value):
        if not self.enabled:
            raise PeripheralError(f'{self.name} DATA_CMD written while disabled')
        if self.raw & TX_ABRT:
            return  # DW holds the TX FIFO flushed until the abort is cleared.
        read, stop, restart = bool(value & 0x100), bool(value & 0x200), bool(value & 0x400)
        addr = self.regs.get(0x04, 0) & 0x3ff
        if self.current is None or restart or self.current['read'] != read:
            if self.current is None:
                self.raw |= START_DET | ACTIVITY
            device = self.devices.get(addr)
            if self.current is None:
                self.current = dict(addr=addr, segments=[])
            self.current['read'] = read
            self.current['segments'].append(['r' if read else 'w', bytearray()])
            if device is None or not device.start(read):
                return self._abort(1)  # ABRT_7B_ADDR_NOACK
        device = self.devices[addr]
        segment = self.current['segments'][-1][1]
        if read:
            byte = device.read() & 0xff
            segment.append(byte)
            if len(self.rx) >= FIFO:
                self.raw |= RX_OVER
            else:
                self.rx.append(byte)
        else:
            segment.append(value & 0xff)
            if not device.write(value & 0xff):
                return self._abort(1 << 3)  # ABRT_TXDATA_NOACK
        if stop:
            self._finish(stopped=True)

    def _abort(self, source):
        self.abort_source |= source
        self.raw |= TX_ABRT
        self._finish(stopped=True, result='nack')

    def _finish(self, stopped, result='ack'):
        if self.current is None:
            return
        device = self.devices.get(self.current['addr'])
        if device and result == 'ack':
            device.stop()
        if stopped:
            self.raw |= STOP_DET
        self.transactions += 1
        self.log.append(dict(addr=hex(self.current['addr']), result=result,
                             data=' '.join(f"{k}:{bytes(d).hex()}" for k, d in self.current['segments'])))
        del self.log[:-32]
        self.current = None

    def snapshot(self):
        return copy.deepcopy(dict(regs=self.regs, raw=self.raw, abort=self.abort_source, rx=self.rx,
                                  current=self.current, log=self.log, transactions=self.transactions,
                                  devices={a: d.snapshot() for a, d in self.devices.items()}))

    def restore(self, state):
        s = copy.deepcopy(state)
        self.regs, self.raw, self.abort_source, self.rx = s['regs'], s['raw'], s['abort'], s['rx']
        self.current, self.log, self.transactions = s['current'], s['log'], s['transactions']
        for addr, device in self.devices.items():
            device.restore(s['devices'].get(addr, {}))

    def report(self):
        return dict(base=hex(self.base), irq=self.irq, enabled=self.enabled, transactions=self.transactions,
                    devices=sorted(hex(a) for a in self.devices), recent=self.log[-12:])
