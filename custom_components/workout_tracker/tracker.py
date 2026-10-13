"""Workout Tracker: the app logic, independent of Home Assistant.

`Tracker` holds today's selection (main group, add-ons, exercise), runs every
action the cards can ask for, and publishes a "bundle" of everything the
cards draw. It talks to Home Assistant only through a small `host` object
(see __init__.py), which keeps this file testable on its own.

Bundle keys (each {"state", "attributes", "last_updated"}):
  plan      today's suggestion, current selection, gym visit
  view      exercise cards for the selected main group + add-ons
  selected  the selected exercise: advice, today's sets, note, record
  day       the day opened in the Overview calendar
  stats     everything the Overview tab draws
  export    the latest CSV export
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from . import core as wc

_LOGGER = logging.getLogger(__name__)
HISTORY = 3
PARTS = ("plan", "view", "selected", "day", "stats", "export")


def _truthy(v):
    return v in (True, "true", "on", 1, "1", "True")


class Tracker:
    def __init__(self, host, db_path, config_path, export_dir, export_url, options, listeners=None,
                 user_id=None, owner=True):
        self.host = host
        self.user_id = user_id      # Home Assistant user this profile belongs to
        self.owner = owner          # the profile that also gets the setup-screen person
        self.db = db_path
        self.config_path = config_path
        self.export_dir = export_dir
        self.export_url = export_url
        self.options = dict(options or {})
        self.cfg = {"main": [], "addon": [], "groups": [], "exercises": {}, "settings": {}}
        self.person = ""
        self.notify = ""
        self.main = None
        self.addons = []
        self.exercise = None
        self.log_day = None
        self.open_day = None
        self.last_pr = None
        self.export_info = None
        self.bundle = {k: {"state": "none", "attributes": {}, "last_updated": "0"} for k in PARTS}
        self._version = 0
        # shared with the websocket layer so open cards survive a reload
        self._listeners = listeners if listeners is not None else set()

    # ------------------------------------------------------------ plumbing
    async def run(self, func, *args):
        return await self.host.run(func, *args)

    def subscribe(self, cb):
        self._listeners.add(cb)
        return lambda: self._listeners.discard(cb)

    def _set(self, part, state, attributes):
        self._version += 1
        self.bundle[part] = {"state": str(state), "attributes": attributes,
                             "last_updated": str(self._version)}

    def _push(self):
        for cb in list(self._listeners):
            try:
                cb(self.bundle)
            except Exception:  # a closed connection must not break the others
                _LOGGER.exception("workout_tracker: listener failed")

    # -------------------------------------------------------------- config
    async def load(self):
        self.cfg = await self.run(wc.load_config, self.config_path, self.db)
        s = self.cfg["settings"]
        for key in ("person", "gym_zone", "notify", "dashboard_url"):
            if key in self.options:
                s[key] = str(self.options.get(key) or "").strip()
        for key in ("days_per_week", "min_visit_minutes"):
            if key in self.options and self.options[key] not in (None, ""):
                s[key] = int(self.options[key])
        self._resolve()

    def _resolve(self):
        """Turn person/notify 'auto' into real names."""
        s = self.cfg["settings"]
        person = s.get("person", "") if self.owner else "auto"
        linked = ""
        if self.user_id:   # the person entity linked to this Home Assistant user
            for p in sorted(self.host.entity_ids("person")):
                if (self.host.attributes(p) or {}).get("user_id") == self.user_id:
                    linked = p
                    break
        if person in ("", "auto") and linked:
            person = linked
        elif person in ("", "auto") and not self.owner:
            person = ""
        elif person in ("", "auto"):
            people = sorted(self.host.entity_ids("person"))
            person = ""
            if len(people) == 1:
                person = people[0]
            else:
                for p in people:
                    trackers = (self.host.attributes(p) or {}).get("device_trackers") or []
                    if any("iphone" in t.lower() or "phone" in t.lower() for t in trackers):
                        person = p
                        break
                if not person and people:
                    person = people[0]
        notify = s.get("notify", "")
        if notify == "auto":
            notify = ""
            for t in (self.host.attributes(person) or {}).get("device_trackers") or [] if person else []:
                name = t.split(".", 1)[1]
                if self.host.has_service("notify", f"mobile_app_{name}"):
                    notify = f"notify.mobile_app_{name}"
                    break
        self.person, self.notify = person, notify
        _LOGGER.info("workout_tracker: person=%s notify=%s", person or "-", notify or "-")

    @property
    def gym_zone(self):
        return self.cfg["settings"].get("gym_zone", "")

    def gym_zone_name(self):
        z = self.gym_zone
        if not z:
            return ""
        attrs = self.host.attributes(z) or {}
        return str(attrs.get("friendly_name") or z.split(".", 1)[-1])

    # ------------------------------------------------------- publishing
    def _groups(self):
        return [self.main] + [g for g in self.cfg["addon"] if g in self.addons] if self.main else list(self.addons)

    async def publish(self, parts=PARTS):
        parts = set(parts)
        if "plan" in parts:
            p = await self.run(wc.plan, self.db, self.cfg)
            visit = await self.run(wc.open_visit, self.db)
            p.update({
                "current_main": self.main,
                "current_addons": list(self.addons),
                "gym_visit": visit,
                "gym_tracking": bool(self.person and self.gym_zone and self.host.attributes(self.gym_zone) is not None),
                "gym_person": self.person,
                "gym_notify": self.notify,
            })
            self._set("plan", (p["next_main"] or "none").title(), p)
        if "view" in parts or "selected" in parts:
            groups = self._groups()
            cards = await self.run(wc.group_view, self.db, self.cfg, groups, HISTORY, self.log_day)
            names = [c["name"] for c in cards]
            if self.exercise not in names:
                self.exercise = names[0] if names else None
            if "view" in parts:
                self._set("view", " + ".join(g.title() for g in groups),
                          {"groups": groups, "cards": cards})
        if "selected" in parts:
            ex = self.cfg["exercises"].get(self.exercise) if self.exercise else None
            if ex is None:
                self._set("selected", "none", {})
            else:
                card = await self.run(wc.exercise_card, self.db, self.exercise, ex, HISTORY, self.log_day)
                card["log_day"] = self.log_day
                card["pr"] = self.last_pr if (self.last_pr and self.last_pr["exercise"] == self.exercise) else None
                self._set("selected", self.exercise, card)
        if "stats" in parts:
            st = await self.run(wc.stats, self.db, self.cfg)
            self._set("stats", st["workouts_this_week"], st)
        if "day" in parts:
            if self.open_day:
                d = await self.run(wc.day_detail, self.db, self.cfg, self.open_day)
                d["catalog"] = [{"group": g, "exercises": wc.visible_exercises(self.cfg, [g])}
                                for g in self.cfg["groups"] if wc.visible_exercises(self.cfg, [g])]
                self._set("day", self.open_day, d)
            else:
                self._set("day", "none", {})
        if "export" in parts:
            if self.export_info:
                self._set("export", self.export_info["created"], {"files": self.export_info["files"]})
            else:
                self._set("export", "unknown", {"files": []})
        self._push()

    async def apply_suggestion(self):
        p = await self.run(wc.plan, self.db, self.cfg)
        self.main = p["next_main"]
        self.addons = [p["next_addon"]] if p["next_addon"] else []

    async def start(self):
        await self.load()
        await self.apply_suggestion()
        await self.publish()

    # ------------------------------------------------------- selection
    async def select_main(self, group):
        g = str(group or "").lower()
        if g in self.cfg["main"]:
            self.main = g
            await self.publish(("plan", "view", "selected"))

    async def toggle_addon(self, group, on=None):
        g = str(group or "").lower()
        if g not in self.cfg["addon"]:
            return
        want = (g not in self.addons) if on is None else _truthy(on)
        if want and g not in self.addons:
            self.addons.append(g)
        elif not want and g in self.addons:
            self.addons.remove(g)
        await self.publish(("plan", "view", "selected"))

    async def select_exercise(self, exercise):
        if exercise in self.cfg["exercises"]:
            self.exercise = exercise
            await self.publish(("selected",))

    async def suggest(self):
        await self.apply_suggestion()
        await self.publish(("plan", "view", "selected"))

    # ------------------------------------------------------- logging
    async def log_set(self, exercise=None, weight=0, reps=1, rpe=0, warmup=False):
        exercise = exercise or self.exercise
        if exercise not in self.cfg["exercises"]:
            _LOGGER.warning("workout_tracker: unknown exercise %s", exercise)
            return
        kind = "warmup" if _truthy(warmup) else "work"
        await self.run(wc.log_set, self.db, exercise, float(weight), int(float(reps)),
                       float(rpe or 0), self.log_day, None, kind)
        if kind == "work":
            await self._check_pr(exercise)
        await self.publish()

    async def rpe_all(self, exercise=None, rpe=0, only_missing=False):
        exercise = exercise or self.exercise
        if not rpe or float(rpe) <= 0:
            return
        await self.run(wc.set_rpe_all, self.db, exercise, float(rpe), self.log_day, _truthy(only_missing))
        await self.publish(("selected", "view", "stats", "day"))

    async def undo(self, exercise=None):
        await self.run(wc.undo_last_set, self.db, exercise)
        await self.publish()

    async def set_note(self, exercise=None, note=""):
        exercise = exercise or self.exercise
        await self.run(wc.set_note, self.db, exercise, note or "")
        await self.publish(("selected", "view"))

    async def set_day(self, day=None):
        d = str(day or "today").strip().lower()
        if d in ("", "today", "none"):
            self.log_day = None
        elif d == "yesterday":
            self.log_day = (date.today() - timedelta(days=1)).isoformat()
        else:
            parsed = date.fromisoformat(d)
            if parsed > date.today():
                return
            self.log_day = None if parsed == date.today() else parsed.isoformat()
        await self.publish(("view", "selected"))

    async def _check_pr(self, exercise):
        pr = await self.run(wc.check_pr, self.db, exercise)
        if not pr:
            return
        self.last_pr = pr
        self.host.fire("workout_pr", pr)

    # ------------------------------------------------------- exercises
    async def add_exercise(self, name, group, step=2.5):
        name = await self.run(wc.add_exercise, self.db, name, group, float(step))
        await self.load()
        if self.cfg["exercises"].get(name, {}).get("group") in self._groups():
            self.exercise = name
        await self.publish()

    async def hide_exercise(self, exercise=None):
        exercise = exercise or self.exercise
        if exercise in self.cfg["exercises"]:
            await self.run(wc.hide_exercise, self.db, exercise, True)
            await self.load()
            await self.publish()

    # ------------------------------------------------------- calendar editing
    async def open_day_(self, day=None):
        d = str(day or "").strip()
        self.open_day = date.fromisoformat(d).isoformat() if d and d != "none" else None
        await self.publish(("day",))

    async def update_set(self, set_id, weight=None, reps=None, rpe=None, warmup=None):
        kind = None if warmup is None else ("warmup" if _truthy(warmup) else "work")
        await self.run(wc.update_set, self.db, int(set_id),
                       None if weight is None else float(weight),
                       None if reps is None else int(float(reps)),
                       "keep" if rpe is None else float(rpe), kind)
        await self.publish()

    async def delete_set(self, set_id):
        await self.run(wc.delete_set, self.db, int(set_id))
        await self.publish()

    async def add_set(self, day, exercise, weight=None, reps=None, rpe=None):
        if exercise not in self.cfg["exercises"]:
            return
        if date.fromisoformat(str(day)) > date.today():
            return
        await self.run(wc.add_set_on, self.db, self.cfg, exercise, str(day), weight, reps,
                       None if not rpe else float(rpe))
        await self.publish()

    # ------------------------------------------------------- body
    async def log_body(self, metric, value, day=None):
        await self.run(wc.add_body, self.db, metric, float(value), day or None)
        await self.publish(("stats",))

    async def delete_body(self, entry_id):
        await self.run(wc.delete_body, self.db, int(entry_id))
        await self.publish(("stats",))

    # ------------------------------------------------------- gym visits
    async def notify_(self, title, message):
        if not self.notify:
            return
        domain, _, name = self.notify.partition(".")
        url = self.cfg["settings"].get("dashboard_url") or "/workout"
        try:
            await self.host.call_service(domain, name, {
                "title": title, "message": message, "data": {"url": url, "clickAction": url}})
        except Exception as err:
            _LOGGER.warning("workout_tracker: notification failed: %s", err)

    async def gym_arrived(self):
        visit = await self.run(wc.start_visit, self.db)
        self.host.fire("workout_gym_arrived", {"start": visit["start"]})
        await self.publish(("plan", "stats"))
        p = self.bundle["plan"]["attributes"]
        if p.get("next_main"):
            today = p["next_main"].title() + (f" + {p['next_addon'].title()}" if p.get("next_addon") else "")
            await self.notify_("At the gym", f"Today: {today}")

    async def gym_left(self):
        visit = await self.run(wc.end_visit, self.db, None,
                               self.cfg["settings"].get("min_visit_minutes", 15))
        await self.publish(("plan", "stats"))
        if not visit:
            return
        self.host.fire("workout_gym_left", visit)
        day = await self.run(wc.day_detail, self.db, self.cfg, visit["start"][:10])
        groups = " + ".join(g.title() for g in day["groups"]) or "No sets logged"
        prs = ""
        if self.last_pr and self.last_pr.get("day") == visit["start"][:10]:
            prs = f", new record on {self.last_pr['exercise']}"
        await self.notify_("Workout done", f"{visit['minutes']} min, {day['sets']} sets: {groups}{prs}")

    async def person_changed(self, old, new):
        name = self.gym_zone_name().lower()
        if not name:
            return
        was, now = str(old or "").lower() == name, str(new or "").lower() == name
        if now and not was:
            await self.gym_arrived()
        elif was and not now:
            await self.gym_left()

    # ------------------------------------------------------- export / day change
    async def export(self):
        res = await self.run(wc.export_csv, self.db, self.cfg, self.export_dir)
        self.export_info = {"created": res["created"], "files": [
            {"name": f["name"], "rows": f["rows"], "url": f"{self.export_url}/{f['file']}"}
            for f in res["files"]]}
        await self.publish(("export",))

    async def midnight(self):
        self.log_day = None
        self.last_pr = None
        await self.load()
        await self.apply_suggestion()
        await self.publish()

    async def refresh(self):
        await self.load()
        await self.publish()


# --------------------------------------------------------------------------
# Several people in one home
# --------------------------------------------------------------------------

def _safe(uid):
    return "".join(c for c in str(uid) if c.isalnum() or c in "-_")[:64] or "default"


class Hub:
    """One Tracker per Home Assistant user, each with its own database.

    The owner (chosen in the setup screen) keeps the original database, so
    existing history stays theirs; everyone else gets users/<user id>.db.
    Trackers are created when someone first opens the page.
    """

    def __init__(self, host, data_dir, owner_db, config_path, export_root, export_url,
                 options, owner_id, listeners=None):
        import asyncio
        self.host = host
        self.data_dir = data_dir
        self.owner_db = owner_db
        self.config_path = config_path
        self.export_root = export_root
        self.export_url = export_url
        self.options = dict(options or {})
        self.owner_id = owner_id or ""
        self.trackers = {}
        self.listeners = listeners if listeners is not None else {}
        self._locks = {}
        self._asyncio = asyncio

    def profile(self, uid):
        return uid or self.owner_id or "owner"

    def db_for(self, uid):
        import os
        uid = self.profile(uid)
        if uid in (self.owner_id, "owner"):
            return self.owner_db
        return os.path.join(self.data_dir, "users", f"{_safe(uid)}.db")

    def has_data(self, uid):
        import os
        return os.path.exists(self.db_for(uid))

    async def get(self, uid=None):
        uid = self.profile(uid)
        if uid in self.trackers:
            return self.trackers[uid]
        lock = self._locks.setdefault(uid, self._asyncio.Lock())
        async with lock:
            if uid in self.trackers:
                return self.trackers[uid]
            import os
            owner = uid in (self.owner_id, "owner")
            safe = _safe(uid)
            t = Tracker(self.host, self.db_for(uid), self.config_path,
                        os.path.join(self.export_root, safe), f"{self.export_url}/{safe}",
                        self.options, listeners=self.listeners.setdefault(uid, set()),
                        user_id=None if uid == "owner" else uid, owner=owner)
            await self.host.run(os.makedirs, os.path.dirname(t.db) or ".", 0o755, True)
            await t.start()
            self.trackers[uid] = t
            return t

    async def start_known(self, user_ids):
        """Start the owner and everyone who already has data (for gym tracking)."""
        await self.get(self.owner_id or None)
        for uid in user_ids:
            if uid != self.owner_id and self.has_data(uid):
                await self.get(uid)

    def people(self):
        """Person entities to watch, mapped to the profiles that use them."""
        out = {}
        for uid, t in self.trackers.items():
            if t.person and t.gym_zone:
                out.setdefault(t.person, []).append(uid)
        return out

    async def person_changed(self, entity_id, old, new):
        for uid in self.people().get(entity_id, []):
            await self.trackers[uid].person_changed(old, new)

    async def midnight(self):
        for t in list(self.trackers.values()):
            await t.midnight()
