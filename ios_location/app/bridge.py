"""iOS Location Bridge — main loop.

Every poll it reads the mode helper (Off / Mirror / Fixed) from Home Assistant, works out the
target location, keeps one location-simulation session open on the USB-attached iPhone, and
publishes its status as sensor.ios_location_bridge. Anything that drops the session (cable,
reboot, iOS update, phone locked during pairing) is retried automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ha import HomeAssistant
from targets import MODE_FIXED, MODE_MIRROR, MODE_OFF, Target, distance_m, resolve_target

log = logging.getLogger("ios_location")

STATUS_ENTITY = "sensor.ios_location_bridge"
ACTIVE_STATES = {"mirroring", "fixed"}
PROBLEM_STATES = {"no_device", "needs_attention", "error"}
STATUS_LABELS = {
    "off": "Off",
    "mirroring": "Mirroring",
    "fixed": "Fixed location",
    "waiting": "Waiting for location",
    "no_device": "iPhone not connected",
    "needs_attention": "Needs attention on iPhone",
    "error": "Error",
    "stopped": "Add-on stopped",
}
NO_DEVICE_USBMUXD_RESTART_S = 60


def load_options() -> dict[str, Any]:
    path = Path(os.environ.get("OPTIONS_PATH", "/data/options.json"))
    defaults = {
        "mode_entity": "input_select.ios_location_mode",
        "source_entity": "device_tracker.mattern17pro",
        "fixed_entity": "input_text.ios_location_fixed",
        "device_udid": "",
        "poll_interval": 15,
        "min_move_meters": 15,
        "max_gps_accuracy": 150,
        "reassert_minutes": 5,
        "notify_service": "",
        "notify_after_seconds": 180,
        "log_level": "info",
    }
    if path.exists():
        defaults.update({k: v for k, v in json.loads(path.read_text()).items() if v is not None})
    return defaults


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Bridge:
    def __init__(self, opts: dict[str, Any]) -> None:
        self.opts = opts
        self.ha = HomeAssistant()
        self.fake = os.environ.get("BRIDGE_FAKE_DEVICE") == "1"
        if self.fake:
            from device import FakeSession as Session

            self.usbmuxd = None
        else:
            from device import IPhoneSession as Session, Usbmuxd

            self.usbmuxd = Usbmuxd()
        self.Session = Session
        self.session = None
        self.stopping = asyncio.Event()

        self.last_set: Optional[tuple[float, float]] = None
        self.last_set_at = 0.0
        self.last_set_iso: Optional[str] = None
        self.backoff = 5.0
        self.next_connect_at = 0.0
        self.no_device_since: Optional[float] = None

        self.status = ""
        self.status_attrs: dict[str, Any] = {}
        self.status_published_at = 0.0
        self.problem_since: Optional[float] = None
        self.problem_notified = False
        self.was_active = False

    # ---------- Home Assistant side ----------

    def _get_state(self, entity_id: str) -> Optional[dict[str, Any]]:
        return self.ha.get_state(entity_id)

    def _read_target(self) -> Target:
        mode_state = self.ha.get_state(self.opts["mode_entity"])
        if mode_state is None:
            log.warning("mode helper %s not found — treating as Off", self.opts["mode_entity"])
        return resolve_target(
            mode_state,
            self.opts["source_entity"],
            self.opts["fixed_entity"],
            self._get_state,
            int(self.opts["max_gps_accuracy"]),
        )

    async def publish(self, state: str, message: str, target: Optional[Target] = None) -> None:
        session = self.session
        attrs: dict[str, Any] = {
            "friendly_name": "iOS Location Bridge",
            "icon": "mdi:map-marker-off" if state in PROBLEM_STATES | {"off", "stopped"} else "mdi:map-marker-radius",
            "status": STATUS_LABELS.get(state, state),
            "message": message,
            "mode": target.mode if target else None,
            "source": target.source if target else None,
            "latitude": self.last_set[0] if self.last_set else None,
            "longitude": self.last_set[1] if self.last_set else None,
            "last_location_update": self.last_set_iso,
            "device_name": getattr(session, "name", None) or None,
            "ios_version": getattr(session, "ios_version", None) or None,
            "udid": getattr(session, "udid", None) or None,
        }
        changed = state != self.status or attrs != self.status_attrs
        if changed:
            if state != self.status:
                log.info("status: %s — %s", state, message)
            self._track_problems(state, message)
        if changed or time.monotonic() - self.status_published_at > 60:
            try:
                await asyncio.to_thread(self.ha.set_state, STATUS_ENTITY, state, attrs)
                self.status_published_at = time.monotonic()
            except Exception as e:  # noqa: BLE001
                log.warning("could not publish status to Home Assistant: %s", e)
        self.status, self.status_attrs = state, attrs

    def _track_problems(self, state: str, message: str) -> None:
        if state in ACTIVE_STATES:
            if self.problem_notified:
                self._notify(f"Location simulation is back on ({STATUS_LABELS[state].lower()}).")
            self.problem_since, self.problem_notified, self.was_active = None, False, True
        elif state in PROBLEM_STATES:
            if self.problem_since is None:
                self.problem_since = time.monotonic()
        else:
            self.problem_since, self.problem_notified = None, False

    async def maybe_notify_problem(self) -> None:
        if (
            self.problem_since is not None
            and not self.problem_notified
            and self.was_active
            and time.monotonic() - self.problem_since >= int(self.opts["notify_after_seconds"])
        ):
            self.problem_notified = True
            msg = self.status_attrs.get("message") or self.status
            await asyncio.to_thread(
                self._notify,
                f"Location simulation has stopped — the iPhone is reporting its real location. {msg}",
            )

    def _notify(self, message: str) -> None:
        service = (self.opts.get("notify_service") or "").strip()
        if not service:
            return
        domain, _, name = service.partition(".")
        if not name:
            domain, name = "notify", domain
        try:
            self.ha.call_service(domain, name, {"title": "iOS Location Bridge", "message": message})
        except Exception as e:  # noqa: BLE001
            log.warning("notify via %s failed: %s", service, e)

    # ---------- iPhone side ----------

    async def close_session(self, clear: bool = False) -> None:
        session, self.session = self.session, None
        self.last_set = None
        self.last_set_iso = None
        if session is None:
            return
        if clear:
            try:
                await session.clear()
                log.info("cleared simulated location")
            except Exception:  # noqa: BLE001
                log.debug("clear failed", exc_info=True)
        await session.close()

    async def ensure_session(self, target: Target) -> bool:
        if self.session is not None and self.session.alive:
            return True
        if self.session is not None:
            log.warning("iPhone session dropped; reconnecting")
            await self.close_session()
        if time.monotonic() < self.next_connect_at:
            return False

        from device import DeviceNotReady, NoDevice

        session = self.Session(self.opts.get("device_udid") or None)
        try:
            await asyncio.wait_for(session.open(), timeout=180)
        except NoDevice as e:
            await session.close()
            self._schedule_retry(fast=True)
            if self.no_device_since is None:
                self.no_device_since = time.monotonic()
            elif self.usbmuxd and time.monotonic() - self.no_device_since > NO_DEVICE_USBMUXD_RESTART_S:
                await asyncio.to_thread(self.usbmuxd.restart)
                self.no_device_since = time.monotonic()
            await self.publish("no_device", str(e), target)
            return False
        except DeviceNotReady as e:
            await session.close()
            self.no_device_since = None
            self._schedule_retry(fast=True)
            await self.publish("needs_attention", str(e), target)
            return False
        except Exception as e:  # noqa: BLE001
            await session.close()
            self.no_device_since = None
            log.warning("could not open iPhone session: %r", e, exc_info=log.isEnabledFor(logging.DEBUG))
            self._schedule_retry()
            await self.publish("error", f"could not connect: {e}", target)
            return False

        self.session = session
        self.no_device_since = None
        self.backoff = 5.0
        self.last_set = None
        return True

    def _schedule_retry(self, fast: bool = False) -> None:
        delay = 10.0 if fast else self.backoff
        if not fast:
            self.backoff = min(self.backoff * 2, 120.0)
        self.next_connect_at = time.monotonic() + delay

    async def apply(self, target: Target) -> None:
        assert target.latitude is not None and target.longitude is not None
        coords = (target.latitude, target.longitude)
        reassert = int(self.opts["reassert_minutes"]) * 60
        due = (
            self.last_set is None
            or distance_m(self.last_set, coords) >= int(self.opts["min_move_meters"])
            or time.monotonic() - self.last_set_at >= reassert
        )
        if not due:
            return
        assert self.session is not None
        try:
            await self.session.set(*coords)
        except Exception as e:  # noqa: BLE001
            log.warning("setting location failed (%r); will reconnect", e)
            await self.close_session()
            self._schedule_retry()
            await self.publish("error", f"setting location failed: {e}", target)
            return
        moved = self.last_set is None or distance_m(self.last_set, coords) >= 1
        self.last_set, self.last_set_at = coords, time.monotonic()
        if moved:
            self.last_set_iso = now_iso()
            log.info("location set to %.6f, %.6f (%s)", coords[0], coords[1], target.source)

    # ---------- loop ----------

    async def tick(self) -> None:
        try:
            target = await asyncio.to_thread(self._read_target)
        except Exception as e:  # noqa: BLE001
            log.warning("could not read Home Assistant state: %s", e)
            return

        if target.mode == MODE_OFF:
            if self.session is not None:
                await self.close_session(clear=True)
            self.next_connect_at = 0.0
            await self.publish("off", "not simulating — the iPhone reports its real location", target)
            return

        if not await self.ensure_session(target):
            return

        if not target.has_location:
            held = "holding the last location" if self.last_set else "nothing set yet"
            await self.publish("waiting", f"{target.problem}; {held}", target)
            return

        await self.apply(target)
        if self.session is None:
            return  # apply() failed and already published the error
        state = "mirroring" if target.mode == MODE_MIRROR else "fixed"
        await self.publish(state, f"{STATUS_LABELS[state]} from {target.source}", target)

    async def run(self) -> None:
        if self.usbmuxd:
            await asyncio.to_thread(self.usbmuxd.start)
            await asyncio.sleep(2)
        poll = int(self.opts["poll_interval"])
        log.info(
            "iOS Location Bridge running: mode %s, source %s, fixed %s, poll %ss",
            self.opts["mode_entity"], self.opts["source_entity"], self.opts["fixed_entity"], poll,
        )
        while not self.stopping.is_set():
            try:
                await self.tick()
                await self.maybe_notify_problem()
            except Exception:  # noqa: BLE001 - the loop must never die
                log.exception("unexpected error in main loop")
            if self.usbmuxd and not self.usbmuxd.running:
                log.warning("usbmuxd exited; restarting it")
                await asyncio.to_thread(self.usbmuxd.restart)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass

        log.info("stopping: clearing simulated location")
        await self.close_session(clear=True)
        await self.publish("stopped", "add-on stopped — the iPhone reports its real location")
        if self.usbmuxd:
            await asyncio.to_thread(self.usbmuxd.stop)


async def amain() -> None:
    opts = load_options()
    logging.basicConfig(
        level=getattr(logging, str(opts.get("log_level", "info")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # pymobiledevice3 is chatty at INFO.
    if opts.get("log_level") != "debug":
        for noisy in ("pymobiledevice3", "asyncio", "urllib3", "developer_disk_image", "pytcp"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    bridge = Bridge(opts)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, bridge.stopping.set)
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(amain())
