"""BES flash controller (norflaship v2) with an attached SPI-NAND chip.

Controller (atc1441 reg_norflaship_v2.h / hal_norflaship_v2.c): TX bytes are
pushed to REG_008, a command is issued by writing REG_000. With REG_004
NEW_CMD_EN the command is CMD + BLOCK_SIZE TX bytes, optionally reading
RX_LEN bytes (REG_000 bit 8, bits 13..25); otherwise REG_000 carries CMD and
a 24-bit address. RX bytes are read from REG_010. Transfers are instant.

Chip: standard SPI-NAND command set. Geometry and ID are one entry of the
firmware's own supported-chip table (0x2c6f63e0, entry 3: ID c8 82 c8,
2 KiB pages + 128 B spare, 64 pages/block, 256 MiB), which matches the
stock partition table (nand_system and nand_data, 65536 pages each).
Pages are stored sparsely; unwritten pages read erased (0xff).
"""
import copy
from system_control import PeripheralError

PAGE, SPARE, PAGES_PER_BLOCK, TOTAL = 2048, 128, 64, 256 * 1024 * 1024
PAGES = TOTAL // PAGE
READ_CACHE = {0x03, 0x0b, 0x3b, 0x6b, 0xbb, 0xeb}


class SpiNand:
    ID = bytes.fromhex('c882c8')

    def __init__(self):
        self.pages = {}  # page index -> bytearray(PAGE + SPARE)
        self.cache = bytearray(b'\xff' * (PAGE + SPARE))
        # A0 protection (all unlocked), B0 config (ECC_EN | BUF), C0 status.
        self.features = {0xa0: 0x00, 0xb0: 0x18, 0xc0: 0x00, 0xd0: 0x00}
        self.wel = False
        self.log = []
        self.counts = dict(read=0, program=0, erase=0)

    def page(self, index):
        data = self.pages.get(index)
        return data if data is not None else b'\xff' * (PAGE + SPARE)

    @staticmethod
    def _row(tx):
        if len(tx) < 3:
            raise PeripheralError('SPI-NAND row address needs 3 bytes')
        row = int.from_bytes(tx[:3], 'big')
        if row >= PAGES:
            raise PeripheralError(f'SPI-NAND page {row:#x} outside {PAGES:#x}')
        return row

    def transfer(self, cmd, tx, rx_len):
        rx = b''
        if cmd == 0xff:
            self.wel = False
            self.features[0xc0] = 0
        elif cmd == 0x9f:
            rx = (self.ID * ((rx_len + 2) // 3 + 1))[:rx_len]
        elif cmd == 0x0f:
            rx = bytes([self.features.get(tx[0], 0) if tx else 0]) * rx_len
        elif cmd == 0x1f:
            if len(tx) >= 2 and tx[0] in (0xa0, 0xb0, 0xd0):
                self.features[tx[0]] = tx[1]
        elif cmd == 0x06:
            self.wel = True
        elif cmd == 0x04:
            self.wel = False
        elif cmd == 0x13:
            row = self._row(tx)
            self.cache[:] = self.page(row)
            self.counts['read'] += 1
        elif cmd in READ_CACHE:
            column = int.from_bytes(tx[:2], 'big') & 0xfff if len(tx) >= 2 else 0
            rx = bytes(self.cache[column:column + rx_len]).ljust(rx_len, b'\xff')
        elif cmd in (0x02, 0x32, 0x84, 0x34):
            if cmd in (0x02, 0x32):
                self.cache[:] = b'\xff' * (PAGE + SPARE)
            column = int.from_bytes(tx[:2], 'big') & 0xfff
            data = tx[2:]
            self.cache[column:column + len(data)] = data[:PAGE + SPARE - column]
        elif cmd == 0x10:
            row = self._row(tx)
            if self.wel and not self._locked():
                old = self.page(row)
                self.pages[row] = bytearray(a & b for a, b in zip(old, self.cache))
                self.counts['program'] += 1
            else:
                self.features[0xc0] |= 0x08  # P_FAIL
            self.wel = False
        elif cmd == 0xd8:
            row = self._row(tx)
            if self.wel and not self._locked():
                first = row - row % PAGES_PER_BLOCK
                for index in range(first, first + PAGES_PER_BLOCK):
                    self.pages.pop(index, None)
                self.counts['erase'] += 1
            else:
                self.features[0xc0] |= 0x04  # E_FAIL
            self.wel = False
        else:
            raise PeripheralError(f'Unsupported SPI-NAND command {cmd:#04x}')
        # Status: operations complete instantly (OIP=0), ECC 00 = no errors.
        self.features[0xc0] = (self.features[0xc0] & 0x0c) | (2 if self.wel else 0)
        self.log.append(f'{cmd:02x} tx={bytes(tx[:6]).hex()}{"…" if len(tx) > 6 else ""} rx={len(rx)}')
        del self.log[:-24]
        return rx

    def _locked(self):
        return bool(self.features[0xa0] & 0x38)  # BP0..BP2 block protection

    def snapshot(self):
        return dict(pages={k: bytes(v) for k, v in self.pages.items()}, cache=bytes(self.cache),
                    features=dict(self.features), wel=self.wel, log=list(self.log), counts=dict(self.counts))

    def restore(self, s):
        self.pages = {k: bytearray(v) for k, v in s['pages'].items()}
        self.cache = bytearray(s['cache'])
        self.features, self.wel = dict(s['features']), s['wel']
        self.log, self.counts = list(s['log']), dict(s['counts'])


class FlashController:
    FIFO = 16

    def __init__(self, name, base, chip):
        self.name, self.base, self.chip = name, base, chip
        self.regs = {}
        self.tx = bytearray()
        self.rx = bytearray()
        self.commands = 0

    def _offset(self, address, size):
        offset = address - self.base
        if not 0 <= offset < 0x1000 or address % size or size not in (1, 2, 4):
            raise PeripheralError(f'{self.name}: bad access {address:#x}/{size}')
        if size != 4 and offset not in (0x08, 0x10):
            raise PeripheralError(f'{self.name}: {size}-byte access only on TX/RX FIFO')
        return offset

    def read(self, address, size):
        offset = self._offset(address, size)
        if offset == 0x0c:
            empty_slots = self.FIFO  # TX drains instantly
            count = min(len(self.rx), 31)
            return (empty_slots << 9) | (count << 4) | (0 if self.rx else 8) | 2
        if offset == 0x10:  # one FIFO byte per read, whatever the width (HAL reads 32-bit)
            if not self.rx:
                raise PeripheralError(f'{self.name}: RX FIFO read while empty')
            value = self.rx[0]
            del self.rx[:1]
            return value
        return self.regs.get(offset, 0)

    def write(self, address, size, value):
        offset = self._offset(address, size)
        if offset == 0x08:
            self.tx += value.to_bytes(size, 'little')
            return
        if offset == 0x18:  # FIFO clear
            if value & 2:
                self.tx.clear()
            if value & 1:
                self.rx.clear()
            return
        self.regs[offset] = value
        if offset == 0x00:
            self._command(value)

    def _command(self, value):
        control = self.regs.get(0x04, 0)
        length = (control >> 12) & 0x1fff
        cmd = value & 0xff
        if control & 1:  # NEW_CMD_EN: CMD + TX bytes (+ RX)
            rx_len = (value >> 13) & 0x1fff if value & 0x100 else 0
            tx = bytes(self.tx[:length])
        else:  # legacy: CMD + 24-bit address
            address = (value >> 8) & 0xffffff
            tx = address.to_bytes(3, 'big') + bytes(self.tx[:length])
            rx_len = length if cmd in READ_CACHE else 0
        del self.tx[:length]
        self.commands += 1
        self.rx += self.chip.transfer(cmd, tx, rx_len)

    def snapshot(self):
        return copy.deepcopy(dict(regs=self.regs, tx=bytes(self.tx), rx=bytes(self.rx), commands=self.commands,
                                  chip=self.chip.snapshot()))

    def restore(self, s):
        self.regs, self.commands = dict(s['regs']), s['commands']
        self.tx, self.rx = bytearray(s['tx']), bytearray(s['rx'])
        self.chip.restore(s['chip'])

    def report(self):
        return dict(base=hex(self.base), commands=self.commands, chip_id=self.chip.ID.hex(),
                    geometry=dict(page=PAGE, spare=SPARE, pages_per_block=PAGES_PER_BLOCK, total=TOTAL),
                    written_pages=len(self.chip.pages), operations=dict(self.chip.counts),
                    recent=self.chip.log[-12:],
                    limitations=['Instant transfers; no DMA, timing, ECC errors or bad blocks.',
                                 'Starts fully erased unless an image is loaded.'])
