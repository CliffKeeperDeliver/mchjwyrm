# Roadmap: Band 9 firmware visible in the browser

Each step ends with something runnable and checked; the next one starts only
after review. Boundary at the start: the boot reaches `btdrv_common_init` and
stops at the internal SPI write `0x40009000` (PC `0x0026039e`, ~1.86 M
instructions, tasks Idle/hpwork/lpwork/AppBringUp).

1. **Foundation in the repository** — *done.* Project imported, firmware
   verified against `provenance.json` and kept out of git, Linux setup script,
   managed browser preview, all 96 tests passing. Fixed lost 401/403 replies
   (unread POST body → TCP reset) and HTTPS-proxy origin rejection.
2. **Bluetooth bring-up boundary** — *done.* ISPI (PL022 + BES analog
   interface) modeled; the stock early bring-up now opens it, passes the PMU
   CHIP_ID check (`0x153`) and configures PMU/RF. The BT controller start is
   replaced (`bt_controller_absent`: the BT CPU is not emulated). Board init
   returns (`0x0c51ac86`), `bes_main` starts; next stop `0x40160404`.
3. **Remaining board bring-up** — *done.* Execution runs in count-limited
   chunks (~7 M instructions/s, ~25× faster) and masked IRQs are taken right
   after the unmask instruction (fixes idle-loop IRQ starvation that made the
   result depend on run slicing). PSRAM controller and PHY, MEMSC
   hardware semaphores, six DesignWare I2C buses (absent devices NACK) and the
   second flash controller with a SPI-NAND chip from the firmware's own table
   (2 Gbit, 2 KiB pages, matching its partition table). The stock NAND driver
   identifies and configures the chip. Boot reaches display init: LCD
   controller `0x40200000` at ~3.67 M instructions (0.5 s).

Remaining (order follows what the boot needs first):

4. **Display controller and panel.** Model the LCDC (`0x40200000`) and the
   DSI path to the RM690B0 AMOLED (panel ID reads, init commands), find where
   frames are flushed and capture them as images.
5. **OS services and storage.** Continue the boot to NSH: erased main-NOR
   config sectors (`0x2c7f8000`), then `rc.sysinit` — a yaffs image on the
   emulated NAND holding the OTA ROMFS files (`app.bin`, `font.bin`,
   `watchface.bin`, …) for `/system`, and an empty `/data`. Then `rcS` starts
   the `miwear` services and UI.
6. **Screen in the browser.** Frame endpoint plus a device view (canvas in a
   Band 9 outline) in the existing web UI.
7. **Input.** Touch controller (Hynitron CST9217 or Zinitix ZTW623 on I2C,
   per `/vendor/touch/*_fw.bin`) and the button GPIO, driven from mouse and
   touch events in the browser.
8. **Real time and speed.** Pace timers/RTC to wall-clock time, fast-forward
   idle, and accelerate hot paths (GPU/DMA2D natively) for a usable frame rate.
9. **Optional.** Synthetic sensor data (heart rate, steps); Bluetooth/phone
   sync stays out of scope.

Steps 4 and 5 are open-ended reverse engineering: their size depends on how
many peripherals the firmware touches before the first frame.
