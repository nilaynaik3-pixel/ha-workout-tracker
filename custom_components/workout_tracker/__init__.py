"""Workout Tracker for Home Assistant.

Adds a "Workout" page to the sidebar: log sets with progression advice,
weekly overview, calendar editing, body measurements, gym visit tracking and
CSV export. Data lives in a SQLite file in /config/workout_tracker/.
"""

from __future__ import annotations

import logging
import os
import re
import zipfile

import voluptuous as vol
from aiohttp import web

from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import CoreState, Event, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_call_later, async_track_time_change

from .tracker import Hub

_LOGGER = logging.getLogger(__name__)

DOMAIN = "workout_tracker"
VERSION = "1.2.0"
PANEL_URL = "workout"
STATIC_URL = "/workout_tracker/static"
EXPORT_URL = "/workout_tracker/export"
DEMO_ZIP = os.path.join(os.path.dirname(__file__), "demos.zip")
CARD_URL = f"{STATIC_URL}/workout-card.js?v={VERSION}"
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# service name -> (tracker method, accepted fields)
SERVICES = {
    "log_set": ("log_set", ["exercise", "weight", "reps", "rpe", "warmup"]),
    "rpe_all": ("rpe_all", ["exercise", "rpe", "only_missing"]),
    "undo": ("undo", ["exercise"]),
    "set_note": ("set_note", ["exercise", "note"]),
    "select_exercise": ("select_exercise", ["exercise"]),
    "select_main": ("select_main", ["group"]),
    "toggle_addon": ("toggle_addon", ["group", "on"]),
    "suggest": ("suggest", []),
    "add_exercise": ("add_exercise", ["name", "group", "step", "demo"]),
    "set_demo": ("set_demo", ["exercise", "demo"]),
    "rest_start": ("rest_start", ["seconds", "exercise"]),
    "rest_stop": ("rest_stop", []),
    "rest_add": ("rest_add", ["seconds"]),
    "rest_settings": ("rest_settings", ["seconds", "notify"]),
    "hide_exercise": ("hide_exercise", ["exercise"]),
    "set_day": ("set_day", ["day"]),
    "open_day": ("open_day_", ["day"]),
    "update_set": ("update_set", ["set_id", "weight", "reps", "rpe", "warmup"]),
    "delete_set": ("delete_set", ["set_id"]),
    "add_set": ("add_set", ["day", "exercise", "weight", "reps", "rpe"]),
    "log_body": ("log_body", ["metric", "value", "day"]),
    "delete_body": ("delete_body", ["entry_id"]),
    "gym_start": ("gym_arrived", []),
    "gym_end": ("gym_left", []),
    "export": ("export", []),
    "refresh": ("refresh", []),
}


