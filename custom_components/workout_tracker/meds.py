"""Medication and supplement reminders.

Schedules work like recurring meetings in Outlook:
    repeat  "daily"  every N days
            "weekly" every N weeks, on chosen weekdays (0 = Monday)
            "none"   as needed: no reminders, log doses yourself
    times   one or more times a day ("08:00", "20:00")
    start   first day; end: last day or None (never)

A dose is one (medicine, day, time). Its status is worked out, not stored,
unless you acted on it: taken / skipped are logged; a dose nobody acted on
is "due" until midnight and "missed" afterwards.

Reminders: at the dose time a phone notification (with Taken / Skip
buttons) and, when you're home, the Echo; once more after 30 minutes if
it's still open.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta

from . import core as wc

_LOGGER = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'medication',
    dose TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    schedule TEXT NOT NULL,
    echo INTEGER NOT NULL DEFAULT 1,
    paused INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS med_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    med_id INTEGER NOT NULL,
    due TEXT,                 -- 'YYYY-MM-DD HH:MM' of the scheduled dose, NULL for as-needed
    status TEXT NOT NULL,     -- taken | skipped
    ts TEXT NOT NULL          -- when it was logged
);
CREATE INDEX IF NOT EXISTS idx_med_log ON med_log(med_id, due);
CREATE TABLE IF NOT EXISTS med_reminders (
    med_id INTEGER NOT NULL,
    due TEXT NOT NULL,
    count INTEGER NOT NULL,
    last TEXT NOT NULL,
    PRIMARY KEY (med_id, due)
);
"""

REPEATS = ("daily", "weekly", "none")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
FIRST_WINDOW = timedelta(hours=2)    # a dose more than this late (e.g. after a restart) gets no reminder
SECOND_AFTER = timedelta(minutes=30)
HISTORY_DAYS = 30


def _connect(db_path):
    con = wc._connect(db_path)
    con.executescript(SCHEMA)
    return con


def _now(now=None):
    return now or datetime.now().replace(second=0, microsecond=0)


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------

def clean_schedule(s):
    """A schedule with only valid fields; raises ValueError for nonsense."""
    if not isinstance(s, dict):
        raise ValueError("schedule missing")
    repeat = s.get("repeat", "daily")
    if repeat not in REPEATS:
        raise ValueError("repeat must be daily, weekly or none")
    every = int(s.get("every") or 1)
    if not 1 <= every <= 365:
        raise ValueError("'every' must be between 1 and 365")
    times = []
    for t in s.get("times") or []:
        m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(t).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            raise ValueError(f"not a time: {t}")
        times.append(f"{int(m.group(1)):02d}:{m.group(2)}")
    times = sorted(set(times))
    if repeat != "none" and not times:
        raise ValueError("add at least one time")
    days = sorted({int(d) for d in (s.get("days") or []) if 0 <= int(d) <= 6})
    if repeat == "weekly" and not days:
        raise ValueError("pick at least one weekday")
    start = date.fromisoformat(str(s.get("start") or date.today().isoformat())).isoformat()
    end = s.get("end") or None
    if end:
        end = date.fromisoformat(str(end)).isoformat()
        if end < start:
            raise ValueError("the end date is before the start date")
    return {"repeat": repeat, "every": every, "times": times if repeat != "none" else [],
            "days": days if repeat == "weekly" else [], "start": start, "end": end}


