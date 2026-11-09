"""Disassemble a small startup sample; does not execute firmware."""
from pathlib import Path
import json
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'python_deps'))
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS

root = HERE / 'n66gl-2.3.98'
data = (root / 'extracted/vela_ap.bin').read_bytes()
decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
lines = [
    '; Static disassembly only; addresses are FILE OFFSETS, not validated runtime addresses.',
    '; Offset 0x10 follows the 16-byte image header. No code was executed.',
]
for instruction in decoder.disasm(data[0x10:0x60], 0x10):
    lines.append(f'{instruction.address:08x}: {instruction.bytes.hex():10} '
                 f'{instruction.mnemonic:8} {instruction.op_str}')
(root / 'startup.thumb.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
strings = json.loads((root / 'vela_ap.bin.strings.json').read_text())
selected = [(offset, text) for offset, text in strings if 'weather' in text.lower()]
inventory = json.loads((root / 'romfs-inventory.json').read_text())
weather = [entry for entry in inventory['app.bin']
           if entry['path'].startswith('/weather/') and entry['type'] == 2]
(root / 'weather-index.json').write_text(
    json.dumps({'strings': selected, 'resources': weather}, indent=2) + '\n', encoding='utf-8')
print(f'Saved startup disassembly, {len(selected)} weather strings, {len(weather)} resource entries.')