class HassHost:
    """The few things the tracker needs from Home Assistant."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def run(self, func, *args):
        return await self.hass.async_add_executor_job(func, *args)

    def entity_ids(self, domain):
        return self.hass.states.async_entity_ids(domain)

    def attributes(self, entity_id):
        st = self.hass.states.get(entity_id) if entity_id else None
        return dict(st.attributes) if st else None

    def has_service(self, domain, service):
        return self.hass.services.has_service(domain, service)

    async def call_service(self, domain, service, data):
        await self.hass.services.async_call(domain, service, data, blocking=False)

    def fire(self, event_type, data):
        self.hass.bus.async_fire(event_type, data)

    def later(self, seconds, coro_fn):
        """Run coro_fn() after `seconds`; returns a cancel function."""
        async def fire(_now):
            await coro_fn()
        return async_call_later(self.hass, seconds, fire)


class DemoView(HomeAssistantView):
    """Exercise pictures, served straight from demos.zip.

    Public like the rest of the static files: <img> tags can't send a token,
    and the pictures are the same for every install.
    """

    url = "/workout_tracker/demo/{ex_id}/{frame}"
    name = "workout_tracker:demo"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._zip = None

    def _read(self, member):
        if self._zip is None:
            self._zip = zipfile.ZipFile(DEMO_ZIP)
        try:
            return self._zip.read(member)
        except KeyError:
            return None

    async def get(self, request, ex_id, frame):
        if not re.fullmatch(r"[A-Za-z0-9_\-]{1,120}", ex_id) or frame not in ("0.webp", "1.webp"):
            return web.Response(status=404)
        data = await self.hass.async_add_executor_job(self._read, f"{ex_id}/{frame}")
        if data is None:
            return web.Response(status=404)
        return web.Response(body=data, content_type="image/webp",
                            headers={"Cache-Control": "public, max-age=31536000, immutable"})


def _paths(hass: HomeAssistant):
    data_dir = hass.config.path("workout_tracker")
    db = os.path.join(data_dir, "workout.db")
    config = os.path.join(os.path.dirname(__file__), "exercises.yaml")
    # Earlier Pyscript version of this tracker: keep using its data.
    legacy_db = hass.config.path("workout", "workout.db")
    legacy_cfg = hass.config.path("workout", "exercises.yaml")
    if not os.path.exists(db) and os.path.exists(legacy_db):
        db = legacy_db
        if os.path.exists(legacy_cfg):
            config = legacy_cfg
    custom_cfg = os.path.join(data_dir, "exercises.yaml")
    if os.path.exists(custom_cfg):
        config = custom_cfg
    return data_dir, db, config, os.path.join(data_dir, "export")


async def async_setup(hass: HomeAssistant, config) -> bool:
    hass.data.setdefault(DOMAIN, {"listeners": {}, "hub": None})
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_profiles)
    return True


async def _users(hass: HomeAssistant):
    """People who can have a profile: active, non-system Home Assistant users."""
    users = await hass.auth.async_get_users()
    return [u for u in users if u.is_active and not u.system_generated]


@websocket_api.websocket_command({vol.Required("type"): "workout_tracker/profiles"})
@websocket_api.async_response
async def ws_profiles(hass: HomeAssistant, connection, msg) -> None:
    """The profiles to choose from, and which one is the current user's."""
    hub = (hass.data.get(DOMAIN) or {}).get("hub")
    users = await _users(hass)
    connection.send_result(msg["id"], {
        "me": connection.user.id if connection.user else None,
        "owner": hub.owner_id if hub else None,
        "profiles": [{"id": u.id, "name": u.name or "User",
                      "has_data": bool(hub and hub.has_data(u.id))} for u in users],
    })


@websocket_api.websocket_command({
    vol.Required("type"): "workout_tracker/subscribe",
    vol.Optional("profile"): vol.Any(str, None),
})
@websocket_api.async_response
async def ws_subscribe(hass: HomeAssistant, connection, msg) -> None:
    """Stream one profile's data to a card: everything now, then on every change.
    Without a profile, the logged-in user's own profile is used."""
    store = hass.data.get(DOMAIN) or {}
    hub = store.get("hub")
    uid = msg.get("profile") or (connection.user.id if connection.user else None)
    if hub is not None:
        uid = hub.profile(uid)
    listeners = store.setdefault("listeners", {}).setdefault(uid or "owner", set())

    @callback
    def forward(bundle):
        connection.send_message(websocket_api.event_message(msg["id"], {"profile": uid, **bundle}))

    listeners.add(forward)

    @callback
    def unsub():
        listeners.discard(forward)

    connection.subscriptions[msg["id"]] = unsub
    connection.send_result(msg["id"])
    if hub is not None:
        tracker = await hub.get(uid)
        forward(tracker.bundle)


