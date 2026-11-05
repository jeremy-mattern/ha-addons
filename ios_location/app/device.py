"""The iPhone side: usbmuxd, pairing, developer disk image, and the location-simulation session.

Uses pymobiledevice3's in-process userspace tunnel (iOS 17.4+), so no root tunnel, TUN device or
NET_ADMIN is needed — only USB access for usbmuxd.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Optional

log = logging.getLogger("ios_location.device")

LOCKDOWN_DIR = Path("/var/lib/lockdown")
PERSIST_LOCKDOWN_DIR = Path(os.environ.get("DATA_DIR", "/data")) / "lockdown"


class DeviceNotReady(Exception):
    """The phone is attached but needs something from the person (Trust, Developer Mode, unlock)."""


class NoDevice(Exception):
    """No iPhone is visible to usbmuxd."""


class Usbmuxd:
    """Runs usbmuxd as a child process and restarts it on demand."""

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None

    @staticmethod
    def _persist_pair_records() -> None:
        # Keep usbmuxd's pair records in /data so a container rebuild doesn't need a new "Trust".
        PERSIST_LOCKDOWN_DIR.mkdir(parents=True, exist_ok=True)
        if LOCKDOWN_DIR.is_symlink():
            return
        if LOCKDOWN_DIR.exists():
            for item in LOCKDOWN_DIR.iterdir():
                target = PERSIST_LOCKDOWN_DIR / item.name
                if not target.exists():
                    shutil.move(str(item), target)
            shutil.rmtree(LOCKDOWN_DIR, ignore_errors=True)
        LOCKDOWN_DIR.parent.mkdir(parents=True, exist_ok=True)
        LOCKDOWN_DIR.symlink_to(PERSIST_LOCKDOWN_DIR)

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self._persist_pair_records()
        binary = shutil.which("usbmuxd")
        if not binary:
            raise RuntimeError("usbmuxd is not installed in the image")
        log.info("starting usbmuxd")
        # -f: stay in the foreground (we supervise it); -U root: keep access to /dev/bus/usb.
        self.proc = subprocess.Popen([binary, "-f", "-U", "root"], stdout=subprocess.DEVNULL)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def restart(self) -> None:
        log.info("restarting usbmuxd to rescan USB")
        self.stop()
        # usbmuxd leaves its socket behind if killed hard.
        try:
            os.unlink("/var/run/usbmuxd")
        except FileNotFoundError:
            pass
        self.start()

    @property
    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)


class IPhoneSession:
    """One open location-simulation session on one iPhone. Reconnect by making a new one."""

    def __init__(self, udid: Optional[str] = None) -> None:
        self.wanted_udid = udid or None
        self.udid: Optional[str] = None
        self.name: str = ""
        self.ios_version: str = ""
        self._stack: Optional[AsyncExitStack] = None
        self._tunnel = None
        self._location = None

    @property
    def alive(self) -> bool:
        return self._stack is not None and self._tunnel is not None and getattr(self._tunnel, "rsd", None) is not None

    async def open(self) -> None:
        from pymobiledevice3 import usbmux
        from pymobiledevice3.exceptions import (
            AlreadyMountedError,
            NotPairedError,
            PairingDialogResponsePendingError,
            PasswordRequiredError,
            UserDeniedPairingError,
            UserspaceTunnelUnavailableError,
        )
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
        from pymobiledevice3.services.amfi import AmfiService
        from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
        from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
        from pymobiledevice3.services.mobile_image_mounter import auto_mount

        try:
            devices = [d for d in await usbmux.list_devices() if d.is_usb]
        except Exception as e:  # noqa: BLE001 - usbmuxd still starting or just restarted
            raise NoDevice(f"USB service not ready ({type(e).__name__})") from e
        if self.wanted_udid:
            devices = [d for d in devices if d.matches_udid(self.wanted_udid)]
        if not devices:
            raise NoDevice("no iPhone connected over USB" + (f" with UDID {self.wanted_udid}" if self.wanted_udid else ""))
        if len(devices) > 1 and not self.wanted_udid:
            log.warning("more than one iPhone attached; using %s (set device_udid to choose)", devices[0].serial)
        serial = devices[0].serial

        # Pair (shows "Trust This Computer?" on the phone the first time) and check Developer Mode.
        try:
            lockdown = await create_using_usbmux(serial=serial, autopair=True, pair_timeout=30)
        except (PairingDialogResponsePendingError, NotPairedError):
            raise DeviceNotReady("tap Trust on the iPhone and enter its passcode")
        except UserDeniedPairingError:
            raise DeviceNotReady("Trust was declined on the iPhone — unplug and replug it, then tap Trust")
        except PasswordRequiredError:
            raise DeviceNotReady("unlock the iPhone so it can be paired")
        try:
            self.udid = lockdown.udid
            info = lockdown.short_info
            self.name = info.get("DeviceName", "")
            self.ios_version = info.get("ProductVersion", lockdown.product_version)
            if lockdown.has_developer_mode and not await lockdown.get_developer_mode_status():
                try:
                    await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
                except Exception:  # noqa: BLE001 - revealing is best effort
                    log.debug("could not reveal the Developer Mode toggle", exc_info=True)
                raise DeviceNotReady(
                    "turn on Developer Mode on the iPhone (Settings → Privacy & Security → Developer Mode), "
                    "then let it restart and confirm"
                )
        finally:
            await lockdown.close()

        stack = AsyncExitStack()
        try:
            tunnel = UserspaceRsdTunnel(serial=serial, autopair=True, remotepairing_fallback=False)
            try:
                rsd = await tunnel.aopen()
            except UserspaceTunnelUnavailableError:
                raise DeviceNotReady(f"iOS {self.ios_version} is too old — this add-on needs iOS 17.4 or later")
            stack.push_async_callback(tunnel.aclose)
            self._tunnel = tunnel

            # The developer disk image lives in RAM on the phone: remount after every reboot.
            try:
                await auto_mount(rsd)
                log.info("developer disk image mounted")
            except AlreadyMountedError:
                log.debug("developer disk image already mounted")
            except Exception as e:  # noqa: BLE001
                # After a restart the phone won't take the image until it's unlocked once.
                if "DeviceLocked" in str(e):
                    raise DeviceNotReady(
                        "the iPhone restarted — unlock it and keep it on the Home Screen until this "
                        "shows Mirroring or Fixed (about 15 seconds)"
                    ) from e
                raise

            dvt = await stack.enter_async_context(DvtProvider(rsd))
            self._location = await stack.enter_async_context(LocationSimulation(dvt))
        except BaseException:
            await stack.aclose()
            self._tunnel = None
            raise
        self._stack = stack
        log.info("connected to %s (iOS %s, %s)", self.name or "iPhone", self.ios_version, self.udid)

    async def set(self, latitude: float, longitude: float) -> None:
        if not self.alive or self._location is None:
            raise ConnectionError("session is not open")
        await asyncio.wait_for(self._location.set(latitude, longitude), timeout=20)

    async def clear(self) -> None:
        if self.alive and self._location is not None:
            await asyncio.wait_for(self._location.clear(), timeout=20)

    async def close(self) -> None:
        stack, self._stack = self._stack, None
        self._location = None
        self._tunnel = None
        if stack is not None:
            try:
                await asyncio.wait_for(stack.aclose(), timeout=20)
            except Exception:  # noqa: BLE001
                log.debug("error while closing session", exc_info=True)


class FakeSession:
    """Stand-in used for testing without a phone (BRIDGE_FAKE_DEVICE=1)."""

    calls: list[tuple] = []

    def __init__(self, udid: Optional[str] = None) -> None:
        self.udid = udid or "00008130-FAKE000000000000"
        self.name = "Fake iPhone"
        self.ios_version = "26.1"
        self._open = False

    @property
    def alive(self) -> bool:
        return self._open and not os.path.exists("/tmp/fake_unplugged")

    async def open(self) -> None:
        if os.path.exists("/tmp/fake_unplugged"):
            raise NoDevice("no iPhone connected over USB")
        self._open = True
        FakeSession.calls.append(("open",))

    async def set(self, latitude: float, longitude: float) -> None:
        if not self.alive:
            raise ConnectionError("session is not open")
        FakeSession.calls.append(("set", round(latitude, 6), round(longitude, 6)))
        log.info("FAKE set %.6f, %.6f", latitude, longitude)

    async def clear(self) -> None:
        FakeSession.calls.append(("clear",))
        log.info("FAKE clear")

    async def close(self) -> None:
        self._open = False
        FakeSession.calls.append(("close",))
