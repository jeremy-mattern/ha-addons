"""Minimal Home Assistant REST client for the add-on (via the Supervisor proxy)."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests

log = logging.getLogger("ios_location.ha")


class HomeAssistant:
    def __init__(self, base_url: Optional[str] = None, token: Optional[str] = None, timeout: float = 10.0) -> None:
        self.base_url = (base_url or os.environ.get("HA_URL") or "http://supervisor/core/api").rstrip("/")
        self.token = token or os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HA_TOKEN", "")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        )

    def get_state(self, entity_id: str) -> Optional[dict[str, Any]]:
        """Return the state object for entity_id, or None if it doesn't exist."""
        if not entity_id:
            return None
        resp = self.session.get(f"{self.base_url}/states/{entity_id}", timeout=self.timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def get_states(self) -> list[dict[str, Any]]:
        resp = self.session.get(f"{self.base_url}/states", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def set_state(self, entity_id: str, state: str, attributes: dict[str, Any]) -> None:
        resp = self.session.post(
            f"{self.base_url}/states/{entity_id}",
            json={"state": state, "attributes": attributes},
            timeout=self.timeout,
        )
        resp.raise_for_status()

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        resp = self.session.post(f"{self.base_url}/services/{domain}/{service}", json=data, timeout=self.timeout)
        resp.raise_for_status()
