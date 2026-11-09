# Xiaomi Smart Band 9 emulator

Runs the stock **2.3.98 Global** firmware (`vela_ap.bin`, NuttX/Vela on a BES
Cortex-M33) on Unicorn and controls it from a browser. The project and its
documentation (in Russian) live in [`band_emulator/`](band_emulator/README.md).
The plan to reach a visible, interactive device screen is in [ROADMAP.md](ROADMAP.md).

## Firmware package

The firmware is **not in git**: this repository is public and the image belongs
to Xiaomi. Copy the OTA package to `band_emulator/n66gl-2.3.98/stock-2.3.98.bin`
(43 410 024 bytes, MD5 `feff6a92d0d6c78226e376555475cd0d`).

## Run (Linux/macOS)

```sh
band_emulator/setup.sh                 # deps into python_deps/, unpack firmware
cd band_emulator
python3 -m unittest discover -s tests  # 96 tests
python3 web_server.py --port 8765      # prints http://…/#key=… links
```

Open the printed link (it carries the access key) and press **▶ Запуск**.
On Windows the original `.bat` launchers described in `band_emulator/` apply.
