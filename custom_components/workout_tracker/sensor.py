"""Steps and calories as Home Assistant sensors, for dashboards and automations.

They follow the owner's profile (the one chosen in the setup screen), the same
numbers as the Activity section of the Overview tab.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

DOMAIN = "workout_tracker"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    store = hass.data[DOMAIN]
    hub = store["hub"]
    uid = hub.profile(hub.owner_id or None)
    listeners = store.setdefault("listeners", {}).setdefault(uid, set())
    entities = [StepsSensor(entry), CaloriesSensor(entry)]
    add(entities)

    @callback
    def on_bundle(bundle):
        part = (bundle or {}).get("activity") or {}
        a = part.get("attributes") or {}
        if not a:
            return
        for ent in entities:
            ent.take(a)

    listeners.add(on_bundle)
    entry.async_on_unload(lambda: listeners.discard(on_bundle))
    # the tracker may already be running (integration reloaded): show its numbers now
    tracker = hub.trackers.get(uid)
    if tracker is not None:
        on_bundle(tracker.bundle)


class _Base(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)}, name="Workout Tracker",
            manufacturer="Workout Tracker", entry_type=DeviceEntryType.SERVICE)
        self._a = None

    @property
    def available(self) -> bool:
        return self._a is not None

    @callback
    def take(self, a: dict) -> None:
        self._a = a
        if self.hass is not None:
            self.async_write_ha_state()


class StepsSensor(_Base):
    _attr_name = "Steps today"
    _attr_icon = "mdi:shoe-print"
    _attr_native_unit_of_measurement = "steps"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "steps_today")

    @property
    def native_value(self):
        return self._a["steps"] if self._a else None

    @property
    def extra_state_attributes(self):
        if not self._a:
            return None
        h = self._a.get("history") or {}
        return {"goal": self._a.get("goal"), "percent": self._a.get("percent"),
                "average_7_days": h.get("avg_steps_7d"), "goal_days_7_days": h.get("goal_days_7d"),
                "sources": (self._a.get("sources") or {}).get("steps")}


class CaloriesSensor(_Base):
    """Active calories only (walking + gym), not the resting burn."""
    _attr_name = "Active calories today"
    _attr_icon = "mdi:fire"
    _attr_native_unit_of_measurement = "kcal"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "calories_today")

    @property
    def native_value(self):
        return (self._a.get("kcal") or {}).get("total") if self._a else None

    @property
    def extra_state_attributes(self):
        if not self._a:
            return None
        k = self._a.get("kcal") or {}
        return {"walking": k.get("walking"), "gym": k.get("gym"),
                "gym_minutes": self._a.get("gym_minutes"), "gym_based_on": self._a.get("gym_basis"),
                "watch_active": self._a.get("watch_kcal"),
                "average_7_days": (self._a.get("history") or {}).get("avg_kcal_7d"),
                "missing": self._a.get("missing") or None}
