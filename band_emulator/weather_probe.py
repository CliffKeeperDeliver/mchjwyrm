"""Run original Band 9 temperature-selection code with controlled RAM inputs."""
import argparse
import json
from pathlib import Path
import struct

from emulator import Emulator, load_profile

HERE = Path(__file__).resolve().parent
PROFILE = HERE / 'profiles/n66gl_2.3.98.json'
# Inferred from caller 0x224a4c and verified by executing the actual callees.
FUNCTIONS = {'minimum': 0x22d834, 'maximum': 0x22d85e}
CONVERT_OFFSET = 0x22d790
DATA_ADDRESS = 0x20001000  # Experiment buffer, not a discovered live pointer.
UNIT_FLAG = 0x20097320  # Literal 0x20097318 at 0x22d7d0, plus ldrb offset 8.
EXPECTED_IMAGE = '7c0115d92f7aa5d22dcb3f9a1ff0614904d736609d2d29eebc32f9ab3f7ebab1'


def signed32(value):
    value = int(value, 16) if isinstance(value, str) else value
    return value - 2**32 if value & 0x80000000 else value


def run_temperature(kind, value_a, value_b, *, fahrenheit=False,
                    extended=True, fallback=0, trace_limit=128):
    """Read actual returned R0; no Python temperature calculation or guest stubs."""
    if kind not in FUNCTIONS:
        raise ValueError('Unknown selector')
    if any(not -32768 <= v <= 32767 for v in (value_a, value_b, fallback)):
        raise ValueError('Input fields are signed 16-bit integers')
    profile = load_profile(PROFILE)
    if profile['sha256'] != EXPECTED_IMAGE:
        raise ValueError('Weather offsets have only been verified for this exact image')
    emu = Emulator(profile, trace_limit=trace_limit)
    data = bytearray(0x188)
    struct.pack_into('<h', data, 0xca, fallback)
    struct.pack_into('<H', data, 0x17c, 1 if extended else 0)
    struct.pack_into('<hh', data, 0x182, value_a, value_b)
    emu.write_ram(DATA_ADDRESS, data)
    emu.write_ram(UNIT_FLAG, bytes([bool(fahrenheit)]))
    result = emu.run(FUNCTIONS[kind], {'r0': DATA_ADDRESS}, max_instructions=5000)
    result['experiment'] = dict(
        name='stock_weather_temperature_selection', selector=kind,
        input_fields={'0x182': value_a, '0x184': value_b, '0x17c': int(extended), '0xca': fallback},
        unit='F' if fahrenheit else 'C', unit_flag_address=hex(UNIT_FLAG),
        mocked_functions=[], mocked_hardware=[],
        note='Controlled RAM inputs; this does not establish forecast time intervals or live sensor data.',
    )
    result['temperature'] = signed32(result['registers']['r0']) if result['status'] == 'returned' else None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--a', type=int, default=-3, help='signed temperature field at +0x182')
    parser.add_argument('--b', type=int, default=3, help='signed temperature field at +0x184')
    parser.add_argument('--fallback', type=int, default=-2, help='field at +0xca')
    parser.add_argument('--fallback-mode', action='store_true', help='set field +0x17c to zero')
    parser.add_argument('--units', choices=['C', 'F', 'both'], default='both')
    parser.add_argument('--output', type=Path, default=HERE / 'results/weather.json')
    args = parser.parse_args()
    try:
        runs = []
        for unit in (['C', 'F'] if args.units == 'both' else [args.units]):
            for kind in FUNCTIONS:
                result = run_temperature(kind, args.a, args.b, fahrenheit=unit == 'F',
                                         extended=not args.fallback_mode, fallback=args.fallback)
                runs.append(result)
                print(f"{unit} {kind}: {result['temperature']} ({result['status']}, {result['instructions']} instructions)")
        report = dict(source='original vela_ap.bin, verified SHA-256', full_device_emulation=False, runs=runs)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('Report:', args.output.resolve())
        return 0 if all(r['status'] == 'returned' for r in runs) else 2
    except (ValueError, OSError) as error:
        print('Error:', error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
