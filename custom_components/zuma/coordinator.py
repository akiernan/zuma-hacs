"""Coordinator for a single Zuma unit: push via the event queue, polling as fallback."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PUSH_UPDATERS, ZumaApi, ZumaError, push_updates
from .const import DOMAIN, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)

type ZumaConfigEntry = ConfigEntry[ZumaCoordinator]

# After a push error (queue lost, device rebooting), wait this long before rebuilding.
_PUSH_RETRY_SECONDS = 5.0


class ZumaCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Refresh device state, driven by the device's own change events.

    The device exposes a long-poll event queue: subscribe to the fast-changing
    leaf nodes, then a poll blocks until one of them changes and returns its new
    value. Values are applied straight into coordinator data, so a volume/light
    change made from the app or the unit shows up in HA within about a second.
    Polling stays on as a slow safety net (and to catch the rare-change
    diagnostics the push set does not subscribe to).
    """

    def __init__(
        self, hass: HomeAssistant, entry: ZumaConfigEntry, api: ZumaApi
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {api.host}",
            update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS),
            config_entry=entry,
        )
        self.api = api
        # Filled in once during setup; entities read it to build device_info.
        self.identity: dict[str, Any] = {}
        # Cached DLNA AVTransport control URL (ephemeral port; re-discovered on failure).
        self.avtransport_url: str | None = None
        # The last airable item seen playing, kept so PLAY can resume it: the
        # device drops mediaRoles from player:player/data as soon as it stops.
        self.last_media_roles: dict[str, Any] | None = None

    @callback
    def async_update_listeners(self) -> None:
        """Remember what is playing before entities see the new data.

        Every update -- a full refresh or a pushed value -- passes through here.
        Only airable items are kept: re-sending their roles is known to restart
        them, while other sources (AirPlay, Spotify Connect) are driven from the
        sending app and have not been shown to replay.
        """
        roles = (self.data or {}).get("media_roles")
        if isinstance(roles, dict) and str(roles.get("path", "")).startswith("airable:"):
            self.last_media_roles = roles
        super().async_update_listeners()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.api.get_state()
        except ZumaError as err:
            raise UpdateFailed(str(err)) from err

    async def async_run_push_listener(self) -> None:
        """Long-poll the event queue forever, applying each change as it arrives.

        Runs as a background task for the life of the config entry. Resilient by
        design: an idle poll returns empty and is simply repeated; any real error
        drops the queue id and rebuilds it after a short wait, then resyncs.
        """
        queue_id: str | None = None
        # Set when a queue is lost: anything queued on it died with it, so the
        # rebuilt queue starts with a full refresh rather than trusting stale data.
        resync = False
        while True:
            try:
                if queue_id is None:
                    queue_id = await self.api.create_event_queue(list(PUSH_UPDATERS))
                    _LOGGER.debug("%s: event queue %s", self.name, queue_id)
                    if resync:
                        resync = False
                        await self.async_request_refresh()
                events = await self.api.poll_events(queue_id)
                if events:
                    _LOGGER.debug("%s: push %s", self.name, events)
                    updates, refresh = push_updates(events)
                    if updates and self.data is not None:
                        self.async_set_updated_data({**self.data, **updates})
                    if refresh:
                        await self.async_request_refresh()
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                # The device should answer an idle poll itself; a client timeout means
                # it didn't, so keep the queue and poll again.
                continue
            except Exception as err:  # noqa: BLE001 -- keep the loop alive on any fault
                _LOGGER.debug("%s: push reset (%s)", self.name, err)
                queue_id = None
                resync = True
                await asyncio.sleep(_PUSH_RETRY_SECONDS)
