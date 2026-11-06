"""Daily activity: steps from the phone and an estimate of calories burned.

Works without a watch. Steps come from the Home Assistant phone app (the
phone's own step count and, where available, Apple Health / Health Connect,
which also merge a watch); the higher of the two counts. Active calories
(only what you burned by moving, not your resting burn) are estimated for
today so far, from your latest body weight in the Body section:

  walking  steps x body weight x 0.0005 kcal
  gym      minutes at the gym x body weight x 3.5 kcal per kg per hour on
           top of resting (strength training at a moderate effort). Minutes
           come from the gym visit (zone), the visit going on right now, or,
           without a visit, from the logged sets.

When the phone also reports measured active energy (a watch worn that day),
it is shown next to the estimate for comparison. Each day's numbers are kept
in the `activity` table, so the history survives restarts.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from . import core as wc

_LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    day TEXT PRIMARY KEY,
    steps INTEGER NOT NULL DEFAULT 0,
    kcal INTEGER,
    resting INTEGER,
    walking INTEGER,
    gym INTEGER,
    watch_kcal INTEGER,
    goal INTEGER,
    updated TEXT NOT NULL
);
"""

# age and sex are kept for older saved settings; the estimate no longer uses them
DEFAULTS = {"goal": 8000, "age": None, "sex": None, "nudge": "17:00", "steps": "auto", "energy": "auto"}
KCAL_PER_STEP_PER_KG = 0.0005
GYM_KCAL_PER_KG_HOUR = 3.5          # MET ~4.5 for strength training, minus the resting 1 MET
MINUTES_PER_SET = 2.5               # set plus rest, when no gym visit was recorded
MAX_GYM_MINUTES = 240
HISTORY_DAYS = 14


def _connect(db_path):
    con = wc._connect(db_path)
    con.executescript(SCHEMA)
    return con


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

def clean_settings(raw):
    """Settings as stored in prefs['activity'], with defaults and sane values."""
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (raw or {}).items() if k in DEFAULTS})
    try:
        s["goal"] = max(500, min(int(float(s["goal"])), 100000))
    except (TypeError, ValueError):
        s["goal"] = DEFAULTS["goal"]
    try:
        s["age"] = int(float(s["age"])) if s["age"] not in (None, "") else None
        if s["age"] is not None and not 10 <= s["age"] <= 110:
            s["age"] = None
    except (TypeError, ValueError):
        s["age"] = None
    sex = str(s["sex"] or "").strip().lower()
    s["sex"] = sex if sex in ("male", "female") else None
    nudge = str(s["nudge"] or "").strip()
    if nudge:
        try:
            nudge = datetime.strptime(nudge[:5], "%H:%M").strftime("%H:%M")
        except ValueError:
            nudge = DEFAULTS["nudge"]
    s["nudge"] = nudge
    for key in ("steps", "energy"):
        v = s[key]
        if isinstance(v, str):
            v = v.strip() or "auto"
            s[key] = v if v == "auto" else [e.strip() for e in v.split(",") if e.strip()]
        elif isinstance(v, (list, tuple)):
            s[key] = [str(e).strip() for e in v if str(e).strip()] or "auto"
        else:
            s[key] = "auto"
    return s


# --------------------------------------------------------------------------
# Calories
# --------------------------------------------------------------------------

def estimate(weight, steps, gym_minutes):
    """Active calories so far today: walking plus gym, nothing for resting.
    Everything is None without a body weight."""
    if not weight:
        return {"total": None, "walking": None, "gym": None}
    walking = round(steps * weight * KCAL_PER_STEP_PER_KG)
    gym = round(GYM_KCAL_PER_KG_HOUR * weight * gym_minutes / 60)
    return {"total": walking + gym, "walking": walking, "gym": gym}


