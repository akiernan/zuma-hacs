"""Async client for the StreamUnlimited StreamSDK HTTP API exposed by Zuma devices.

Surface (port 80, plaintext, no authentication), every call a JSON POST, as the
official nSDK bindings make them:

    POST /api/getData            {"path", "roles": [..], "type": "structure"}
    POST /api/getRows            {"path", "roles": [..], "from", "to", "type": "structure"}
    POST /api/setData            {"path", "role", "value"}
    POST /api/event/modifyQueue  {"queueId"?, "subscribe": [..], "unsubscribe": [..]}
    POST /api/event/pollQueue    {"queueId", "timeout"}

``"type": "structure"`` makes getData answer with an object keyed by role name
(``{"value": ..., "title": ...}``) and getRows with one such object per row,
rather than arrays that must be matched to the requested roles by position.

Two quirks drive the code below:
  * Values are tagged unions -- {"i32_": 22, "type": "i32_"} -- so reads must be
    unwrapped and writes must be re-tagged with the matching type name.
  * Application-level errors arrive as a non-2xx status (usually 500) with a JSON
    {"error": {...}} body; other failures (e.g. "Unknown queue id!", 400) are bare
    text. Either way the status, not the body, decides.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import Any

import aiohttp

from .const import (
    CONTROL_VERBS,
    PATH_CIRCADIAN,
    PATH_CONTROL,
    PATH_DEVICE_NAME,
    PATH_LED_CURFEW,
    PATH_LIGHT,
    PATH_BEZEL,
    PATH_MANUFACTURER,
    PATH_MASTER,
    PATH_NETWORK_INFO,
    PATH_TEMP_MODE,
    PATH_MODEL,
    PATH_MUTE,
    PATH_PLAYER_DATA,
    PATH_SERIAL,
    PATH_VERSION,
    PATH_VOLUME,
    VOLUME_MAX,
)

_LOGGER = logging.getLogger(__name__)


class ZumaError(Exception):
    """The device replied, but with an application-level error."""


def unwrap(reply: Any, role: str = "value") -> Any:
    """Turn a getData reply into the plain Python value of one role."""
    if not isinstance(reply, dict):
        return None
    return unwrap_item(reply.get(role))


def unwrap_item(item: Any) -> Any:
    """Unwrap one tagged value.

    A scalar leaf is tagged (``{"i32_": 22, "type": "i32_"}``); a composite node
    such as ``player:player/data`` is a plain untagged dict and is returned as-is.
    """
    if not isinstance(item, dict):
        return item
    tag = item.get("type")
    return item.get(tag) if tag else item


def wrap(value: bool | int | str) -> dict[str, Any]:
    """Tag a Python scalar the way setData expects."""
    # bool before int on purpose: bool is a subclass of int, and sending a
    # boolean as i32_ makes the device reject the write.
    if isinstance(value, bool):
        return {"bool_": value, "type": "bool_"}
    if isinstance(value, int):
        return {"i32_": value, "type": "i32_"}
    if isinstance(value, str):
        return {"string_": value, "type": "string_"}
    raise TypeError(f"no StreamSDK tag for {type(value).__name__}")


class ZumaApi:
    """Talk to one Zuma unit's StreamSDK web API."""

    def __init__(
        self, host: str, session: aiohttp.ClientSession, timeout: float = 8.0
    ) -> None:
        self._host = host
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def host(self) -> str:
        """Address this client is bound to."""
        return self._host

    async def _request(
        self,
        endpoint: str,
        body: dict[str, Any],
        *,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> Any:
        url = f"http://{self._host}/api/{endpoint}"
        try:
            async with self._session.post(
                url, json=body, timeout=timeout or self._timeout
            ) as resp:
                status = resp.status
                text = await resp.text()
        except aiohttp.ClientError as err:
            raise ZumaError(f"cannot reach {self._host}: {err}") from err

        try:
            data = json.loads(text) if text else None
        except ValueError:
            data = None
            if 200 <= status < 300:
                raise ZumaError(f"non-JSON reply from {endpoint}: {text[:120]}") from None

        if not 200 <= status < 300:
            if isinstance(data, dict) and isinstance(data.get("error"), dict):
                raise ZumaError(str(data["error"].get("message", data["error"])))
            raise ZumaError(f"HTTP {status} from {endpoint}: {text[:120]}")
        return data

    # --- primitives -------------------------------------------------------

    async def get_data(self, path: str, roles: Sequence[str] = ("value",)) -> Any:
        """Raw getData: an object keyed by role name, values still tagged."""
        return await self._request(
            "getData", {"path": path, "roles": list(roles), "type": "structure"}
        )

    async def get_value(self, path: str) -> Any:
        """getData for the ``value`` role, unwrapped."""
        return unwrap(await self.get_data(path))

    async def set_value(self, path: str, value: bool | int | str) -> Any:
        """setData on the ``value`` role."""
        return await self._request(
            "setData", {"path": path, "role": "value", "value": wrap(value)}
        )

    async def get_rows(
        self,
        path: str,
        roles: Sequence[str] = ("path", "type"),
        start: int = 0,
        end: int = 200,
    ) -> list[dict[str, Any]]:
        """List a container node's children, one role-keyed object per row."""
        reply = await self._request(
            "getRows",
            {
                "path": path,
                "roles": list(roles),
                "from": start,
                "to": end,
                "type": "structure",
            },
        )
        rows = reply.get("rows") if isinstance(reply, dict) else None
        return [row for row in rows or [] if isinstance(row, dict) and row]

    # --- conveniences -----------------------------------------------------

    async def get_volume(self) -> int | None:
        """Volume as the device counts it: 0-100."""
        return await self.get_value(PATH_VOLUME)

    async def set_volume(self, volume: int) -> None:
        """Set volume, clamped to the device's 0-100 range."""
        await self.set_value(PATH_VOLUME, max(0, min(VOLUME_MAX, int(volume))))

    async def get_mute(self) -> bool | None:
        """Current mute flag."""
        return await self.get_value(PATH_MUTE)

    async def set_mute(self, mute: bool) -> None:
        """Mute or unmute."""
        await self.set_value(PATH_MUTE, bool(mute))

    async def control(self, verb: str) -> Any:
        """Invoke a transport verb on player:player/control.

        Only pause/stop/next/previous exist. An unrecognised value shape is not
        rejected as such -- the device falls back to "play the current directory"
        and reports "Directory is empty. No playable items found.", which is why
        a bad verb looks like a playback failure.
        """
        if verb not in CONTROL_VERBS:
            raise ValueError(f"unknown control verb {verb!r}; have {CONTROL_VERBS}")
        return await self._request(
            "setData", {"path": PATH_CONTROL, "role": "activate", "value": {"control": verb}}
        )

    async def get_player_state(self) -> str | None:
        """Transport state string: stopped / playing / paused."""
        data = await self.get_value(PATH_PLAYER_DATA)
        return data.get("state") if isinstance(data, dict) else None

    # --- event queue (push) ----------------------------------------------

    async def create_event_queue(self, paths: list[str]) -> str:
        """Create a change-notification queue subscribed to leaf nodes.

        Leaf nodes subscribe with type ``item`` (containers would use ``rows``);
        the device then pushes ``{"itemType": "update", "path": ...}`` when one
        changes. Returns the queue id (a brace-wrapped UUID) to poll.
        """
        qid = await self._request(
            "event/modifyQueue",
            {"subscribe": [{"path": p, "type": "item"} for p in paths]},
        )
        if not isinstance(qid, str):
            raise ZumaError(f"unexpected modifyQueue reply: {qid!r}")
        return qid

    async def poll_events(self, queue_id: str, client_timeout: float = 90.0) -> list[str]:
        """Long-poll the queue; return the list of changed paths.

        The device holds the connection until a subscribed node changes (it does
        not honour the ``timeout`` param as an idle return), so ``client_timeout``
        is the liveness ceiling: on expiry the caller simply re-polls. A stale
        queue id yields HTTP 400 "Unknown queue id!", surfaced as ZumaError.
        """
        events = await self._request(
            "event/pollQueue",
            {"queueId": queue_id, "timeout": 30000},
            timeout=aiohttp.ClientTimeout(total=client_timeout),
        )
        if not isinstance(events, list):
            return []
        return [e["path"] for e in events if isinstance(e, dict) and e.get("path")]

    async def get_light(self) -> dict[str, Any] | None:
        """Current lamp state: {power, brightness 0-100, temperature K, ...}."""
        state = await self.get_value(PATH_LIGHT)
        return state if isinstance(state, dict) else None

    async def set_light(self, state: dict[str, Any]) -> Any:
        """Write a full zumaLightState. Caller supplies every field.

        The value is composite, not a tagged scalar, so it bypasses wrap().
        """
        return await self._request(
            "setData",
            {
                "path": PATH_LIGHT,
                "role": "value",
                "value": {"type": "zumaLightState", "zumaLightState": state},
            },
        )

    async def get_identity(self) -> dict[str, Any]:
        """Identity for the config flow and device registry.

        ``serial`` is the same UUID the unit publishes in its mDNS TXT record,
        so a manually-added entry and a discovered one resolve to one device.
        """
        return {
            "serial": await self.get_value(PATH_SERIAL),
            "name": await self.get_value(PATH_DEVICE_NAME),
            "version": await self.get_value(PATH_VERSION),
            "model": await self.get_value(PATH_MODEL),
            "manufacturer": await self.get_value(PATH_MANUFACTURER),
        }

    async def get_state(self) -> dict[str, Any]:
        """One poll of everything the entities need.

        player:player/data is fetched once and mined for both transport state and
        now-playing metadata -- it carries the lot, whatever the source is
        (airable radio, Spotify Connect, AirPlay, TIDAL).
        """
        player = await self.get_value(PATH_PLAYER_DATA)
        if not isinstance(player, dict):
            player = {}
        track = player.get("trackRoles") or {}
        meta = (track.get("mediaData") or {}).get("metaData") or {}
        return {
            "volume": await self.get_volume(),
            "mute": await self.get_mute(),
            "state": player.get("state"),
            "title": track.get("title"),
            "image": track.get("icon"),
            "source": meta.get("serviceID"),
            # The device advertises per-stream which transport ops are valid; live
            # radio reports next_/previous false even though the verbs are accepted.
            "controls": player.get("controls") or {},
            "circadian": await self.get_value(PATH_CIRCADIAN),
            "led_curfew": await self.get_value(PATH_LED_CURFEW),
            "light": await self.get_light(),
            **await self._get_diagnostics(),
        }

    async def _get_diagnostics(self) -> dict[str, Any]:
        """Read-only diagnostics: connectivity, thermal, accessory, group role."""
        # network:info carries a type="networkInfo" tag, so get_value already
        # unwraps it to the inner object (keys: wireless, wired, gateways, ...).
        info = await self.get_value(PATH_NETWORK_INFO)
        if not isinstance(info, dict):
            info = {}
        # Prefer whichever interface is up; a Lumisonic is normally on wireless.
        wired, wifi = info.get("wired") or {}, info.get("wireless") or {}
        iface = wired if wired.get("state") == "up" else wifi
        addrs = iface.get("addresses") or []
        ip = next((a.get("ip") for a in addrs if a.get("protocol") == "ipv4"), None)
        return {
            "ip": ip,
            "ssid": wifi.get("ssid"),
            "bssid": wifi.get("bssid"),
            "rssi": wifi.get("signalLevel"),
            "frequency": wifi.get("frequency"),
            "thermal": await self.get_value(PATH_TEMP_MODE),
            "bezel": await self.get_value(PATH_BEZEL),
            "master": await self.get_value(PATH_MASTER),
        }