async def _register_frontend(hass: HomeAssistant) -> None:
    store = hass.data[DOMAIN]
    if not store.get("static"):
        www = os.path.join(os.path.dirname(__file__), "www")
        _, _, _, export_dir = _paths(hass)
        await hass.async_add_executor_job(lambda: os.makedirs(export_dir, exist_ok=True))
        await hass.http.async_register_static_paths([
            StaticPathConfig(STATIC_URL, www, False),
            StaticPathConfig(EXPORT_URL, export_dir, False),
        ])
        # Load the cards everywhere, so they can also be used on any dashboard.
        frontend.add_extra_js_url(hass, CARD_URL)
        hass.http.register_view(DemoView(hass))
        store["static"] = True
    if PANEL_URL not in hass.data.get(frontend.DATA_PANELS, {}):
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL,
            webcomponent_name="workout-tracker-panel",
            sidebar_title="Workout",
            sidebar_icon="mdi:dumbbell",
            module_url=CARD_URL,
            require_admin=False,
            config={},
        )


async def _default_owner(hass: HomeAssistant) -> str:
    """Whose is the data that already exists? The Home Assistant owner by default."""
    users = await _users(hass)
    for u in users:
        if u.is_owner:
            return u.id
    return users[0].id if users else ""


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    store = hass.data.setdefault(DOMAIN, {"listeners": {}, "hub": None})
    data_dir, db, config, export_dir = _paths(hass)
    await hass.async_add_executor_job(lambda: os.makedirs(data_dir, exist_ok=True))

    options = {**entry.data, **entry.options}
    owner_id = options.get("owner") or await _default_owner(hass)
    hub = Hub(HassHost(hass), data_dir, db, config, export_dir, EXPORT_URL, options, owner_id,
              listeners=store.setdefault("listeners", {}))
    store["hub"] = hub

    await _register_frontend(hass)

    # ---- services: each acts on one person's profile
    def make_handler(method, fields):
        async def handler(call: ServiceCall) -> None:
            kwargs = {k: call.data[k] for k in fields if k in call.data}
            uid = call.data.get("profile") or call.context.user_id
            try:
                tracker = await hub.get(uid)
                await getattr(tracker, method)(**kwargs)
            except (ValueError, TypeError, KeyError) as err:
                raise HomeAssistantError(str(err)) from err
        return handler

    for name, (method, fields) in SERVICES.items():
        hass.services.async_register(DOMAIN, name, make_handler(method, fields),
                                     schema=vol.Schema({}, extra=vol.ALLOW_EXTRA))

    # ---- start once HA is running (other integrations' entities exist by then)
    unsubs = []

    async def start(_event=None):
        users = await _users(hass)
        await hub.start_known([u.id for u in users])
        watched = hub.people()
        if watched:
            _LOGGER.info("workout_tracker: gym tracking on for %s", ", ".join(watched))

    if hass.state is CoreState.running:
        await start()
    else:
        fired = False

        async def on_started(event):
            nonlocal fired
            fired = True
            await start(event)

        remove = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, on_started)

        @callback
        def cancel_start() -> None:
            if not fired:   # removing a listener that already ran logs an error
                remove()

        unsubs.append(cancel_start)

    # ---- gym arrival/departure for every profile, by its person entity
    @callback
    def is_person(event_data) -> bool:
        return str(event_data.get("entity_id", "")).startswith("person.")

    @callback
    def person_moved(event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None or old.state == new.state:
            return
        hass.async_create_task(hub.person_changed(event.data["entity_id"], old.state, new.state))

    unsubs.append(hass.bus.async_listen(EVENT_STATE_CHANGED, person_moved, event_filter=is_person))

    @callback
    def at_midnight(_now) -> None:
        hass.async_create_task(hub.midnight())

    unsubs.append(async_track_time_change(hass, at_midnight, hour=0, minute=5, second=0))

    for u in unsubs:
        entry.async_on_unload(u)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    for name in SERVICES:
        hass.services.async_remove(DOMAIN, name)
    store = hass.data.get(DOMAIN, {})
    store["hub"] = None
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Integration deleted: take the page out of the sidebar (the data stays)."""
    frontend.async_remove_panel(hass, PANEL_URL)
