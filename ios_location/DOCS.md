# iOS Location Bridge

Sets the GPS location of an iPhone plugged into this server by USB. Everything on that
phone — Find My, Maps, the Home Assistant app — sees the location this app sets.

Two modes, switched from Home Assistant:

- **Mirror** — the phone follows another tracker (by default `device_tracker.mattern17pro`,
  your daily phone), updating when it moves more than `min_move_meters`.
- **Fixed** — the phone sits at one place you choose.
- **Off** — simulation is cleared and the phone reports its real location.

It uses pymobiledevice3's in-process userspace tunnel, so it needs only USB access — no
root network tunnel, TUN device or elevated network privileges. Requires **iOS 17.4 or later**.

## Helpers it reads

| Helper | Purpose |
|---|---|
| `input_select.ios_location_mode` | `Off` / `Mirror` / `Fixed` |
| `input_text.ios_location_fixed` | The Fixed spot: `29.7604, -95.3698`, an entity with coordinates (`zone.home`, `person.cedric`), or a zone name (`Home`) |

Status is published as **`sensor.ios_location_bridge`** (`off`, `mirroring`, `fixed`,
`waiting`, `no_device`, `needs_attention`, `error`) with the current latitude/longitude,
device name, iOS version and a plain-English `message` attribute.

## First-time setup

1. Prepare the phone: sign into your Apple Account, turn on Find My and **Share My Location**,
   and set **Find My → Me → Use This iPhone as My Location**. Turn off automatic iOS updates
   (Settings → General → Software Update → Automatic Updates, all toggles), and
   Auto-Lock → Never so pairing and the first mount go smoothly.
2. Plug the phone into the server's USB port and start the app.
3. On the phone, tap **Trust** and enter the passcode. The sensor shows `needs_attention`
   until you do.
4. The app reveals **Settings → Privacy & Security → Developer Mode** on the phone. Turn it on,
   let the phone restart, unlock it and confirm **Turn On**.
5. Within a minute the app mounts the developer disk image (downloaded once from GitHub) and the
   sensor goes to `off`. Set `input_select.ios_location_mode` to `Mirror` or `Fixed`.

Pairing records live in the app's `/data`, so updates and rebuilds don't need a new Trust.

## Options

| Option | Default | Meaning |
|---|---|---|
| `mode_entity` | `input_select.ios_location_mode` | Mode helper |
| `source_entity` | `device_tracker.mattern17pro` | What Mirror follows — use the raw GPS tracker, not a `person` that could include the spoofed phone |
| `fixed_entity` | `input_text.ios_location_fixed` | Fixed-location helper |
| `device_udid` | *(first phone)* | Pin to one phone if more than one is attached |
| `poll_interval` | `15` s | How often HA is checked |
| `min_move_meters` | `15` | Ignore tracker jitter smaller than this |
| `max_gps_accuracy` | `150` m | Skip fixes worse than this (the last location is held) |
| `reassert_minutes` | `5` | Re-send the location even if unchanged — doubles as a health check |
| `notify_service` | `notify.mobile_app_mattern17pro` | Alert when simulation stops and when it resumes; blank to disable |
| `notify_after_seconds` | `180` | Grace period before the stop alert |

## What it handles by itself

- **Phone reboots / iOS updates / cable pulls:** the session is rebuilt and the developer disk
  image remounted automatically (it lives in RAM, so every reboot needs it). After a restart the
  phone must be **unlocked at the moment of the remount** — unlock it and keep it on the Home
  Screen until the status shows Mirroring or Fixed (retries every 10 s). Once mounted, it keeps
  working while locked.
- **USB not seen after a replug:** usbmuxd is restarted after 60 s with no phone.
- **Add-on stop:** the simulated location is cleared first.

Whenever it can't hold the location, the phone reports its **real** location. That's what the
`notify_service` alert is for.

## Cautions

- Don't let the spoofed phone's Home Assistant app count toward Pine Hollow presence — it will
  report the fake location.
- Mirror lags the source by `poll_interval` plus however often the source tracker updates.
- A sudden jump between Fixed spots is visible to anyone watching live.
