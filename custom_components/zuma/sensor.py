"""Read-only diagnostic sensors for a Zuma unit."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import THERMAL_MODES
from .coordinator import ZumaConfigEntry, ZumaCoordinator
from .entity import ZumaEntity


@dataclass(frozen=True, kw_only=True)
class ZumaSensorDescription(SensorEntityDescription):
    """A sensor whose value comes from a coordinator-data key or the identity dict."""

    value_fn: Callable[[ZumaCoordinator], Any]
    attrs_fn: Callable[[ZumaCoordinator], dict[str, Any]] | None = None


SENSORS: tuple[ZumaSensorDescription, ...] = (
    ZumaSensorDescription(
        key="wifi_signal",
        translation_key="wifi_signal",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.data.get("rssi"),
        attrs_fn=lambda c: {
            k: c.data.get(k) for k in ("ssid", "bssid", "frequency") if c.data.get(k)
        },
    ),
    ZumaSensorDescription(
        key="ip_address",
        translation_key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: c.data.get("ip"),
    ),
    ZumaSensorDescription(
        key="firmware",
        translation_key="firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.identity.get("version"),
    ),
    ZumaSensorDescription(
        key="thermal_mode",
        translation_key="thermal_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(THERMAL_MODES.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        # An unrecognised mode reads as unknown: an ENUM sensor may only report
        # one of its options, and anything else would fail the state write.
        value_fn=lambda c: THERMAL_MODES.get(c.data.get("thermal")),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZumaConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Zuma diagnostic sensors."""
    coordinator = entry.runtime_data
    async_add_entities(ZumaSensor(coordinator, desc) for desc in SENSORS)


class ZumaSensor(ZumaEntity, SensorEntity):
    """One diagnostic value."""

    entity_description: ZumaSensorDescription

    def __init__(
        self, coordinator: ZumaCoordinator, description: ZumaSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{self._serial}_{description.key}"

    @property
    def native_value(self) -> Any:
        """Current reading."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Optional extra attributes (e.g. SSID/BSSID on the signal sensor)."""
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator) or None
