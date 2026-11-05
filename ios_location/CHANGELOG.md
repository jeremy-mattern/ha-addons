# Changelog

## 0.1.0

- First release: Off / Mirror / Fixed modes driven by Home Assistant helpers.
- In-process userspace tunnel (iOS 17.4+) — USB access only, no elevated network privileges.
- Self-healing: automatic re-pair, developer-disk-image remount, usbmuxd restart, reconnect.
- `sensor.ios_location_bridge` status entity and optional drop/recovery push alerts.
