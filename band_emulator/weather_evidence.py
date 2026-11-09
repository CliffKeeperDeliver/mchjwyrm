"""Reproduce the static evidence tying the executed helpers to weather code."""
import json
import struct

from emulator import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_MCLASS
from weather_probe import HERE, PROFILE, EXPECTED_IMAGE, load_profile, Emulator


def main():
    profile = load_profile(PROFILE)
    assert profile['sha256'] == EXPECTED_IMAGE
    image = Emulator(profile).image  # Verifies actual image SHA-256.
    target = b'app_weather_get_local_temperature\0'
    string_offset = image.index(target)
    pointer = struct.pack('<I', 0x2c100000 + string_offset)
    pointer_offsets = []
    at = 0
    while (at := image.find(pointer, at)) >= 0:
        pointer_offsets.append(at)
        at += 1
    assert 0x224b54 in pointer_offsets
    decoder = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_MCLASS)
    regions = [
        ('weather caller: reference to diagnostic function name and calls to selectors', 0x224a4c, 0x224b42),
        ('temperature conversion; stops before literal pool', 0x22d790, 0x22d7d0),
        ('minimum selector', 0x22d834, 0x22d85e),
        ('maximum selector', 0x22d85e, 0x22d888),
        ('fallback conversion wrapper', 0x3db100, 0x3db10c),
    ]
    sections = []
    for name, start, end in regions:
        instructions = [dict(offset=hex(i.address - profile['image_base']), address=hex(i.address),
                             bytes=i.bytes.hex(), instruction=f'{i.mnemonic} {i.op_str}'.strip())
                        for i in decoder.disasm(image[start:end], profile['image_base'] + start)]
        assert sum(len(bytes.fromhex(i['bytes'])) for i in instructions) == end - start
        sections.append(dict(name=name, start_offset=hex(start), end_offset=hex(end), instructions=instructions))
    result = dict(
        image_sha256=EXPECTED_IMAGE,
        diagnostic_string=dict(text=target[:-1].decode(), file_offset=hex(string_offset),
                               data_pointer=hex(0x2c100000 + string_offset),
                               pointer_locations=[hex(v) for v in pointer_offsets]),
        note='The 0x2c100000 data-address mapping is inferred from matching pointers; not a complete SoC map.',
        sections=sections,
    )
    output = HERE / 'results/weather-evidence.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    text = []
    for section in sections:
        text.append('; ' + section['name'])
        text.extend(f"{i['address']} [{i['offset']}]: {i['instruction']}" for i in section['instructions'])
        text.append('')
    (HERE / 'results/weather-functions.thumb.txt').write_text('\n'.join(text), encoding='utf-8')
    print('Saved weather diagnostic reference and five complete disassembly ranges.')


if __name__ == '__main__':
    main()
