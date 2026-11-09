#!/usr/bin/env bash
# Linux/macOS setup: Python deps into python_deps/ and firmware extraction.
# The OTA package itself is not in git; copy stock-2.3.98.bin into
# n66gl-2.3.98/ first (see README.md, "Firmware package").
set -euo pipefail
cd "$(dirname "$0")"

if ! python3 -c 'import sys; sys.path.insert(0, "python_deps"); import unicorn, capstone' 2>/dev/null; then
    python3 -m pip install --quiet --target python_deps -r requirements.txt
fi

if [ ! -f n66gl-2.3.98/extracted/vela_ap.bin ]; then
    if [ ! -f n66gl-2.3.98/stock-2.3.98.bin ]; then
        echo "Missing n66gl-2.3.98/stock-2.3.98.bin (Mi Fitness OTA 2.3.98, md5 feff6a92d0d6c78226e376555475cd0d)" >&2
        exit 1
    fi
    # MD5 from the device log; ZipFile.read checks each entry CRC. Unlike
    # inspect_firmware.py this does not regenerate the tracked inventories.
    python3 -c '
import hashlib, pathlib, zipfile
root = pathlib.Path("n66gl-2.3.98")
package = root / "stock-2.3.98.bin"
if hashlib.md5(package.read_bytes()).hexdigest() != "feff6a92d0d6c78226e376555475cd0d":
    raise SystemExit("stock-2.3.98.bin does not match the expected MD5")
(root / "extracted").mkdir(exist_ok=True)
with zipfile.ZipFile(package) as archive:
    for name in archive.namelist():
        (root / "extracted" / name).write_bytes(archive.read(name))
'
fi
