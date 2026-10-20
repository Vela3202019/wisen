"""
The laptop's side of the device API: pairing, and a small HTTP client that
only ever connects out. Every packet it sends goes through the egress gate
(wisen/egress.py), which logs it as "cloud:<host>" before it leaves.

Pairing stores the server URL, the server's user id, this device's id and its
token in ~/.wisen/cloud.json (WISEN_CLOUD_CREDENTIALS), readable by you only.
"""
import json
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

import httpx

from wisen import config, egress

POLL_WAIT = 25.0  # seconds the server may hold a poll open


def credentials_path() -> Path:
    return Path(config.get("cloud", "credentials", "WISEN_CLOUD_CREDENTIALS", "~/.wisen/cloud.json")).expanduser()


@dataclass
class Credentials:
    url: str
    user_id: str
    device_id: str
    token: str

    @classmethod
    def load(cls, path: Path | None = None) -> "Credentials | None":
        try:
            return cls(**json.loads((path or credentials_path()).read_text()))
        except FileNotFoundError:
            return None

    def save(self, path: Path | None = None) -> Path:
        path = path or credentials_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(tmp, path)
        return path


class Revoked(Exception):
    """The server refused this device's token (401): it was revoked."""


class Refused(Exception):
    """The server refused the request (4xx other than 401); retrying will not help."""


class Unavailable(Exception):
    """The server could not be reached or failed (network error, 5xx); retry later."""


def check_url(url: str) -> str:
    """The server's base URL. HTTPS only, except for a server on this machine."""
    parts = urlsplit(url.rstrip("/"))
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ValueError("the server URL must start with https:// (http:// only for localhost)")
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


async def _send(http: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
    try:
        response = await http.request(method, url, **kwargs)
    except httpx.TransportError as e:
        raise Unavailable(f"{type(e).__name__}: {e}") from None
    if response.status_code == 401:
        raise Revoked("the server refused this device's token; pair again with `wisen cloud pair`")
    if response.status_code >= 500:
        raise Unavailable(f"server error {response.status_code}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:200]
        raise Refused(f"{response.status_code}: {detail}")
    return response.json()


async def pair(url: str, code: str, name: str | None = None, *, http: httpx.AsyncClient | None = None) -> Credentials:
    """Spend a one-time code from the dashboard on a device token."""
    url = check_url(url)
    async with _client(http) as client:
        paired = await _send(client, "POST", f"{url}/api/device/pair",
                             json={"code": code, "name": name or platform.node() or "laptop"})
    return Credentials(url=url, **paired)


class _client:
    """Use the given httpx client (tests pass one bound to the app) or own one."""

    def __init__(self, http: httpx.AsyncClient | None):
        self.http, self.owned = http, None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self.http is None:
            self.owned = httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=POLL_WAIT + 15))
        return self.http or self.owned

    async def __aexit__(self, *exc) -> None:
        if self.owned:
            await self.owned.aclose()


class CloudClient:
    def __init__(self, creds: Credentials, http: httpx.AsyncClient | None = None):
        self.creds = creds
        self.destination = f"cloud:{urlsplit(creds.url).netloc}"
        self._ctx = _client(http)
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._http = await self._ctx.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._ctx.__aexit__(*exc)

    async def _post(self, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self.creds.token}", "Content-Type": "application/json"}
        return await _send(self._http, "POST", f"{self.creds.url}{path}", headers=headers, **kwargs)

    async def send_packet(self, packet: dict) -> dict:
        """Check, log and upload one packet. Raises EgressRejected (nothing
        sent) if the gate refuses it."""
        return await self._post("/api/device/packets", content=egress.prepare(packet, self.destination))

    async def poll(self, cursor: int, wait: float = POLL_WAIT) -> dict:
        return await self._post("/api/device/poll", json={"cursor": cursor, "wait": wait})

    async def heartbeat(self, job_id: str) -> None:
        await self._post(f"/api/device/jobs/{job_id}/heartbeat", json={})

    async def finish(self, job_id: str, status: dict) -> None:
        await self._post(f"/api/device/jobs/{job_id}/finish", content=egress.prepare(status, self.destination))
