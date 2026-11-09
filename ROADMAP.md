# Roadmap: Band 9 firmware visible in the browser

Each step ends with something runnable and checked; the next one starts only
after review. Boundary at the start: the boot reaches `btdrv_common_init` and
stops at the internal SPI write `0x40009000` (PC `0x0026039e`, ~1.86 M
instructions, tasks Idle/hpwork/lpwork/AppBringUp).

1. **Foundation in the repository** — *done.* Project imported, firmware
   verified against `provenance.json` and kept out of git, Linux setup script,
   managed browser preview, all 96 tests passing. Fixed lost 401/403 replies
   (unread POST body → TCP reset) and HTTPS-proxy origin rejection.
2. **Bluetooth bring-up boundary.** Model the internal SPI at `0x40009000`
   enough for the chip-revision read of register `0x3000` (or a narrow,
   documented HLE of the BT controller), until `AppBringUp` returns from
   board init (`0x0c51ac86`).
3. **Remaining board bring-up.** Continue `AppBringUp` through the next unknown
   peripherals (I2C/PMU, RTC, watchdog, sensors), same rule as now: stop on
   unknown hardware, identify it, model it narrowly, add a test. Goal: NuttX
   starts the Vela init/application services.
4. **Storage.** Present the OTA ROMFS images (`app`, `font`, `i18n`,
   `watchface`, `system`, `misc`, `vendor`, `quickapp`) where the firmware's
   partition table expects them, plus an empty writable `/data` partition, so
   the UI can load fonts, images and watchfaces.
5. **Display pipeline.** Find the LCD/panel driver and the frame flush path
   (LVGL → DMA2D/GPU → panel, expected 192×490 AMOLED), model the panel
   controller and capture each flushed frame.
6. **Screen in the browser.** Frame endpoint plus a device view (canvas in a
   Band 9 outline) in the existing web UI.
7. **Input.** Touch controller and the wake/button GPIO driven from mouse and
   touch events in the browser.
8. **Real time and speed.** Pace timers/RTC to wall-clock time with the host
   clock, accelerate hot loops so the UI responds at a usable frame rate.
9. **Optional.** Synthetic sensor data (heart rate, steps); Bluetooth/phone
   sync stays out of scope.

Steps 3 and 5 are open-ended reverse engineering: their size depends on how
many peripherals the firmware touches before the first frame.