def gym_minutes_on(db_path, day, now=None):
    """Minutes at the gym on `day`: finished visits plus a visit still going on;
    without any visit, worked out from the sets logged that day.
    Returns (minutes, basis) with basis 'visit', 'sets' or None."""
    now = now or datetime.now()
    con = _connect(db_path)
    visits = con.execute("SELECT start, end FROM gym_visits WHERE substr(start, 1, 10) = ?", (day,)).fetchall()
    sets = con.execute("SELECT MIN(ts) AS a, MAX(ts) AS b, COUNT(*) AS n FROM sets "
                       "WHERE day = ? AND kind = 'work'", (day,)).fetchone()
    con.close()
    minutes = 0.0
    for v in visits:
        start = datetime.fromisoformat(v["start"])
        end = datetime.fromisoformat(v["end"]) if v["end"] else (now if day == now.date().isoformat() else None)
        if end and end > start:
            minutes += min((end - start).total_seconds() / 60, MAX_GYM_MINUTES)
    if minutes > 0:
        return round(min(minutes, MAX_GYM_MINUTES)), "visit"
    n = sets["n"] or 0
    if not n:
        return 0, None
    by_count = n * MINUTES_PER_SET
    if n >= 2 and sets["a"] and sets["b"]:
        span = (datetime.fromisoformat(sets["b"]) - datetime.fromisoformat(sets["a"])).total_seconds() / 60 + 5
        if by_count <= span <= MAX_GYM_MINUTES:
            return round(span), "sets"
    return round(min(by_count, MAX_GYM_MINUTES)), "sets"


def latest_body(db_path):
    """Latest body weight (kg) and height (cm), or None."""
    con = _connect(db_path)
    out = {}
    for metric in ("weight", "height"):
        r = con.execute("SELECT value FROM body WHERE metric = ? ORDER BY day DESC, id DESC LIMIT 1",
                        (metric,)).fetchone()
        out[metric] = r["value"] if r else None
    con.close()
    return out


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

def save_day(db_path, day, steps, est, watch_kcal, goal, now=None):
    con = _connect(db_path)
    with con:
        con.execute(
            "INSERT INTO activity(day, steps, kcal, resting, walking, gym, watch_kcal, goal, updated) "
            "VALUES(?, ?, ?, NULL, ?, ?, ?, ?, ?) ON CONFLICT(day) DO UPDATE SET "
            "steps = excluded.steps, kcal = excluded.kcal, resting = NULL, "
            "walking = excluded.walking, gym = excluded.gym, watch_kcal = excluded.watch_kcal, "
            "goal = excluded.goal, updated = excluded.updated",
            (day, int(steps), est.get("total"), est.get("walking"), est.get("gym"),
             watch_kcal, int(goal), (now or datetime.now()).isoformat(timespec="seconds")))
    con.close()


def history(db_path, today=None, days=HISTORY_DAYS):
    """The last `days` days (oldest first, today last), with averages."""
    d0 = date.fromisoformat(today) if today else date.today()
    since = (d0 - timedelta(days=days - 1)).isoformat()
    con = _connect(db_path)
    rows = {r["day"]: dict(r) for r in con.execute("SELECT * FROM activity WHERE day >= ?", (since,))}
    con.close()
    out = []
    for i in range(days):
        d = (d0 - timedelta(days=days - 1 - i)).isoformat()
        r = rows.get(d)
        out.append({"day": d, "steps": r["steps"] if r else None, "kcal": r["kcal"] if r else None,
                    "watch_kcal": r["watch_kcal"] if r else None, "goal": r["goal"] if r else None})
    past = [r for r in out[-8:-1] if r["steps"] is not None]      # the 7 days before today
    avg_steps = round(sum(r["steps"] for r in past) / len(past)) if past else None
    kc = [r["kcal"] for r in past if r["kcal"] is not None]
    avg_kcal = round(sum(kc) / len(kc)) if kc else None
    met = sum(1 for r in past if r["goal"] and r["steps"] >= r["goal"])
    return {"days": out, "avg_steps_7d": avg_steps, "avg_kcal_7d": avg_kcal,
            "goal_days_7d": met, "days_counted_7d": len(past)}


# --------------------------------------------------------------------------
# Tracker side
# --------------------------------------------------------------------------

