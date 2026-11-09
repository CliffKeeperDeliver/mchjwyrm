"""Offline inspection only. Never connects to or writes to a wearable."""
import hashlib
import json
from pathlib import Path
import re
import struct
import zipfile

ROOT = Path(__file__).resolve().parent / 'n66gl-2.3.98'
PACKAGE = ROOT / 'stock-2.3.98.bin'
EXPECTED_MD5 = 'feff6a92d0d6c78226e376555475cd0d'


def main():
    package = PACKAGE.read_bytes()
    if hashlib.md5(package).hexdigest() != EXPECTED_MD5:
        raise ValueError('Package does not match the checksum in the device log')
    dest = ROOT / 'extracted'
    dest.mkdir(exist_ok=True)
    entries = []
    with zipfile.ZipFile(PACKAGE) as archive:
        if sum(i.file_size for i in archive.infolist()) > 256 * 1024 * 1024:
            raise ValueError('Unreasonable uncompressed package size')
        for info in archive.infolist():
            if not re.fullmatch(r'[A-Za-z0-9_.-]+', info.filename):
                raise ValueError('Unexpected archive path')
            data = archive.read(info)  # Also checks the ZIP CRC.
            (dest / info.filename).write_bytes(data)
            markers = {}
            for name, magic in [('ELF', b'\x7fELF'), ('squashfs', b'hsqs'),
                                ('romfs', b'-rom1fs-'), ('gzip', b'\x1f\x8b\x08')]:
                hits = [m.start() for m in re.finditer(re.escape(magic), data)]
                if hits:
                    markers[name] = hits[:32]
            entries.append(dict(name=info.filename, size=len(data),
                                sha256=hashlib.sha256(data).hexdigest(),
                                header_hex=data[:64].hex(), signature_candidates=markers))
    (ROOT / 'inventory.json').write_text(json.dumps(entries, indent=2) + '\n', encoding='utf-8')
    for item in entries:
        print(item['name'], item['size'], item['header_hex'][:64], item['signature_candidates'])
    for name in ['version', 'ota.json']:
        print(name, (dest / name).read_text(encoding='utf-8'))
    for name in ['vela_ap.bin', 'vela_ota.bin', 'recovery.bin']:
        data = (dest / name).read_bytes()
        print(name, 'first words:', [hex(v) for v in struct.unpack_from('<32I', data)])
        strings = [(m.start(), m.group().decode('ascii'))
                   for m in re.finditer(rb'[ -~]{8,}', data)]
        selected = [(offset, text) for offset, text in strings
                    if re.search(r'NuttX|BES|best1503|Cortex|weather|Weather|\.c$|\.cpp$|build|gcc|2\.3\.98', text)]
        (ROOT / (name + '.strings.json')).write_text(json.dumps(selected, indent=2) + '\n', encoding='utf-8')
        print('Selected strings:', selected[:25])


if __name__ == '__main__':
    main()
