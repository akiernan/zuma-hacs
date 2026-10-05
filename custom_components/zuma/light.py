"""The Zuma lamp: tunable-white brightness + colour temperature over HTTP."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_TRANSITION,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import LIGHT_MAX_KELVIN, LIGHT_MIN_KELVIN, LIGHT_TRANSITIONS
from .coordinator import ZumaConfigEntry, ZumaCoordinator
from .entity import ZumaEntity


def _nearest_transition(seconds: float) -> str:
    """Map HA's transition (seconds) to the device's fixed millisecond buckets."""
    ms = seconds * 1000
    return LIGHT_TRANSITIONS[min(LIGHT_TRANSITIONS, key=lambda b: abs(b - ms))]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZumaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Zuma light."""
    async_add_entities([ZumaLight(entry.runtime_data)])


class ZumaLight(ZumaEntity, LightEntity):
    """Brightness + colour temperature for one Zuma unit.

    power is its own field: brightness 0 leaves the lamp powered (on but dark), so
    on/off sets `power` and leaves brightness untouched -- that way the lamp comes
    back at the level it had. It only goes one way, though: setting a non-zero
    brightness switches the lamp on.
    """

    _attr_name = None
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_supported_features = LightEntityFeature.TRANSITION
    _attr_min_color_temp_kelvin = LIGHT_MIN_KELVIN
    _attr_max_color_temp_kelvin = LIGHT_MAX_KELVIN

    def __init__(self, coordinator: ZumaCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{self._serial}_light"

    @property
    def _light(self) -> dict[str, Any]:
        return self.coordinator.data.get("light") or {}

    @property
    def available(self) -> bool:
        """Only present the light if the device actually reported its state."""
        return super().available and bool(self._light)

    @property
    def is_on(self) -> bool | None:
        """Power flag; brightness 0 can still read as on."""
        return self._light.get("power")

    @property
    def brightness(self) -> int | None:
        """Device 0-100 mapped to HA's 0-255."""
        pct = self._light.get("brightness")
        return None if pct is None else round(pct * 255 / 100)

    @property
    def color_temp_kelvin(self) -> int | None:
        """Colour temperature in Kelvin, as the device stores it."""
        return self._light.get("temperature")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Power on, applying any brightness / colour-temp / transition given."""
        changes: dict[str, Any] = {}
        if ATTR_BRIGHTNESS in kwargs:
            changes["brightness"] = round(kwargs[ATTR_BRIGHTNESS] * 100 / 255)
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            # Clamp to the advertised range; the device tolerates more but renders poorly.
            changes["temperature"] = max(
                LIGHT_MIN_KELVIN, min(LIGHT_MAX_KELVIN, kwargs[ATTR_COLOR_TEMP_KELVIN])
            )
        await self._set(True, changes, kwargs.get(ATTR_TRANSITION))

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Power off, keeping brightness so it restores on next turn-on."""
        await self._set(False, {}, kwargs.get(ATTR_TRANSITION))

    async def _set(
        self, power: bool, changes: dict[str, Any], transition: float | None
    ) -> None:
        """Send power plus only the fields being changed, as a patch.

        Nothing read from the cache goes back to the device, so a brightness or
        colour change made from the app since the last update is kept, not
        reverted. With no transition the device uses its own default.
        """
        patch = {"power": power, **changes}
        if transition is not None:
            patch["lastTransitionPeriod"] = _nearest_transition(transition)
        await self.coordinator.api.patch_light(patch)
        state = {**self._light, **patch}
        # Reflect the commanded state at once, optimistically. The lamp fades over
        # lastTransitionPeriod and reports the *old* power/brightness until the fade
        # settles, so an immediate read-back flickers (e.g. off -> on -> off on
        # turn-off). Trust the command we just made and let the push event / poll
        # reconcile once the device settles.
        data = dict(self.coordinator.data or {})
        data["light"] = state
        self.coordinator.async_set_updated_data(data)
