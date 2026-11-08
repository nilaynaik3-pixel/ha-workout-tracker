"""Cardio: treadmill, bike, rower and the like, logged as one entry per session.

Plain Python like core.py, so it runs in the executor and is testable alone.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from .core import _connect, _today

SCHEMA = """
CREATE TABLE IF NOT EXISTS cardio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    activity TEXT NOT NULL,
    minutes REAL NOT NULL,
    distance REAL,
    level REAL,
    kcal REAL,
    hr REAL
);
CREATE INDEX IF NOT EXISTS idx_cardio_day ON cardio(day);
"""

# key: (has distance, what "level" means or None)
ACTIVITIES = {
    "treadmill": (True, "incline"),
    "run": (True, None),
    "walk": (True, None),
    "bike": (True, "level"),
    "cycling": (True, None),
    "cross": (True, "level"),
    "rower": (True, "level"),
    "stairs": (False, "level"),
    "swim": (True, None),
    "rope": (False, None),
    "other": (False, None),
}
WEEK_TARGET = 150   # minutes of cardio a week (WHO guideline for adults)


def _con(db_path):
    con = _connect(db_path)
    con.executescript(SCHEMA)
    return con


def _num(v, lo=0.0, hi=100000.0):
    if v in (None, ""):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x <= lo else min(x, hi)


def log_cardio(db_path, activity, minutes, distance=None, level=None, kcal=None, hr=None,
               day=None, now=None):
    activity = str(activity or "").strip().lower()
    if activity not in ACTIVITIES:
        raise ValueError(f"unknown activity {activity}")
    mins = _num(minutes, 0, 24 * 60)
    if not mins:
        raise ValueError("minutes must be more than 0")
    has_dist, has_level = ACTIVITIES[activity]
    con = _con(db_path)
    with con:
        cur = con.execute(
            "INSERT INTO cardio(ts, day, activity, minutes, distance, level, kcal, hr) VALUES(?,?,?,?,?,?,?,?)",
            ((now or datetime.now()).isoformat(timespec="seconds"), _today(day), activity, mins,
             _num(distance, 0, 1000) if has_dist else None,
             _num(level, -100, 100) if has_level else None,
             _num(kcal, 0, 20000), _num(hr, 0, 250)))
    con.close()
    return cur.lastrowid


def delete_cardio(db_path, entry_id):
    con = _con(db_path)
    with con:
        con.execute("DELETE FROM cardio WHERE id = ?", (int(entry_id),))
    con.close()


def _row(r):
    d = dict(r)
    d["pace"] = round(d["minutes"] / d["distance"], 2) if d.get("distance") else None   # min per km
    return d


def entries(db_path, since=None, until=None):
    con = _con(db_path)
    rows = con.execute("SELECT * FROM cardio WHERE day >= ? AND day <= ? ORDER BY day, id",
                       (since or "0000", until or "9999")).fetchall()
    con.close()
    return [_row(r) for r in rows]


def minutes_by_day(db_path, since=None):
    con = _con(db_path)
    rows = con.execute("SELECT day, SUM(minutes) AS m FROM cardio WHERE day >= ? GROUP BY day",
                       (since or "0000",)).fetchall()
    con.close()
    return {r["day"]: round(r["m"]) for r in rows}


def cardio_part(db_path, today=None, weeks=12):
    """What the Workout and Overview tabs show about cardio."""
    day = _today(today)
    d0 = date.fromisoformat(day)
    week0 = d0 - timedelta(days=d0.weekday())
    start = week0 - timedelta(weeks=weeks - 1)
    rows = entries(db_path, start.isoformat(), day)
    week_rows = []
    for w in range(weeks):
        ws = start + timedelta(weeks=w)
        we = (ws + timedelta(days=6)).isoformat()
        in_w = [r for r in rows if ws.isoformat() <= r["day"] <= we]
        week_rows.append({"start": ws.isoformat(),
                          "minutes": round(sum(r["minutes"] for r in in_w)),
                          "km": round(sum(r["distance"] or 0 for r in in_w), 1),
                          "sessions": len(in_w)})
    # per activity: the last sessions, for a pace or distance trend
    trend = {}
    for r in entries(db_path, (d0 - timedelta(days=180)).isoformat(), day):
        trend.setdefault(r["activity"], []).append(
            {"day": r["day"], "minutes": r["minutes"], "distance": r["distance"], "pace": r["pace"],
             "level": r["level"]})
    last = {}
    for a, pts in trend.items():
        last[a] = pts[-1]
        trend[a] = pts[-20:]
    return {
        "activities": [{"key": k, "distance": v[0], "level": v[1]} for k, v in ACTIVITIES.items()],
        "today": [r for r in rows if r["day"] == day],
        "recent": list(reversed(rows[-8:])),
        "weeks": week_rows,
        "week_minutes": week_rows[-1]["minutes"],
        "target": WEEK_TARGET,
        "trend": trend,
        "last": last,     # last session per activity, to prefill the form
    }


def day_entries(db_path, day):
    return entries(db_path, day, day)


def csv_rows(db_path):
    return [[r["day"], r["ts"][11:16], r["activity"], r["minutes"],
             "" if r["distance"] is None else r["distance"],
             "" if r["pace"] is None else r["pace"],
             "" if r["level"] is None else r["level"],
             "" if r["kcal"] is None else r["kcal"],
             "" if r["hr"] is None else r["hr"]] for r in entries(db_path)]


CSV_HEADER = ["date", "time", "activity", "minutes", "distance_km", "pace_min_per_km", "level_or_incline",
              "kcal", "avg_heart_rate"]
