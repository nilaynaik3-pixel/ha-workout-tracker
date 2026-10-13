"""Setup screen for Workout Tracker (Settings > Devices & services > Add integration)."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

DOMAIN = "workout_tracker"


async def _user_options(hass):
    users = await hass.auth.async_get_users()
    people = [u for u in users if u.is_active and not u.system_generated]
    owner = next((u.id for u in people if u.is_owner), people[0].id if people else "")
    return [selector.SelectOptionDict(value=u.id, label=u.name or "User") for u in people], owner


def _schema(defaults: dict, users=None) -> vol.Schema:
    fields = {}
    if users:
        fields[vol.Required("owner", default=defaults.get("owner") or users[0]["value"])] = selector.SelectSelector(
            selector.SelectSelectorConfig(options=users, mode=selector.SelectSelectorMode.DROPDOWN))
    fields.update({
        vol.Required("days_per_week", default=defaults.get("days_per_week", 5)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=7, step=1, mode=selector.NumberSelectorMode.SLIDER)),
    })
    zone = defaults.get("gym_zone")
    fields[vol.Optional("gym_zone", **({"description": {"suggested_value": zone}} if zone else {}))] = \
        selector.EntitySelector(selector.EntitySelectorConfig(domain="zone"))
    person = defaults.get("person")
    fields[vol.Optional("person", **({"description": {"suggested_value": person}} if person and person != "auto" else {}))] = \
        selector.EntitySelector(selector.EntitySelectorConfig(domain="person"))
    fields[vol.Required("notify", default=defaults.get("notify", "auto"))] = selector.TextSelector()
    fields[vol.Required("min_visit_minutes", default=defaults.get("min_visit_minutes", 15))] = selector.NumberSelector(
        selector.NumberSelectorConfig(min=1, max=120, step=1, mode=selector.NumberSelectorMode.BOX,
                                      unit_of_measurement="min"))
    return vol.Schema(fields)


def _clean(user_input: dict) -> dict:
    out = dict(user_input)
    out["days_per_week"] = int(out.get("days_per_week", 5))
    out["min_visit_minutes"] = int(out.get("min_visit_minutes", 15))
    out["gym_zone"] = out.get("gym_zone") or ""
    out["person"] = out.get("person") or "auto"
    n = str(out.get("notify") or "").strip()
    out["notify"] = "" if n.lower() in ("", "off", "none", "no") else n
    return out


class WorkoutTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="Workout Tracker", data=_clean(user_input))
        users, owner = await _user_options(self.hass)
        defaults = {"owner": owner}
        zones = [s.entity_id for s in self.hass.states.async_all("zone")
                 if "gym" in s.entity_id or "gym" in str(s.attributes.get("friendly_name", "")).lower()]
        if zones:
            defaults["gym_zone"] = zones[0]
        return self.async_show_form(step_id="user", data_schema=_schema(defaults, users))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WorkoutTrackerOptionsFlow()


class WorkoutTrackerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=_clean(user_input))
        current = {**self.config_entry.data, **self.config_entry.options}
        users, owner = await _user_options(self.hass)
        current.setdefault("owner", owner)
        return self.async_show_form(step_id="init", data_schema=_schema(current, users))
