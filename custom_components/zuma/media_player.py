"""Media player for a Zuma unit: volume, mute and transport state."""

from __future__ import annotations

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
import aiohttp

from homeassistant.components import media_source
from homeassistant.components.media_player import async_process_play_media_url
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ZumaError, play_action
from .const import AIRABLE_RADIO_ID_PREFIX, VOLUME_MAX
from .dlna import discover_avtransport, play_url, probe_mime
from .coordinator import ZumaConfigEntry, ZumaCoordinator
from .entity import ZumaEntity

# The device reports transport state as a bare string on player:player/data.
STATE_MAP = {
    "stopped": MediaPlayerState.IDLE,
    "playing": MediaPlayerState.PLAYING,
    "paused": MediaPlayerState.PAUSED,
    "transitioning": MediaPlayerState.BUFFERING,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZumaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Zuma media player."""
    async_add_entities([ZumaMediaPlayer(entry.runtime_data)])


class ZumaMediaPlayer(ZumaEntity, MediaPlayerEntity):
    """Volume, mute and transport for one Zuma unit.

    Playing needs the item, not just a verb: the device takes a play command only
    with the item's full roles attached. So PLAY resumes the last airable station
    or podcast seen playing, and play_media starts an airable item by its device
    id or path. Other sources (AirPlay, Spotify Connect, TIDAL Connect) are
    started from their apps; this entity then controls them.
    """

    _attr_name = None
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _BASE_FEATURES = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.PLAY_MEDIA
        | MediaPlayerEntityFeature.BROWSE_MEDIA
    )

    def __init__(self, coordinator: ZumaCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = self._serial

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Volume and pause/stop always; skip/prev only when the stream allows them.

        The control node accepts `next`/`previous` for any stream, but on a live
        broadcast they do nothing -- the device says so via controls.next_ and
        controls.previous, so trust that rather than advertising dead buttons.
        """
        features = self._BASE_FEATURES
        if self._play_action() is not None:
            features |= MediaPlayerEntityFeature.PLAY
        controls = self.coordinator.data.get("controls") or {}
        if controls.get("next_"):
            features |= MediaPlayerEntityFeature.NEXT_TRACK
        if controls.get("previous"):
            features |= MediaPlayerEntityFeature.PREVIOUS_TRACK
        return features

    @property
    def state(self) -> MediaPlayerState | None:
        """Transport state, or None if the device reported something unknown."""
        return STATE_MAP.get(self.coordinator.data.get("state"))

    @property
    def volume_level(self) -> float | None:
        """Volume as HA wants it: 0.0-1.0."""
        volume = self.coordinator.data.get("volume")
        return None if volume is None else volume / VOLUME_MAX

    @property
    def is_volume_muted(self) -> bool | None:
        """Whether the unit is muted."""
        return self.coordinator.data.get("mute")

    @property
    def media_title(self) -> str | None:
        """Station or track name, whatever source is playing."""
        return self.coordinator.data.get("title")

    @property
    def media_image_url(self) -> str | None:
        """Artwork URL as the device reports it."""
        return self.coordinator.data.get("image")

    @property
    def media_content_type(self) -> MediaType | None:
        """Everything this device plays is audio."""
        return MediaType.MUSIC if self.coordinator.data.get("title") else None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Expose which backend is playing, e.g. airableRadios."""
        source = self.coordinator.data.get("source")
        return {"zuma_service": source} if source else None

    def _play_action(self) -> str | None:
        return play_action(
            self.coordinator.data.get("state"), self.coordinator.last_media_roles
        )

    async def async_media_play(self) -> None:
        """Resume where a pause left off, or restart the last airable item."""
        action = self._play_action()
        if action == "resume":
            await self._control("pause")  # a toggle: resumes when paused
        elif action == "replay":
            await self.coordinator.api.play_roles(self.coordinator.last_media_roles)
            await self.coordinator.async_request_refresh()
        else:
            raise HomeAssistantError("Nothing to resume: start a station first")

    async def async_media_pause(self) -> None:
        """Pause. Live streams report `stopped` rather than `paused` afterwards."""
        # pause toggles, so on an already-paused player it would resume instead.
        if self.coordinator.data.get("state") == "paused":
            return
        await self._control("pause")

    async def async_media_stop(self) -> None:
        """Stop playback."""
        await self._control("stop")

    async def async_media_next_track(self) -> None:
        """Next item in whatever list the device is playing."""
        await self._control("next")

    async def async_media_previous_track(self) -> None:
        """Previous item in whatever list the device is playing."""
        await self._control("previous")

    async def _control(self, verb: str) -> None:
        await self.coordinator.api.control(verb)
        await self.coordinator.async_request_refresh()

    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        """Let HA's media browser feed URLs (and TTS/radio media sources) in."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )

    async def async_play_media(
        self, media_type: str, media_id: str, **kwargs
    ) -> None:
        """Play an airable item natively, or any stream URL via DLNA.

        Airable items are named by the device's own station id
        (``airable://airable/radio/<id>``) or by an ``airable:`` path whose first row plays; the
        device looks them up and resolves the stream itself.

        Anything else is a URL: the nsdk API can't play an arbitrary URL, but the
        Rygel renderer on the same box can. It only accepts what its sink list
        allows -- MP3 and clean AAC play; ICF/ICY `audio/aacp` streams (e.g.
        streamtheworld `.aac` mounts) are refused, so use the `.mp3` mount.
        """
        if media_id.startswith("airable:"):
            await self._play_airable(media_id)
            return

        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(
                self.hass, media_id, self.entity_id
            )
            media_id = item.url
        media_id = async_process_play_media_url(self.hass, media_id)

        session = self.coordinator.api._session  # noqa: SLF001 -- reuse HA's session
        mime = await probe_mime(media_id, session)

        async def _attempt() -> None:
            if not self.coordinator.avtransport_url:
                self.coordinator.avtransport_url = await discover_avtransport(
                    self.coordinator.api.host, session
                )
            if not self.coordinator.avtransport_url:
                raise HomeAssistantError("Could not find the Zuma DLNA renderer")
            await play_url(
                session, self.coordinator.avtransport_url, media_id, "Zuma stream", mime
            )

        try:
            await _attempt()
        except (aiohttp.ClientError, HomeAssistantError):
            # Port likely rotated; forget it and rediscover once.
            self.coordinator.avtransport_url = None
            await _attempt()
        await self.coordinator.async_request_refresh()

    async def _play_airable(self, media_id: str) -> None:
        api = self.coordinator.api
        try:
            if media_id.startswith(AIRABLE_RADIO_ID_PREFIX):
                roles = await api.airable_station_roles(media_id)
            else:
                roles = await api.airable_playable_roles(media_id)
            await api.play_roles(roles)
        except ZumaError as err:
            raise HomeAssistantError(f"Cannot play {media_id}: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_set_volume_level(self, volume: float) -> None:
        """Set volume, converting HA's 0.0-1.0 to the device's 0-100."""
        await self.coordinator.api.set_volume(round(volume * VOLUME_MAX))
        await self.coordinator.async_request_refresh()

    async def async_mute_volume(self, mute: bool) -> None:
        """Mute or unmute."""
        await self.coordinator.api.set_mute(mute)
        await self.coordinator.async_request_refresh()