def due_on(sched, day):
    """Is there a dose on this day?"""
    d = day if isinstance(day, date) else date.fromisoformat(day)
    start = date.fromisoformat(sched["start"])
    if sched["repeat"] == "none" or d < start:
        return False
    if sched.get("end") and d > date.fromisoformat(sched["end"]):
        return False
    if sched["repeat"] == "daily":
        return (d - start).days % sched["every"] == 0
    # weekly: weeks counted from the Monday of the start week
    w0 = start - timedelta(days=start.weekday())
    w = d - timedelta(days=d.weekday())
    return d.weekday() in sched["days"] and ((w - w0).days // 7) % sched["every"] == 0


def describe(sched):
    """'Every day at 08:00 and 20:00', 'Every 2 weeks on Mon, Thu at 09:00', 'As needed'."""
    if sched["repeat"] == "none":
        return "As needed"
    t = sched["times"]
    at = " at " + (", ".join(t[:-1]) + " and " + t[-1] if len(t) > 1 else t[0])
    if sched["repeat"] == "daily":
        base = "Every day" if sched["every"] == 1 else f"Every {sched['every']} days"
    else:
        days = sched["days"]
        names = "weekday" if days == [0, 1, 2, 3, 4] else ", ".join(DAYS[d] for d in days)
        base = ("Every week" if sched["every"] == 1 else f"Every {sched['every']} weeks") + f" on {names}"
        if days == list(range(7)) and sched["every"] == 1:
            base = "Every day"
    out = base + at
    if sched.get("end"):
        e = date.fromisoformat(sched["end"])
        out += f", until {e.day} {e:%b}"
    return out


# --------------------------------------------------------------------------
# Medicines
# --------------------------------------------------------------------------

def _row(r):
    sched = json.loads(r["schedule"])
    return {"id": r["id"], "name": r["name"], "kind": r["kind"], "dose": r["dose"], "note": r["note"],
            "schedule": sched, "summary": describe(sched), "echo": bool(r["echo"]),
            "paused": bool(r["paused"])}


def list_meds(db_path, include_deleted=False):
    con = _connect(db_path)
    rows = con.execute("SELECT * FROM meds" + ("" if include_deleted else " WHERE deleted = 0")
                       + " ORDER BY lower(name)").fetchall()
    con.close()
    return [_row(r) for r in rows]


def get_med(db_path, med_id):
    con = _connect(db_path)
    r = con.execute("SELECT * FROM meds WHERE id = ?", (int(med_id),)).fetchone()
    con.close()
    return _row(r) if r else None


def save_med(db_path, name, schedule, kind="medication", dose="", note="", echo=True, med_id=None):
    name = " ".join(str(name or "").split())[:60]
    if not name:
        raise ValueError("give it a name")
    sched = clean_schedule(schedule)
    kind = "supplement" if str(kind).lower().startswith("supp") else "medication"
    dose = " ".join(str(dose or "").split())[:40]
    note = " ".join(str(note or "").split())[:120]
    con = _connect(db_path)
    with con:
        if med_id:
            cur = con.execute("UPDATE meds SET name=?, kind=?, dose=?, note=?, schedule=?, echo=? "
                              "WHERE id=? AND deleted=0",
                              (name, kind, dose, note, json.dumps(sched), int(bool(echo)), int(med_id)))
            if not cur.rowcount:
                raise ValueError("that medicine doesn't exist")
            new_id = int(med_id)
        else:
            cur = con.execute("INSERT INTO meds(name, kind, dose, note, schedule, echo, created) "
                              "VALUES(?, ?, ?, ?, ?, ?, ?)",
                              (name, kind, dose, note, json.dumps(sched), int(bool(echo)),
                               datetime.now().isoformat(timespec="seconds")))
            new_id = cur.lastrowid
    con.close()
    return new_id


def set_paused(db_path, med_id, paused):
    con = _connect(db_path)
    with con:
        con.execute("UPDATE meds SET paused = ? WHERE id = ?", (int(bool(paused)), int(med_id)))
    con.close()


def delete_med(db_path, med_id):
    """Hidden, history kept."""
    con = _connect(db_path)
    with con:
        con.execute("UPDATE meds SET deleted = 1 WHERE id = ?", (int(med_id),))
    con.close()


# --------------------------------------------------------------------------
# Doses
# --------------------------------------------------------------------------

def _at(at, due, now):
    """When it was taken: 'HH:MM' (on the dose's day, else today) or a full
    'YYYY-MM-DD HH:MM'; default now."""
    if not at:
        return _now(now)
    at = str(at).strip()
    if re.fullmatch(r"\d{1,2}:\d{2}", at):
        day = datetime.fromisoformat(str(due)).date() if due else _now(now).date()
        h, m = map(int, at.split(":"))
        if h > 23 or m > 59:
            raise ValueError(f"not a time: {at}")
        return datetime.combine(day, datetime.min.time()).replace(hour=h, minute=m)
    return datetime.fromisoformat(at).replace(second=0, microsecond=0)


def log_dose(db_path, med_id, status, due=None, now=None, at=None):
    """Taken or skipped. For a scheduled dose `due` is 'YYYY-MM-DD HH:MM';
    logging the same dose again replaces the earlier entry. `at` = when it
    was taken (default now)."""
    if status not in ("taken", "skipped"):
        raise ValueError("status must be taken or skipped")
    ts = _at(at, due, now).isoformat(sep=" ", timespec="minutes")
    con = _connect(db_path)
    with con:
        if due:
            due = datetime.fromisoformat(str(due)).strftime("%Y-%m-%d %H:%M")
            con.execute("DELETE FROM med_log WHERE med_id = ? AND due = ?", (int(med_id), due))
        cur = con.execute("INSERT INTO med_log(med_id, due, status, ts) VALUES(?, ?, ?, ?)",
                          (int(med_id), due, status, ts))
        log_id = cur.lastrowid
    con.close()
    return log_id


def update_log(db_path, log_id, status=None, at=None):
    """Change a logged dose: taken / skipped, and when."""
    con = _connect(db_path)
    row = con.execute("SELECT due, ts, status FROM med_log WHERE id = ?", (int(log_id),)).fetchone()
    if not row:
        con.close()
        raise ValueError("that dose isn't logged")
    status = status or row["status"]
    if status not in ("taken", "skipped"):
        con.close()
        raise ValueError("status must be taken or skipped")
    ts = row["ts"]
    if at:
        base = row["due"] or row["ts"][:10] + " 00:00"
        ts = _at(at, base, None).isoformat(sep=" ", timespec="minutes")
    with con:
        con.execute("UPDATE med_log SET status = ?, ts = ? WHERE id = ?", (status, ts, int(log_id)))
    con.close()


def undo_log(db_path, log_id):
    con = _connect(db_path)
    with con:
        con.execute("DELETE FROM med_log WHERE id = ?", (int(log_id),))
    con.close()


def _logs(db_path, since):
    con = _connect(db_path)
    rows = [dict(r) for r in con.execute(
        "SELECT id, med_id, due, status, ts FROM med_log WHERE coalesce(due, ts) >= ? ORDER BY ts", (since,))]
    con.close()
    return rows


def doses_on(meds, day, logs, now=None):
    """Every scheduled dose on `day` with its status, plus as-needed doses taken."""
    now = _now(now)
    d = day if isinstance(day, date) else date.fromisoformat(day)
    by_due = {(l["med_id"], l["due"]): l for l in logs if l["due"]}
    out = []
    for m in meds:
        if m["paused"] or not due_on(m["schedule"], d):
            continue
        for t in m["schedule"]["times"]:
            due = f"{d.isoformat()} {t}"
            log = by_due.get((m["id"], due))
            due_dt = datetime.fromisoformat(due)
            if log:
                status = log["status"]
            elif d < now.date():
                status = "missed"
            elif due_dt > now:
                status = "upcoming"
            else:
                status = "due"
            out.append({"med_id": m["id"], "name": m["name"], "dose": m["dose"], "note": m["note"],
                        "kind": m["kind"], "due": due, "time": t, "status": status,
                        "log_id": log["id"] if log else None,
                        "at": log["ts"][11:16] if log else None})
    names = {m["id"]: m for m in meds}
    for l in logs:
        if not l["due"] and l["ts"][:10] == d.isoformat() and l["med_id"] in names:
            m = names[l["med_id"]]
            out.append({"med_id": m["id"], "name": m["name"], "dose": m["dose"], "note": m["note"],
                        "kind": m["kind"], "due": None, "time": l["ts"][11:16], "status": l["status"],
                        "log_id": l["id"], "at": l["ts"][11:16], "as_needed": True})
    out.sort(key=lambda x: (x["time"], x["name"].lower()))
    return out


def overview(db_path, now=None, days=HISTORY_DAYS):
    """Everything the Meds tab draws."""
    now = _now(now)
    today = now.date()
    meds = list_meds(db_path)
    first = today - timedelta(days=days - 1)
    logs = _logs(db_path, first.isoformat())
    hist = {m["id"]: [] for m in meds}
    totals = {m["id"]: {"taken": 0, "missed": 0, "skipped": 0} for m in meds}
    for i in range(days):
        d = first + timedelta(days=i)
        doses = doses_on(meds, d, logs, now)
        for m in meds:
            mine = [x for x in doses if x["med_id"] == m["id"] and not x.get("as_needed")]
            cnt = {"taken": 0, "missed": 0, "skipped": 0, "due": 0, "upcoming": 0}
            for x in mine:
                cnt[x["status"]] = cnt.get(x["status"], 0) + 1
            for k in ("taken", "missed", "skipped"):
                totals[m["id"]][k] += cnt[k]
            hist[m["id"]].append({"day": d.isoformat(), **cnt, "doses": len(mine)})
    for m in meds:
        t = totals[m["id"]]
        counted = t["taken"] + t["missed"]
        m["adherence"] = round(100 * t["taken"] / counted) if counted else None
        m["totals"] = t
        m["history"] = hist[m["id"]][-14:]
    all_t = sum(t["taken"] for t in totals.values())
    all_c = all_t + sum(t["missed"] for t in totals.values())
    return {
        "today": doses_on(meds, today, logs, now),
        # the last two weeks, newest first, to look back and fix days
        "days": [{"day": (today - timedelta(days=i)).isoformat(),
                  "doses": doses_on(meds, today - timedelta(days=i), logs, now)} for i in range(14)],
        "meds": meds,
        "adherence": round(100 * all_t / all_c) if all_c else None,
        "recent": [{**l, "name": next((m["name"] for m in meds if m["id"] == l["med_id"]), "?")}
                   for l in reversed(logs[-20:])],
    }


# --------------------------------------------------------------------------
# Reminders
# --------------------------------------------------------------------------

def reminders_due(db_path, now=None):
    """Doses to remind about right now: [(dose, which)] with which = 1 or 2."""
    now = _now(now)
    meds = [m for m in list_meds(db_path) if not m["paused"] and m["schedule"]["repeat"] != "none"]
    if not meds:
        return []
    logs = _logs(db_path, (now.date() - timedelta(days=1)).isoformat())
    con = _connect(db_path)
    sent = {(r["med_id"], r["due"]): dict(r) for r in con.execute("SELECT * FROM med_reminders")}
    con.close()
    out = []
    for d in (now.date() - timedelta(days=1), now.date()):
        for dose in doses_on(meds, d, logs, now):
            if dose["status"] not in ("due", "missed") or dose.get("as_needed"):
                continue
            due = datetime.fromisoformat(dose["due"])
            late = now - due
            r = sent.get((dose["med_id"], dose["due"]))
            if r is None and timedelta(0) <= late < FIRST_WINDOW:
                out.append((dose, 1))
            elif r and r["count"] == 1 and now - datetime.fromisoformat(r["last"]) >= SECOND_AFTER \
                    and late < FIRST_WINDOW + SECOND_AFTER:
                out.append((dose, 2))
    return out


def mark_reminded(db_path, med_id, due, count, now=None):
    now = _now(now)
    con = _connect(db_path)
    with con:
        con.execute("INSERT INTO med_reminders(med_id, due, count, last) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(med_id, due) DO UPDATE SET count = excluded.count, last = excluded.last",
                    (int(med_id), due, count, now.isoformat(sep=" ", timespec="minutes")))
        con.execute("DELETE FROM med_reminders WHERE due < ?", ((now - timedelta(days=3)).strftime("%Y-%m-%d"),))
    con.close()


def spoken(dose, second=False):
    """What the Echo says, per house personality (for script.say)."""
    name = dose["name"]
    what = f"{name}, {dose['dose']}" if dose["dose"] else name
    note = f" {dose['note'].rstrip('.')}." if dose["note"] else ""
    if second:
        normal = f"Reminder: you haven't taken your {what} yet.{note}"
        lines = {
            "rocky": [f"Question? {name} still not taken. Rocky waits. Rocky not patient. Take now!",
                      f"Friend forgets {name}. Bad bad bad. Take it, then Rocky happy.",
                      f"Thirty minutes passed, question? {name} still in bottle. Fix, fix!"],
            "roast": [f"Still no {name}? Bold strategy for someone held together by snacks and hope.",
                      f"Thirty minutes later and the {name} is still waiting. It's a pill, not a commitment.",
                      f"Your {name} called. It feels ignored. Honestly, so do I."],
        }
    else:
        normal = f"Time for your {what}.{note}"
        lines = {
            "rocky": [f"Time for {name}, question? Yes! Take {what}. Body gets stronger. Amaze!{note}",
                      f"Rocky reminds friend: {what}. Good for human body. Take now.{note}",
                      f"{name} time! Rocky watches. Rocky proud when friend takes it.{note}"],
            "roast": [f"Time for your {what}. Your body is running on fumes, help it out.{note}",
                      f"{name} o'clock. Even you can manage to swallow one thing on schedule.{note}",
                      f"Take your {what}. I'd do it for you, but I'm a smart speaker, not a miracle worker.{note}"],
        }
    return normal, lines


class MedsMixin:
    """Medication features for Tracker (see tracker.py)."""

    def _meds_init(self):
        self.meds_data = None

    async def meds_part(self):
        o = await self.run(overview, self.db)
        o["echo"] = self._echo_target()
        o["home"] = self._is_home()
        o["phone"] = bool(self.notify)
        return o

    def _is_home(self):
        if not self.person or not hasattr(self.host, "state"):
            return False
        return self.host.state(self.person) == "home"

    def _echo_target(self):
        """How the Echo speaks: 'say' (house personality script), an announce
        notify entity, or None."""
        if self.host.has_service("script", "say"):
            return {"via": "say", "entity": "script.say"}
        for e in sorted(self.host.entity_ids("notify")):
            if e.endswith("_announce"):
                return {"via": "announce", "entity": e}
        return None

    async def _speak(self, dose, second=False):
        echo = self._echo_target()
        if not echo:
            return
        normal, lines = spoken(dose, second)
        try:
            if echo["via"] == "say":
                await self.host.call_service("script", "say", {"message": normal, "lines": lines, "to": "echo"})
            else:
                await self.host.call_service("notify", "send_message",
                                             {"entity_id": echo["entity"], "message": normal})
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("workout_tracker: Echo reminder failed: %s", err)

    async def meds_tick(self, now=None):
        """Every minute: send reminders that are due."""
        due = await self.run(reminders_due, self.db, now)
        for dose, which in due:
            await self.run(mark_reminded, self.db, dose["med_id"], dose["due"], which, now)
            await self._remind(dose, which == 2)
        if due:
            await self.publish(("meds",))

    async def _remind(self, dose, second):
        what = f"{dose['name']} ({dose['dose']})" if dose["dose"] else dose["name"]
        title = f"Still to take: {dose['name']}" if second else f"Time for {dose['name']}"
        body = f"{what} at {dose['time']}" + (f". {dose['note']}" if dose["note"] else "")
        key = f"{dose['med_id']}|{dose['due']}"
        prof = self.user_id or ""
        await self.notify_(title, body, {
            "tag": f"wt-med-{dose['med_id']}-{dose['due'].replace(' ', 'T')}",
            "url": "/workout?tab=meds", "clickAction": "/workout?tab=meds",
            "push": {"interruption-level": "time-sensitive"},
            "actions": [
                {"action": f"WT_MED_TAKEN|{prof}|{key}", "title": "Taken"},
                {"action": f"WT_MED_SKIP|{prof}|{key}", "title": "Skip", "destructive": True},
            ]})
        self.host.fire("workout_med_reminder", {"name": dose["name"], "due": dose["due"], "second": second})
        if self._is_home() and self._med_echo(dose["med_id"]):
            await self._speak(dose, second)

    def _med_echo(self, med_id):
        for m in (self.bundle.get("meds", {}).get("attributes") or {}).get("meds", []):
            if m["id"] == med_id:
                return m["echo"]
        return True

    async def _clear_notification(self, med_id, due):
        if self.notify and due:
            domain, _, name = self.notify.partition(".")
            try:
                await self.host.call_service(domain, name, {
                    "message": "clear_notification",
                    "data": {"tag": f"wt-med-{med_id}-{due.replace(' ', 'T')}"}})
            except Exception:  # noqa: BLE001
                pass

    # ---------------------------------------------------------- actions
    async def med_save(self, name, schedule, kind="medication", dose="", note="", echo=True, med_id=None):
        from .tracker import _truthy
        if isinstance(schedule, str):
            schedule = json.loads(schedule)
        await self.run(lambda: save_med(self.db, name, schedule, kind, dose, note,
                                        _truthy(echo) if not isinstance(echo, bool) else echo, med_id))
        await self.publish(("meds",))

    async def med_delete(self, med_id):
        await self.run(delete_med, self.db, med_id)
        await self.publish(("meds",))

    async def med_pause(self, med_id, paused=True):
        from .tracker import _truthy
        await self.run(set_paused, self.db, med_id, _truthy(paused) if not isinstance(paused, bool) else paused)
        await self.publish(("meds",))

    async def med_log(self, med_id, status="taken", due=None, at=None):
        await self.run(lambda: log_dose(self.db, med_id, status, due or None, None, at or None))
        if due:
            await self._clear_notification(int(med_id), datetime.fromisoformat(str(due)).strftime("%Y-%m-%d %H:%M"))
        await self.publish(("meds",))

    async def med_update(self, log_id, status=None, at=None):
        await self.run(lambda: update_log(self.db, log_id, status or None, at or None))
        await self.publish(("meds",))

    async def med_undo(self, log_id):
        await self.run(undo_log, self.db, log_id)
        await self.publish(("meds",))

    async def med_action(self, kind, key):
        """From a notification button: kind TAKEN / SKIP, key 'med_id|YYYY-MM-DD HH:MM'."""
        med_id, _, due = key.partition("|")
        await self.med_log(int(med_id), "taken" if kind == "TAKEN" else "skipped", due or None)
