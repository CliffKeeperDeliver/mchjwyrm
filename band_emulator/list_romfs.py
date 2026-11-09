"""Read ROMFS directories without mounting or executing their contents.

Format reference: https://docs.kernel.org/filesystems/romfs.html
"""
from pathlib import Path
import hashlib
import json
import struct

ROOT = Path(__file__).resolve().parent / 'n66gl-2.3.98'


def inventory(data):
    assert data[:8] == b'-rom1fs-'
    size = struct.unpack_from('>I', data, 8)[0]
    assert 16 <= size <= len(data)
    check = data[:min(size, 512)]
    assert len(check) % 4 == 0
    assert sum(struct.unpack('>' + 'I' * (len(check) // 4), check)) & 0xffffffff == 0
    root = (data.index(b'\0', 16) + 16) & ~15
    rows = []
    seen = set()

    def walk(offset, prefix, depth=0):
        if depth > 64:
            raise ValueError('Directory recursion limit')
        while offset:
            if offset in seen:
                raise ValueError('Repeated directory entry')
            seen.add(offset)
            assert offset % 16 == 0 and offset + 16 <= size
            nxt, spec, length, checksum = struct.unpack_from('>4I', data, offset)
            end = data.index(b'\0', offset + 16, size)
            name = data[offset + 16:end].decode('utf-8')
            start = (end + 16) & ~15
            kind = nxt & 7
            if name not in ('.', '..'):
                assert '/' not in name and '\\' not in name
                path = prefix + '/' + name
                row = dict(path=path, type=kind, size=length, header_offset=offset,
                           data_offset=start, executable=bool(nxt & 8))
                if kind in (2, 3):
                    assert start + length <= size
                    content = data[start:start + length]
                    row['sha256'] = hashlib.sha256(content).hexdigest()
                    row['elf'] = content.startswith(b'\x7fELF')
                rows.append(row)
                if kind == 1:
                    walk(spec, path, depth + 1)
            offset = nxt & ~15

    walk(root, '')
    return rows


def main():
    manifest = json.loads((ROOT / 'extracted/ota.json').read_text())
    for section in manifest['sections']:
        name = section['location_path']
        data = (ROOT / 'extracted' / name).read_bytes()
        assert hashlib.md5(data).hexdigest() == section['md5sum'], name
    print('All 12 section MD5 checks passed')
    result = {}
    for image in sorted((ROOT / 'extracted').glob('*.bin')):
        data = image.read_bytes()
        if data[:8] != b'-rom1fs-':
            continue
        rows = inventory(data)
        result[image.name] = rows
        print(image.name, 'entries:', len(rows), 'ELF files:', sum(r.get('elf', False) for r in rows))
        selected = [r for r in rows if 'weather' in r['path'].lower() or r.get('elf')]
        print('Relevant entries:', selected[:20])
    (ROOT / 'romfs-inventory.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
