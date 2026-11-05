# Changelog

## 0.1.1

- Fix: a phone plugged in (or rebooted) after the app started was never seen, because USB hot-plug events don't reach usbmuxd inside the container. usbmuxd now rescans every 15 s while no phone is found.

## 0.1.0

- First release: Off / Mirror / Fixed modes driven by Home Assistant helpers.
- In-process userspace tunnel (iOS 17.4+) — USB access only, no elevated network privileges.
- Self-healing: automatic re-pair, developer-disk-image remount, usbmuxd restart, reconnect.
- `sensor.ios_location_bridge` status entity and optional drop/recovery push alerts.
