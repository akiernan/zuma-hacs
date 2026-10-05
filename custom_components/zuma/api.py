"""Async client for the StreamUnlimited StreamSDK HTTP API exposed by Zuma devices.

Surface (port 80, plaintext, no authentication), every call a JSON POST, as the
official nSDK bindings make them:

    POST /api/getData            {"path", "roles": [..], "type": "structure"}
    POST /api/getRows            {"path", "roles": [..], "from", "to", "type": "structure"}
    POST /api/setData            {"path", "role", "value"}
    POST /api/event/modifyQueue  {"queueId"?, "subscribe": [..], "unsubscribe": [..]}
    POST /api/event/pollQueue    {"queueId", "timeout": <seconds>}

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

from collections.abc import Awaitable, Callable
import asyncio
import json
import logging
from collections.abc import Sequence
from typing import Any

import aiohttp

from .const import (
    CONTROL_VERBS,
    PATH_CIRCADIAN,
    AIRABLE_RADIO_ID_PREFIX,
    PATH_AIRABLE,
    PATH_CONTROL,
    PATH_DEVICE_NAME,
    PATH_LED_CURFEW,
    PATH_LIGHT,
    PATH_BEZEL,
    PATH_MANUFACTURER,
    PATH_MASTER,
    PATH_NETWORK_INFO,
    PATH_WIRELESS_RSSI,
    PATH_TEMP_MODE,
    PATH_MODEL,
    PATH_MUTE,
    PATH_PLAYER_DATA,
    PATH_SERIAL,
    PUSH_POLL_TIMEOUT_SECONDS,
    PATH_VERSION,
    PATH_VOLUME,
    VOLUME_MAX,
)

_LOGGER = logging.getLogger(__name__)

# The device can silently drop a volume write that lands right on the heels of
# another (both get HTTP 200; the second never takes). How often varies by unit and
# its state: one Zuma SL lost 30 of 30 back-to-back pairs, another 0 of 30. Spacing
# fixes most of it: on the lossy unit, slider-style bursts spaced 0.15 s still needed
# a resend in 5 of 40, while at 0.3 s none of 40 did.
VOLUME_WRITE_SPACING = 0.3
# Spacing makes a drop rare but can't undo one -- say, a write from another app
# landing just ahead of ours. So once a burst of writes settles, the volume is
# read back after this long and, if the last value didn't take, written once more.
VOLUME_VERIFY_DELAY = 1.0

# How many requests a poll keeps in flight at once against the (embedded) device.
READ_CONCURRENCY = 4


class ZumaError(Exception):
    """The device replied, but with an application-level error."""


def unwrap(reply: Any, role: str = "value") -> Any:
    """Turn a getData reply into the plain Python value of one role."""
    if not isinstance(reply, dict):
        return None
    return unwrap_item(reply.get(role))


def unwrap_item(item: Any) -> Any:
    """Unwrap one tagged value: a getData role or an event's ``itemValue``.

    A scalar leaf is tagged (``{"i32_": 22, "type": "i32_"}``); a composite node
    such as ``player:player/data`` is a plain untagged dict and is returned as-is.
    """
    if not isinstance(item, dict):
        return item
    tag = item.get("type")
    if not tag:
        return item
    value = item.get(tag)
    if tag == "bool_" and not isinstance(value, bool):
        return _lax_bool(value)
    return value


# The device doesn't enforce types: a bool_ may arrive as the string "0" or "1",
# and a non-empty string is truthy, so taken as-is "0" would read as on.
_BOOL_STRINGS = {"0": False, "1": True}


def _lax_bool(value: Any) -> bool | None:
    """A bool_ payload that isn't a JSON boolean; None if it isn't "0"/"1" either."""
    return _BOOL_STRINGS.get(value) if isinstance(value, str) else None


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


def player_fields(player: Any) -> dict[str, Any]:
    """Mine player:player/data for transport state and now-playing metadata.

    It carries the lot, whatever the source is (airable radio, Spotify Connect,
    AirPlay, TIDAL).
    """
    if not isinstance(player, dict):
        player = {}
    track = player.get("trackRoles") or {}
    meta = (track.get("mediaData") or {}).get("metaData") or {}
    return {
        "state": player.get("state"),
        "title": track.get("title"),
        "image": track.get("icon"),
        "source": meta.get("serviceID"),
        # The device advertises per-stream which transport ops are valid; live
        # radio reports next_/previous false even though the verbs are accepted.
        "controls": player.get("controls") or {},
        # What is playing, in the form a play command takes back. Present only
        # while something plays: it disappears from player:player/data on stop.
        "media_roles": player.get("mediaRoles") or None,
    }


def can_pause(state: str | None, controls: dict[str, Any]) -> bool:
    """Whether what is playing can be paused, as the device advertises it.

    A podcast episode reports controls.pause true and pauses; live radio
    leaves pause out and a pause just stops it. A source playing with no
    controls at all is given the benefit of the doubt.
    """
    if controls:
        return bool(controls.get("pause"))
    return state == "playing"


def play_action(state: str | None, last_media_roles: Any) -> str | None:
    """How PLAY should start playback from this state, or None if it can't.

    "resume": the player is paused. pause is a toggle -- sent while paused it
    carries on from the same position -- whereas re-sending the item's roles
    restarts it from the beginning, and a bare play stops it outright.

    "replay": stopped, with an airable item remembered. Its roles are sent
    again; the device keeps no position once stopped, so a podcast starts over
    (live radio has no position to lose).
    """
    if state == "paused":
        return "resume"
    if state != "playing" and last_media_roles:
        return "replay"
    return None


def network_fields(info: Any) -> dict[str, Any]:
    """IP, SSID, BSSID and frequency from network:info.

    network:info carries a type="networkInfo" tag, so get_value has already
    unwrapped it to the inner object (keys: wireless, wired, gateways, ...).
    Its signal level is a cached figure; live RSSI comes from get_rssi.
    """
    if not isinstance(info, dict):
        info = {}
    # Prefer whichever interface is up; a Lumisonic is normally on wireless.
    wired, wifi = info.get("wired") or {}, info.get("wireless") or {}
    iface = wired if wired.get("state") == "up" else wifi
    addrs = iface.get("addresses") or []
    return {
        "ip": next((a.get("ip") for a in addrs if a.get("protocol") == "ipv4"), None),
        "ssid": wifi.get("ssid"),
        "bssid": wifi.get("bssid"),
        "frequency": wifi.get("frequency"),
    }


def _light_update(value: Any) -> dict[str, Any] | None:
    return {"light": value} if isinstance(value, dict) else None


# Leaf nodes the push listener subscribes to, and how each event's value folds
# into coordinator data. Subscribed as itemWithValue, so an event carries the new
# value and applying it needs no read-back.
PUSH_UPDATERS: dict[str, Callable[[Any], dict[str, Any] | None]] = {
    PATH_VOLUME: lambda v: {"volume": v},
    PATH_MUTE: lambda v: {"mute": v},
    PATH_PLAYER_DATA: player_fields,
    PATH_LIGHT: _light_update,
    PATH_CIRCADIAN: lambda v: {"circadian": v},
    PATH_LED_CURFEW: lambda v: {"led_curfew": v},
}


def push_updates(events: list[tuple[str, Any]]) -> tuple[dict[str, Any], bool]:
    """Fold (path, value) events into coordinator-data updates.

    Events apply in order, so the last of a burst (a slider drag yields a dozen)
    wins. Returns the updates and whether a full refresh is still needed: an
    event with no value, or for a path with no updater, can only be resolved by
    reading the device.
    """
    updates: dict[str, Any] = {}
    refresh = False
    for path, value in events:
        updater = PUSH_UPDATERS.get(path)
        update = updater(value) if updater and value is not None else None
        if update is None:
            refresh = True
        else:
            updates.update(update)
    return updates, refresh


class ZumaApi:
    """Talk to one Zuma unit's StreamSDK web API."""

    def __init__(
        self, host: str, session: aiohttp.ClientSession, timeout: float = 8.0
    ) -> None:
        self._host = host
        self._session = session
        self._airable_root: str | None = None
        self._volume_lock = asyncio.Lock()
        self._volume_target = 0
        self._volume_seq = 0
        self._volume_last_write = float("-inf")
        self._volume_verify: asyncio.Task[None] | None = None
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
        """Set volume, clamped to the device's 0-100 range.

        Writes go out one at a time, spaced VOLUME_WRITE_SPACING apart, and the
        newest request wins: a caller that has been overtaken while waiting
        returns without writing, since a later value is about to be sent. A
        slider dragged through 52, 54, 56 thus sends 52 then 56, and the device
        lands on 56 instead of dropping a write and stopping short.

        After the last write of a burst, a background check reads the volume
        back and resends it once if it didn't take (see _verify_volume).
        """
        self._volume_target = max(0, min(VOLUME_MAX, int(volume)))
        self._volume_seq += 1
        mine = self._volume_seq
        if self._volume_verify is not None:
            self._volume_verify.cancel()  # a newer value supersedes the old check
        if await self._write_volume(mine):
            self._volume_verify = asyncio.create_task(self._verify_volume(mine))

    async def _write_volume(self, seq: int) -> bool:
        """Write the current target, spaced from the last write, unless overtaken."""
        async with self._volume_lock:
            loop = asyncio.get_running_loop()
            delay = self._volume_last_write + VOLUME_WRITE_SPACING - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            if seq != self._volume_seq:
                return False  # overtaken; the newest caller writes the newest value
            try:
                await self.set_value(PATH_VOLUME, self._volume_target)
            finally:
                self._volume_last_write = loop.time()
            return True

    async def _verify_volume(self, seq: int) -> None:
        """Once a burst settles, read the volume back; resend once if it was dropped.

        Abandoned if a newer value is requested meanwhile. A read failure is let
        go: the regular poll will show whatever the device has.
        """
        await asyncio.sleep(VOLUME_VERIFY_DELAY)
        if seq != self._volume_seq:
            return
        try:
            actual = await self.get_volume()
        except ZumaError:
            return
        if seq != self._volume_seq or actual == self._volume_target:
            return
        _LOGGER.debug("volume %s didn't take (device has %s); resending", self._volume_target, actual)
        try:
            await self._write_volume(seq)
        except ZumaError as err:
            _LOGGER.debug("volume resend failed: %s", err)

    async def get_mute(self) -> bool | None:
        """Current mute flag."""
        return await self.get_value(PATH_MUTE)

    async def set_mute(self, mute: bool) -> None:
        """Mute or unmute."""
        await self.set_value(PATH_MUTE, bool(mute))

    async def control(self, verb: str) -> Any:
        """Invoke a transport verb on player:player/control.

        Covers the verbs that act on what is already playing. ``play`` is
        deliberately not one: it needs the item's roles, see play_roles. Sent
        bare, the device falls back to "play the current directory" and reports
        "Directory is empty. No playable items found.".
        """
        if verb not in CONTROL_VERBS:
            raise ValueError(f"unknown control verb {verb!r}; have {CONTROL_VERBS}")
        return await self._request(
            "setData", {"path": PATH_CONTROL, "role": "activate", "value": {"control": verb}}
        )

    async def get_rssi(self) -> float | None:
        """Live WiFi signal level in dBm, or None if it can't be sampled.

        An action, not a value: activating the node takes a fresh reading.
        A unit with no wireless link (e.g. on Ethernet) fails the action,
        which is reported as no reading rather than an error.
        """
        try:
            reply = await self._request(
                "setData", {"path": PATH_WIRELESS_RSSI, "role": "activate", "value": None}
            )
        except ZumaError as err:
            _LOGGER.debug("no RSSI reading: %s", err)
            return None
        value = unwrap_item(reply)
        return value if isinstance(value, int | float) else None

    async def play_roles(self, media_roles: dict[str, Any]) -> Any:
        """Start playback of an item, given its roles as the device reported them.

        This is the play half of player:player/control: unlike the transport
        verbs it needs to know *what* to play, and the device wants the item's
        full roles (path, context, mediaData with its prePlayPath, ...) exactly
        as browsing returned them. It resolves the stream itself.
        """
        return await self._request(
            "setData",
            {
                "path": PATH_CONTROL,
                "role": "activate",
                "value": {"control": "play", "playMode": "normal", "mediaRoles": media_roles},
            },
        )

    async def airable_root(self) -> str:
        """The airable browse root, with the account's host. Cached once known."""
        if self._airable_root is None:
            path = unwrap(await self.get_data(PATH_AIRABLE, ("path",)), "path")
            if not isinstance(path, str) or not path.startswith("airable:"):
                raise ZumaError(f"device reported no airable root: {path!r}")
            self._airable_root = path.rstrip("/")
        return self._airable_root

    async def airable_playable_roles(self, path: str) -> dict[str, Any]:
        """The first row an airable path lists, with every role (@all), if playable.

        A station is a playable container whose first row is its audio item, and
        a podcast's episodes list starts with its newest episode; those roles are
        what play_roles takes. A leaf item's own path (a single episode's
        ``/id/airable/feed.episode/<id>``) can't be listed: the device answers
        "Error during communication with server".
        """
        reply = await self._request(
            "getRows",
            {"path": path, "roles": ["@all"], "from": 0, "to": 1, "type": "structure"},
        )
        rows = reply.get("rows") if isinstance(reply, dict) else None
        row = rows[0] if rows else None
        if not isinstance(row, dict) or row.get("type") != "audio":
            raise ZumaError(f"nothing playable at {path}")
        return row

    async def airable_station_roles(self, station: str) -> dict[str, Any]:
        """Look an airable radio station up by id, as the Zuma apps do.

        Takes the bare numeric id or the device's ``airable://airable/radio/<id>``.
        """
        station_id = station.removeprefix(AIRABLE_RADIO_ID_PREFIX)
        root = await self.airable_root()
        return await self.airable_playable_roles(f"{root}/id/airable/radio/{station_id}")

    async def get_player_state(self) -> str | None:
        """Transport state string: stopped / playing / paused."""
        data = await self.get_value(PATH_PLAYER_DATA)
        return data.get("state") if isinstance(data, dict) else None

    # --- event queue (push) ----------------------------------------------

    async def create_event_queue(self, paths: list[str]) -> str:
        """Create a change-notification queue subscribed to leaf nodes.

        Leaf nodes subscribe with type ``itemWithValue`` (containers would use
        ``rows``); the device then pushes ``{"itemType": "update", "path": ...,
        "itemValue": <tagged value>}`` when one changes. Returns the queue id (a
        brace-wrapped UUID) to poll.
        """
        qid = await self._request(
            "event/modifyQueue",
            {"subscribe": [{"path": p, "type": "itemWithValue"} for p in paths]},
        )
        if not isinstance(qid, str):
            raise ZumaError(f"unexpected modifyQueue reply: {qid!r}")
        return qid

    async def poll_events(
        self, queue_id: str, timeout: int = PUSH_POLL_TIMEOUT_SECONDS
    ) -> list[tuple[str, Any]]:
        """Long-poll the queue; return (path, unwrapped value) per change, in order.

        The device holds the connection until a subscribed node changes or
        ``timeout`` *seconds* pass, then answers with an empty list. The client
        timeout sits a little above it so a quiet queue never trips it. The value
        is None if an event carries none (e.g. a removal). A stale queue id yields
        HTTP 400 "Unknown queue id!", surfaced as ZumaError.
        """
        events = await self._request(
            "event/pollQueue",
            {"queueId": queue_id, "timeout": timeout},
            timeout=aiohttp.ClientTimeout(total=timeout + 5),
        )
        if not isinstance(events, list):
            return []
        return [
            (e["path"], unwrap_item(e.get("itemValue")))
            for e in events
            if isinstance(e, dict) and e.get("path")
        ]

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

    async def patch_light(self, fields: dict[str, Any]) -> Any:
        """Change only the given zumaLightState fields; the rest stay as they are.

        Activating the light node with a partial zumaLightState applies it as a
        patch, where a value write (set_light) replaces the whole state. So
        nothing stale is written back: a power-only patch sets power (it is not
        a toggle) and leaves brightness and temperature alone. Without a
        lastTransitionPeriod the device uses its default for the change.
        """
        return await self._request(
            "setData",
            {
                "path": PATH_LIGHT,
                "role": "activate",
                "value": {"type": "zumaLightState", "zumaLightState": fields},
            },
        )

    async def _gather(self, *aws: Awaitable[Any]) -> list[Any]:
        """Run requests concurrently, at most READ_CONCURRENCY in flight at once.

        The device is embedded, so a poll's dozen requests go out a few at a
        time rather than all together. The first failure is raised, as a
        sequential run would.
        """
        limit = asyncio.Semaphore(READ_CONCURRENCY)

        async def one(aw: Awaitable[Any]) -> Any:
            async with limit:
                return await aw

        return await asyncio.gather(*(one(aw) for aw in aws))

    async def get_identity(self) -> dict[str, Any]:
        """Identity for the config flow and device registry.

        ``serial`` is the same UUID the unit publishes in its mDNS TXT record,
        so a manually-added entry and a discovered one resolve to one device.
        """
        keys = ("serial", "name", "version", "model", "manufacturer")
        paths = (PATH_SERIAL, PATH_DEVICE_NAME, PATH_VERSION, PATH_MODEL, PATH_MANUFACTURER)
        return dict(zip(keys, await self._gather(*map(self.get_value, paths)), strict=True))

    async def get_state(self) -> dict[str, Any]:
        """One poll of everything the entities need.

        player:player/data is fetched once and mined for both transport state and
        now-playing metadata (see player_fields). Diagnostics ride along:
        connectivity, live RSSI, thermal mode, accessory and group role.
        """
        (
            volume, mute, player, circadian, led_curfew, light,
            info, rssi, thermal, bezel, master,
        ) = await self._gather(
            self.get_volume(),
            self.get_mute(),
            self.get_value(PATH_PLAYER_DATA),
            self.get_value(PATH_CIRCADIAN),
            self.get_value(PATH_LED_CURFEW),
            self.get_light(),
            self.get_value(PATH_NETWORK_INFO),
            self.get_rssi(),
            self.get_value(PATH_TEMP_MODE),
            self.get_value(PATH_BEZEL),
            self.get_value(PATH_MASTER),
        )
        return {
            "volume": volume,
            "mute": mute,
            **player_fields(player),
            "circadian": circadian,
            "led_curfew": led_curfew,
            "light": light,
            **network_fields(info),
            "rssi": rssi,
            "thermal": thermal,
            "bezel": bezel,
            "master": master,
        }