class ActivityMixin:
    """Activity features for Tracker (see tracker.py)."""

    def _activity_init(self):
        self.activity = None          # last computed today-values, to publish only on change
        self._nudged_day = None

    @property
    def activity_settings_(self):
        return clean_settings(self.prefs.get("activity"))

    # ---- where the numbers come from
    def _phone_entities(self):
        """Entities that belong to this person's phones (same device as their trackers)."""
        if not self.person:
            return []
        trackers = (self.host.attributes(self.person) or {}).get("device_trackers") or []
        related = getattr(self.host, "device_entities", None)
        out = []
        for t in trackers:
            if related:
                out += related(t)
        if not out and self.notify.startswith("notify.mobile_app_"):
            # no device registry (tests, older hosts): guess from the phone's notify name
            name = self.notify.split("mobile_app_", 1)[1]
            out = [e for e in self.host.entity_ids("sensor") if e.startswith(f"sensor.{name}_")]
        return sorted(set(out))

    def activity_sources(self):
        s = self.activity_settings_
        phone = self._phone_entities()
        steps = s["steps"] if s["steps"] != "auto" else [
            e for e in phone if e.startswith("sensor.") and e.endswith("_steps")]
        energy = s["energy"] if s["energy"] != "auto" else [
            e for e in phone if e.startswith("sensor.") and e.endswith("_active_energy")]
        return {"steps": steps, "energy": energy}

    def _today_value(self, entity_id, today):
        """A number from an entity, only if it was updated today (a phone that hasn't
        sent anything since midnight still shows yesterday's count)."""
        state = self.host.state(entity_id) if hasattr(self.host, "state") else None
        try:
            value = float(state)
        except (TypeError, ValueError):
            return None
        updated = self.host.last_updated(entity_id) if hasattr(self.host, "last_updated") else None
        if updated is not None and updated.date() != today:
            return None
        return value

    # ---- today
    async def activity_part(self, now=None):
        now = now or datetime.now()
        today = now.date()
        day = today.isoformat()
        s = self.activity_settings_
        src = self.activity_sources()
        steps_vals = [v for v in (self._today_value(e, today) for e in src["steps"]) if v is not None]
        steps = int(max(steps_vals)) if steps_vals else 0
        energy_vals = [v for v in (self._today_value(e, today) for e in src["energy"]) if v is not None]
        watch = int(round(max(energy_vals))) if energy_vals else None
        body = await self.run(latest_body, self.db)
        gym_min, gym_basis = await self.run(gym_minutes_on, self.db, day, now)
        est = estimate(body["weight"], steps, gym_min)
        missing = [] if body["weight"] else ["weight"]
        today_vals = (day, steps, est["total"], watch, gym_min, s["goal"])
        if self.activity != today_vals:
            self.activity = today_vals
            if steps or est["total"] is not None:
                await self.run(save_day, self.db, day, steps, est, watch, s["goal"], now)
        hist = await self.run(history, self.db, day)
        return {
            "day": day,
            "steps": steps,
            "goal": s["goal"],
            "percent": round(100 * steps / s["goal"]) if s["goal"] else 0,
            "kcal": est,
            "watch_kcal": watch,
            "gym_minutes": gym_min,
            "gym_basis": gym_basis,
            "weight": body["weight"],
            "missing": missing,
            "settings": s,
            "sources": src,
            "phone": bool(self.notify),
            "history": hist,
        }

    async def _publish_activity(self, now=None):
        a = await self.activity_part(now)
        self._set("activity", a["steps"], a)

    async def activity_tick(self, now=None):
        """Every minute: refresh today's numbers when they changed, and the step nudge."""
        now = now or datetime.now()
        before = self.activity
        a = await self.activity_part(now)
        if before != self.activity or now.minute % 15 == 0:
            self._set("activity", a["steps"], a)
            self._push()
        s = a["settings"]
        day = a["day"]
        if (s["nudge"] and now.strftime("%H:%M") == s["nudge"] and self._nudged_day != day
                and a["steps"] < a["goal"]):
            self._nudged_day = day
            left = a["goal"] - a["steps"]
            minutes = max(1, -(-left // 100))       # about 100 steps a minute
            await self.notify_(f"{left:,} steps to go",
                               f"{a['steps']:,} of {a['goal']:,} so far today. "
                               f"About {minutes} minutes of walking gets you there.",
                               {"tag": "workout-steps", "url": "/workout?tab=overview",
                                "clickAction": "/workout?tab=overview"})

    async def activity_settings(self, goal=None, age=None, sex=None, nudge=None, steps=None, energy=None):
        raw = dict(self.prefs.get("activity") or {})
        for key, value in (("goal", goal), ("age", age), ("sex", sex), ("nudge", nudge),
                           ("steps", steps), ("energy", energy)):
            if value is not None:
                raw[key] = value
        clean = clean_settings(raw)
        await self.run(wc.set_pref, self.db, "activity", clean)
        self.prefs["activity"] = clean
        self.activity = None
        await self.publish(("activity",))
