"""Workout tracker core: storage, planning and progression logic.

Plain native Python (no pyscript features), so it runs at full speed and can
do blocking file/SQLite I/O inside task.executor. The pyscript wrapper in
/config/pyscript/workout.py is the only thing that talks to Home Assistant.

Model
-----
* Main groups (chest, shoulders, back, legs) rotate. Today's suggestion is the
  main group trained least recently (before today). Skipping a day changes
  nothing, and training out of order just updates that group's date.
* Add-on groups (biceps, triceps, abs) are combined with any main group.
  The suggested add-on is again the one trained least recently.
* Exercises come from exercises.yaml plus any added from the dashboard
  (stored in the database). Exercises can be hidden; their history is kept.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime

import yaml

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    day TEXT NOT NULL,
    exercise TEXT NOT NULL,
    set_no INTEGER NOT NULL,
    weight REAL NOT NULL,
    reps INTEGER NOT NULL,
    rpe REAL
);
CREATE INDEX IF NOT EXISTS idx_sets_ex_day ON sets(exercise, day);
CREATE TABLE IF NOT EXISTS exercise_overrides (
    name TEXT PRIMARY KEY,
    grp TEXT,
    step REAL,
    hidden INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS exercise_notes (
    name TEXT PRIMARY KEY,
    note TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS body (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_body_metric_day ON body(metric, day);
CREATE TABLE IF NOT EXISTS gym_visits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start TEXT NOT NULL,
    end TEXT
);
"""

# Body metrics: key, label, unit
METRICS = [
    ("weight", "Body weight", "kg"),
    ("bodyfat", "Body fat", "%"),
    ("waist", "Waist", "cm"),
    ("chest", "Chest", "cm"),
    ("arms", "Arms", "cm"),
    ("thighs", "Thighs", "cm"),
    ("hips", "Hips", "cm"),
    ("height", "Height", "cm"),
]
WARMUP_FACTOR = 0.6

DEFAULT_SETS = 3
DEFAULT_REPS = 15
DEFAULT_STEP = 2.5
DEFAULT_RIR = 1  # reps in reserve assumed when no RPE is logged


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _ex_entry(ex, d_sets, d_reps, d_step):
    return {
        "group": str(ex.get("group", "other")).strip().lower(),
        "step": float(ex.get("step", d_step)),
        "sets": int(ex.get("sets", d_sets)),
        "reps": int(ex.get("reps", d_reps)),
        "start_weight": float(ex.get("start_weight", 0) or 0),
        "hidden": False,
        "custom": False,
    }


def load_config(path: str, db_path: str | None = None) -> dict:
    """Read exercises.yaml, then apply dashboard additions/hides from the DB.

    Returns {"main": [...], "addon": [...], "groups": [...],
             "defaults": {...}, "exercises": {name: {...}}}
    `exercises` includes hidden ones (flag "hidden") so their history still
    maps to a group; lists shown to the user skip them.
    """
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    d = raw.get("defaults") or {}
    d_sets = int(d.get("sets", DEFAULT_SETS))
    d_reps = int(d.get("reps", DEFAULT_REPS))
    d_step = float(d.get("step", DEFAULT_STEP))

    g = raw.get("groups") or {}
    main = [str(x).strip().lower() for x in g.get("main", ["chest", "shoulders", "back", "legs"])]
    addon = [str(x).strip().lower() for x in g.get("addon", ["biceps", "triceps", "abs"])]

    exercises = {}
    for name, ex in (raw.get("exercises") or {}).items():
        exercises[str(name)] = _ex_entry(ex or {}, d_sets, d_reps, d_step)

    if db_path:
        con = _connect(db_path)
        for r in con.execute("SELECT name, grp, step, hidden FROM exercise_overrides"):
            name = r["name"]
            if name in exercises:
                exercises[name]["hidden"] = bool(r["hidden"])
            elif r["grp"]:
                e = _ex_entry({"group": r["grp"], "step": r["step"] or d_step}, d_sets, d_reps, d_step)
                e["hidden"] = bool(r["hidden"])
                e["custom"] = True
                exercises[name] = e
        con.close()

    groups = list(main) + [x for x in addon if x not in main]
    for ex in exercises.values():
        if ex["group"] not in groups:
            groups.append(ex["group"])

    st = raw.get("settings") or {}
    settings = {
        "person": str(st.get("person") or "").strip(),
        "gym_zone": str(st.get("gym_zone") or "").strip(),
        "notify": str(st.get("notify") or "").strip(),
        "min_visit_minutes": int(st.get("min_visit_minutes", 15)),
        "days_per_week": int(st.get("days_per_week", 5)),
        "dashboard_url": str(st.get("dashboard_url") or "").strip(),
    }
    return {"main": main, "addon": addon, "groups": groups,
            "defaults": {"sets": d_sets, "reps": d_reps, "step": d_step},
            "settings": settings, "exercises": exercises}


def visible_exercises(cfg, groups):
    """Names of non-hidden exercises in `groups`, ordered by group then config order."""
    out = []
    for g in groups:
        out += [n for n, e in cfg["exercises"].items() if e["group"] == g and not e["hidden"]]
    return out


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = [r["name"] for r in con.execute("PRAGMA table_info(sets)")]
    if "kind" not in cols:   # added in v2: 'work' or 'warmup'
        con.execute("ALTER TABLE sets ADD COLUMN kind TEXT NOT NULL DEFAULT 'work'")
        con.commit()
    return con


def _today(today: str | None) -> str:
    return today or date.today().isoformat()


def log_set(db_path, exercise, weight, reps, rpe=None, today=None, now=None, kind="work"):
    """Append one set. Set number is counted per exercise per day.
    kind is 'work' or 'warmup'; warm-ups are kept but ignored by the advice,
    records, set counts and charts."""
    day = _today(today)
    rpe = None if rpe in (None, "", 0, 0.0) else float(rpe)
    kind = "warmup" if str(kind).lower() in ("warmup", "warm-up", "true", "1") else "work"
    con = _connect(db_path)
    with con:
        n = con.execute(
            "SELECT COUNT(*) AS n FROM sets WHERE exercise = ? AND day = ?",
            (exercise, day),
        ).fetchone()["n"]
        con.execute(
            "INSERT INTO sets(ts, day, exercise, set_no, weight, reps, rpe, kind) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            ((now or datetime.now()).isoformat(timespec="seconds"), day,
             exercise, n + 1, float(weight), int(reps), rpe, kind),
        )
    con.close()
    return n + 1


def set_rpe_all(db_path, exercise, rpe, today=None, only_missing=False):
    """The 'same for all sets' shortcut: apply one RPE to today's sets."""
    day = _today(today)
    con = _connect(db_path)
    with con:
        sql = "UPDATE sets SET rpe = ? WHERE exercise = ? AND day = ? AND kind = 'work'"
        if only_missing:
            sql += " AND rpe IS NULL"
        cur = con.execute(sql, (float(rpe), exercise, day))
    con.close()
    return cur.rowcount


def undo_last_set(db_path, exercise=None):
    """Delete the most recent set (of `exercise`, if given)."""
    con = _connect(db_path)
    with con:
        if exercise:
            row = con.execute("SELECT * FROM sets WHERE exercise = ? ORDER BY id DESC LIMIT 1",
                              (exercise,)).fetchone()
        else:
            row = con.execute("SELECT * FROM sets ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            con.execute("DELETE FROM sets WHERE id = ?", (row["id"],))
    con.close()
    return dict(row) if row else None


def add_exercise(db_path, name, group, step):
    """Add (or un-hide) an exercise from the dashboard."""
    name = " ".join(str(name).split())
    if not name:
        raise ValueError("empty exercise name")
    con = _connect(db_path)
    with con:
        con.execute(
            "INSERT INTO exercise_overrides(name, grp, step, hidden) VALUES(?, ?, ?, 0) "
            "ON CONFLICT(name) DO UPDATE SET grp = excluded.grp, step = excluded.step, hidden = 0",
            (name, str(group).strip().lower(), float(step)),
        )
    con.close()
    return name


def hide_exercise(db_path, name, hidden=True):
    """Hide an exercise from the lists. Its history stays in the database."""
    con = _connect(db_path)
    with con:
        con.execute(
            "INSERT INTO exercise_overrides(name, hidden) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET hidden = excluded.hidden",
            (name, int(bool(hidden))),
        )
    con.close()


def sessions_for(db_path, exercise, limit=3, until=None, warmups=False):
    """Last `limit` training days for an exercise (up to `until`), newest first.
    Only working sets unless warmups=True."""
    kind = "" if warmups else " AND kind = 'work'"
    con = _connect(db_path)
    days = [r["day"] for r in con.execute(
        "SELECT DISTINCT day FROM sets WHERE exercise = ? AND day <= ?" + kind +
        " ORDER BY day DESC LIMIT ?",
        (exercise, until or "9999-12-31", limit),
    )]
    out = []
    for d in days:
        rows = con.execute(
            "SELECT id, set_no, weight, reps, rpe, kind FROM sets "
            "WHERE exercise = ? AND day = ?" + kind + " ORDER BY set_no",
            (exercise, d),
        ).fetchall()
        out.append({"day": d, "sets": [dict(r) for r in rows]})
    con.close()
    return out


# --------------------------------------------------------------------------
# Strength estimate and progression
# --------------------------------------------------------------------------

def _fmt_w(w):
    return f"{round(w, 2):g}"


def round_step(x, step):
    return max(round(round(x / step) * step, 2), 0.0)


def e1rm(weight, reps, rpe=None):
    """Estimated one-rep max (Epley), counting reps left in the tank.

    RPE 8 means ~2 reps in reserve, so 15 reps @ RPE 8 is treated like 17
    to failure. Without RPE we assume 1 rep in reserve.
    """
    rir = DEFAULT_RIR if rpe is None else max(0.0, min(5.0, 10.0 - rpe))
    return weight * (1 + (reps + rir) / 30.0)


def session_strength(sess):
    """Best estimated max across all sets of a session."""
    return max((e1rm(s["weight"], s["reps"], s["rpe"]) for s in sess["sets"]), default=0.0)


def summarize_session(sess):
    """'Mon 12 Oct · 3×15 @ 30 kg · RPE 7–8' (handles mixed weights/reps)."""
    sets = sess["sets"]
    d = date.fromisoformat(sess["day"])
    label = d.strftime("%a %d %b")
    weights = {s["weight"] for s in sets}
    reps = [s["reps"] for s in sets]
    unit = " kg" if any(s["weight"] for s in sets) else ""
    if len(weights) == 1 and len(set(reps)) == 1:
        w = f" @ {_fmt_w(sets[0]['weight'])}{unit}" if unit else " (bodyweight)"
        body = f"{len(sets)}×{reps[0]}{w}"
    elif len(weights) == 1:
        w = f" @ {_fmt_w(sets[0]['weight'])}{unit}" if unit else " (bodyweight)"
        body = f"{'/'.join(map(str, reps))}{w}"
    else:
        body = ", ".join(f"{s['reps']}×{_fmt_w(s['weight'])}" for s in sets) + unit
    rpes = [s["rpe"] for s in sets if s["rpe"] is not None]
    if rpes:
        lo, hi = min(rpes), max(rpes)
        body += f" · RPE {_fmt_w(lo)}" if lo == hi else f" · RPE {_fmt_w(lo)}–{_fmt_w(hi)}"
    return f"{label} · {body}"


def _working(sess):
    """Sets at the session's top weight (lighter warm-up/back-off sets ignored)."""
    top = max(s["weight"] for s in sess["sets"])
    return top, [s for s in sess["sets"] if s["weight"] == top]


def suggest(ex_cfg, sessions):
    """Advice for the next session of one exercise.

    Target is `sets` × `reps` (default 3×15), but any mix of weight, reps and
    set count is understood through the strength estimate.

      increase  all target sets hit the reps at RPE ≤ 8 (or, without RPE, two
                sessions in a row), or the weight was clearly too light
                (5+ reps over target).
      reduce    the last session shows you couldn't do the target reps at that
                weight (e.g. a heavy day of 10 reps); the suggestion is the
                weight that fits the target reps. Also after RPE 10 twice.
      hold      everything else.
    Returns {"action", "weight", "reps", "reason"}.
    """
    t_sets, t_reps, step = ex_cfg["sets"], ex_cfg["reps"], ex_cfg["step"]
    if not sessions:
        w = ex_cfg.get("start_weight") or 0
        return {"action": "start", "weight": w, "reps": t_reps,
                "reason": f"No history yet. Pick a weight you can do for {t_reps} reps with 2–3 left."}

    last = sessions[0]
    top, work = _working(last)
    work = work[:t_sets] if len(work) >= t_sets else work
    rpes = [s["rpe"] for s in work if s["rpe"] is not None]
    max_rpe = max(rpes) if rpes else None
    hit = len(work) >= t_sets and all(s["reps"] >= t_reps for s in work)
    reps_txt = "/".join(str(s["reps"]) for s in work)
    best = session_strength(last)

    def rpe10(sess):
        return any(s["rpe"] is not None and s["rpe"] >= 10 for s in sess["sets"])

    def prev_hit_same():
        if len(sessions) < 2:
            return False
        p_top, p_work = _working(sessions[1])
        return (p_top == top and len(p_work) >= t_sets
                and all(s["reps"] >= t_reps for s in p_work[:t_sets]))

    # Bodyweight (logged as 0 kg): progress on reps.
    if top == 0:
        if hit and (max_rpe is None or max_rpe <= 8):
            more = min(s["reps"] for s in work) + 2
            return {"action": "increase", "weight": 0, "reps": more,
                    "reason": f"Hit {t_sets}×{t_reps}. Go for {more} reps, or hold a light plate."}
        return {"action": "hold", "weight": 0, "reps": t_reps,
                "reason": f"Last time {reps_txt}. Aim for {t_sets}×{t_reps}."}

    if len(sessions) >= 2 and rpe10(sessions[0]) and rpe10(sessions[1]):
        return {"action": "reduce", "weight": max(top - step, 0), "reps": t_reps,
                "reason": "RPE 10 two sessions running. Drop a step and rebuild."}

    if hit and max_rpe is not None and max_rpe <= 8:
        return {"action": "increase", "weight": top + step, "reps": t_reps,
                "reason": f"Hit {t_sets}×{t_reps} @ {_fmt_w(top)} kg at RPE ≤ {_fmt_w(max_rpe)}."}
    if hit and max_rpe is None and prev_hit_same():
        return {"action": "increase", "weight": top + step, "reps": t_reps,
                "reason": f"Hit {t_sets}×{t_reps} @ {_fmt_w(top)} kg two sessions in a row."}

    if any(s["reps"] >= t_reps + 5 for s in work) and (max_rpe is None or max_rpe <= 9):
        w = round_step(best / (1 + (t_reps + 2) / 30.0), step)
        w = min(max(w, top + step), top + 2 * step)
        return {"action": "increase", "weight": w, "reps": t_reps,
                "reason": f"{reps_txt} @ {_fmt_w(top)} kg was light. Go up to {_fmt_w(w)} kg."}

    # What could you do for the target reps, to failure?
    w_fail = best / (1 + t_reps / 30.0)
    if w_fail < top - step:
        w = round_step(w_fail, step)
        if w >= top:
            w = top - step
        return {"action": "reduce", "weight": w, "reps": t_reps,
                "reason": f"{reps_txt} @ {_fmt_w(top)} kg is about {t_reps} reps at "
                          f"{w_fail:.0f} kg. Use {_fmt_w(w)} kg for {t_sets}×{t_reps}."}

    if hit and max_rpe is not None:
        why = f"Hit {t_sets}×{t_reps} but it was hard (RPE {_fmt_w(max_rpe)}). Repeat until it's ≤ 8."
    elif hit:
        why = f"Hit {t_sets}×{t_reps}. Log RPE, or hit it once more, to unlock an increase."
    else:
        why = f"Last time {reps_txt} @ {_fmt_w(top)} kg. Aim for {t_sets}×{t_reps}."
    return {"action": "hold", "weight": top, "reps": t_reps, "reason": why}


def exercise_card(db_path, name, ex_cfg, history=3, today=None, series=8):
    day = _today(today)
    sess = sessions_for(db_path, name, limit=max(history + 2, series + 1), until=day)
    past = [s for s in sess if s["day"] != day]
    advice = suggest(ex_cfg, past)
    today_all = sessions_for(db_path, name, limit=1, until=day, warmups=True)
    today_sets = today_all[0]["sets"] if today_all and today_all[0]["day"] == day else []
    work_today = [s for s in today_sets if s["kind"] == "work"]

    # Did you warm up last time? Then default the first set to a warm-up.
    prev_all = [s for s in sessions_for(db_path, name, limit=2, until=day, warmups=True) if s["day"] != day]
    warmup_habit = (prev_all[0]["sets"][0]["kind"] == "warmup") if prev_all else True
    wu_weight = round_step(advice["weight"] * WARMUP_FACTOR, ex_cfg["step"]) if advice["weight"] else 0

    strength = round(session_strength(past[0]), 1) if past else None
    trend = None
    if len(past) >= 2:
        prev = session_strength(past[1])
        if prev > 0:
            trend = round((session_strength(past[0]) / prev - 1) * 100, 1)

    return {
        "name": name,
        "group": ex_cfg["group"],
        "target": f"{ex_cfg['sets']}×{ex_cfg['reps']}",
        "target_sets": ex_cfg["sets"],
        "step": ex_cfg["step"],
        "history": [summarize_session(s) for s in past[:history]],
        "last_day": past[0]["day"] if past else None,
        "strength": strength,
        "trend": trend,
        "series": [{"day": s["day"], "strength": round(session_strength(s), 1)}
                   for s in reversed(past[:series])],
        "today": [{"id": s["id"], "set_no": s["set_no"], "weight": s["weight"], "reps": s["reps"],
                   "rpe": s["rpe"], "kind": s["kind"]} for s in today_sets],
        "today_done": len(work_today),
        "warmup_default": bool(warmup_habit and not today_sets),
        "warmup_weight": wu_weight,
        "note": get_note(db_path, name),
        "suggestion": advice,
        "default_weight": advice["weight"],
        "default_reps": advice["reps"],
    }


def group_view(db_path, cfg, groups, history=3, today=None):
    """Cards for every visible exercise in `groups`."""
    return [exercise_card(db_path, n, cfg["exercises"][n], history, today)
            for n in visible_exercises(cfg, [g.lower() for g in groups])]


# --------------------------------------------------------------------------
# Planning: which groups are due
# --------------------------------------------------------------------------

def group_last_trained(db_path, cfg, before=None):
    """{group: last day trained}, only counting days before `before` if given."""
    con = _connect(db_path)
    sql = "SELECT exercise, MAX(day) AS day FROM sets"
    args = ()
    if before:
        sql += " WHERE day < ?"
        args = (before,)
    rows = con.execute(sql + " GROUP BY exercise", args).fetchall()
    con.close()
    last = {}
    for r in rows:
        ex = cfg["exercises"].get(r["exercise"])
        if not ex:
            continue
        g = ex["group"]
        if g not in last or r["day"] > last[g]:
            last[g] = r["day"]
    return last


def plan(db_path, cfg, today=None):
    """Today's suggestion: the main group and add-on trained least recently.

    Only days before today count, so the suggestion stays put while you train.
    Never-trained groups come first, in config order.
    """
    day = _today(today)
    before = group_last_trained(db_path, cfg, before=day)
    all_last = group_last_trained(db_path, cfg)
    d0 = date.fromisoformat(day)

    def rows(groups):
        out = []
        for i, g in enumerate(groups):
            ld = before.get(g)
            out.append({"group": g, "last": ld,
                        "days": (d0 - date.fromisoformat(ld)).days if ld else None,
                        "today": all_last.get(g) == day, "_order": i})
        return out

    def stalest(rs):
        if not rs:
            return None
        return min(rs, key=lambda r: (r["last"] or "", r["_order"]))["group"]

    main, addon = rows(cfg["main"]), rows(cfg["addon"])
    nxt_main, nxt_addon = stalest(main), stalest(addon)
    for r in main + addon:
        del r["_order"]
    return {"next_main": nxt_main, "next_addon": nxt_addon,
            "main": main, "addon": addon,
            "trained_today": [g for g in cfg["groups"] if all_last.get(g) == day]}


# --------------------------------------------------------------------------
# Overview statistics
# --------------------------------------------------------------------------

def stats(db_path, cfg, today=None, weeks=12, series=30, days_per_week=None):
    """Everything the Overview tab draws, in one dict (kept compact)."""
    from datetime import timedelta
    if days_per_week is None:
        days_per_week = cfg.get("settings", {}).get("days_per_week", 5)

    day = _today(today)
    d0 = date.fromisoformat(day)
    week0 = d0 - timedelta(days=d0.weekday())            # Monday this week
    start = week0 - timedelta(weeks=weeks - 1)
    group_of = {n: e["group"] for n, e in cfg["exercises"].items()}

    con = _connect(db_path)
    rows = con.execute(
        "SELECT day, exercise, weight, reps, rpe FROM sets WHERE kind = 'work' ORDER BY day, id").fetchall()
    con.close()

    # ---- per day
    per_day = {}
    for r in rows:
        g = group_of.get(r["exercise"], "other")
        d = per_day.setdefault(r["day"], {"sets": 0, "volume": 0.0, "groups": {}})
        d["sets"] += 1
        d["volume"] += r["weight"] * r["reps"]
        d["groups"][g] = d["groups"].get(g, 0) + 1

    def ordered_groups(gmap):
        return [g for g in cfg["groups"] if g in gmap] + [g for g in gmap if g not in cfg["groups"]]

    calendar = []
    for i in range((d0 - start).days + 1):
        dd = (start + timedelta(days=i)).isoformat()
        info = per_day.get(dd)
        calendar.append({"day": dd, "sets": info["sets"] if info else 0,
                         "groups": ordered_groups(info["groups"]) if info else []})

    week_rows = []
    for w in range(weeks):
        ws = start + timedelta(weeks=w)
        days = [(ws + timedelta(days=i)).isoformat() for i in range(7)]
        sets = sum(per_day[x]["sets"] for x in days if x in per_day)
        by_group = {}
        for x in days:
            for g, n in per_day.get(x, {"groups": {}})["groups"].items():
                by_group[g] = by_group.get(g, 0) + n
        week_rows.append({
            "start": ws.isoformat(),
            "workouts": sum(1 for x in days if x in per_day),
            "sets": sets,
            "volume": round(sum(per_day[x]["volume"] for x in days if x in per_day)),
            "by_group": by_group,
        })

    this_week = []
    for i in range(7):
        dd = (week0 + timedelta(days=i)).isoformat()
        info = per_day.get(dd)
        this_week.append({"day": dd, "sets": info["sets"] if info else 0,
                          "groups": ordered_groups(info["groups"]) if info else [],
                          "future": dd > day})

    # ---- muscle balance, last 28 days
    since = (d0 - timedelta(days=27)).isoformat()
    last_seen = group_last_trained(db_path, cfg)
    balance = []
    for g in cfg["main"] + cfg["addon"]:
        n = sum(per_day[x]["groups"].get(g, 0) for x in per_day if x >= since)
        ld = last_seen.get(g)
        balance.append({"group": g, "sets": n, "last": ld,
                        "days": (d0 - date.fromisoformat(ld)).days if ld else None,
                        "addon": g in cfg["addon"]})

    # ---- per-exercise progression + personal bests
    by_ex = {}
    for r in rows:
        by_ex.setdefault(r["exercise"], {}).setdefault(r["day"], []).append(
            {"weight": r["weight"], "reps": r["reps"], "rpe": r["rpe"]})
    exercises, prs = [], []
    recent = (d0 - timedelta(days=14)).isoformat()
    for name, days in by_ex.items():
        ex = cfg["exercises"].get(name)
        if not ex:
            continue
        pts = []
        best_s, best_s_day, best_w, best_w_day = 0.0, None, 0.0, None
        for dd in sorted(days):
            sets_ = days[dd]
            top = max(s["weight"] for s in sets_)
            st = session_strength({"sets": sets_})
            top_reps = max(s["reps"] for s in sets_ if s["weight"] == top)
            pts.append({"day": dd, "top": top, "reps": top_reps, "strength": round(st, 1),
                        "sets": len(sets_)})
            if st > best_s + 1e-9:
                best_s, best_s_day = st, dd
            if top > best_w + 1e-9:
                best_w, best_w_day = top, dd
        exercises.append({"name": name, "group": ex["group"], "hidden": ex["hidden"],
                          "last": pts[-1]["day"], "sessions": len(pts),
                          "points": pts[-series:]})
        if best_w > 0:
            prs.append({"name": name, "group": ex["group"],
                        "weight": best_w, "weight_day": best_w_day,
                        "strength": round(best_s, 1), "strength_day": best_s_day,
                        "new": (best_w_day or "") >= recent})
    exercises.sort(key=lambda e: e["last"], reverse=True)
    prs.sort(key=lambda p: p["weight_day"] or "", reverse=True)

    return {
        "today": day,
        "target_days": days_per_week,
        "this_week": this_week,
        "workouts_this_week": sum(1 for d in this_week if d["sets"]),
        "weeks": week_rows,
        "calendar": calendar,
        "balance": balance,
        "exercises": exercises,
        "prs": prs[:10],
        "total_workouts": len(per_day),
        "days": history_days(db_path, cfg),
        "gym": gym_stats(db_path, day, weeks),
        "body": body_stats(db_path, day),
    }


# --------------------------------------------------------------------------
# Day view / editing past days
# --------------------------------------------------------------------------

def day_detail(db_path, cfg, day):
    """All sets of one day, grouped by exercise in the order they were done."""
    con = _connect(db_path)
    rows = con.execute(
        "SELECT id, exercise, set_no, weight, reps, rpe, kind FROM sets WHERE day = ? ORDER BY id",
        (day,)).fetchall()
    con.close()
    blocks, index = [], {}
    for r in rows:
        name = r["exercise"]
        if name not in index:
            ex = cfg["exercises"].get(name, {})
            index[name] = len(blocks)
            blocks.append({"name": name, "group": ex.get("group", "other"),
                           "step": ex.get("step", DEFAULT_STEP), "sets": []})
        blocks[index[name]]["sets"].append(
            {"id": r["id"], "weight": r["weight"], "reps": r["reps"], "rpe": r["rpe"], "kind": r["kind"]})
    groups = []
    for b in blocks:
        if b["group"] not in groups:
            groups.append(b["group"])
    return {"day": day, "exercises": blocks, "groups": groups,
            "sets": sum(1 for b in blocks for x in b["sets"] if x["kind"] == "work"),
            "warmups": sum(1 for b in blocks for x in b["sets"] if x["kind"] == "warmup"),
            "visit": visit_on(db_path, day)}


def _renumber(con, exercise, day):
    ids = [r["id"] for r in con.execute(
        "SELECT id FROM sets WHERE exercise = ? AND day = ? ORDER BY id", (exercise, day))]
    for i, sid in enumerate(ids, 1):
        con.execute("UPDATE sets SET set_no = ? WHERE id = ?", (i, sid))


def update_set(db_path, set_id, weight=None, reps=None, rpe="keep", kind=None):
    """Change one logged set. rpe=None or 0 clears it; 'keep' leaves it."""
    con = _connect(db_path)
    with con:
        row = con.execute("SELECT * FROM sets WHERE id = ?", (int(set_id),)).fetchone()
        if not row:
            con.close()
            return None
        w = float(weight) if weight is not None else row["weight"]
        r = int(reps) if reps is not None else row["reps"]
        if rpe == "keep":
            e = row["rpe"]
        else:
            e = None if rpe in (None, "", 0, 0.0) else float(rpe)
        k = row["kind"] if kind is None else ("warmup" if str(kind).lower() in ("warmup", "true", "1") else "work")
        con.execute("UPDATE sets SET weight = ?, reps = ?, rpe = ?, kind = ? WHERE id = ?",
                    (w, max(r, 1), e, k, int(set_id)))
    con.close()
    return {"id": int(set_id), "weight": w, "reps": max(r, 1), "rpe": e, "kind": k}


def delete_set(db_path, set_id):
    con = _connect(db_path)
    with con:
        row = con.execute("SELECT exercise, day FROM sets WHERE id = ?", (int(set_id),)).fetchone()
        if not row:
            con.close()
            return None
        con.execute("DELETE FROM sets WHERE id = ?", (int(set_id),))
        _renumber(con, row["exercise"], row["day"])
    con.close()
    return dict(row)


def add_set_on(db_path, cfg, exercise, day, weight=None, reps=None, rpe=None):
    """Add a set to any day. Missing weight/reps copy that day's last set of the
    exercise, else the most recent earlier session, else the targets."""
    ex = cfg["exercises"].get(exercise) or {"reps": DEFAULT_REPS}
    if weight is None or reps is None:
        same = [s for s in day_detail(db_path, cfg, day)["exercises"] if s["name"] == exercise]
        work = [x for x in same[0]["sets"] if x["kind"] == "work"] if same else []
        if work:
            last = work[-1]
            weight = last["weight"] if weight is None else weight
            reps = last["reps"] if reps is None else reps
        else:
            prev = [s for s in sessions_for(db_path, exercise, 1, until=day) if s["day"] < day]
            if not prev:
                prev = sessions_for(db_path, exercise, 1)
            if prev:
                top, work = _working(prev[0])
                weight = top if weight is None else weight
                reps = work[0]["reps"] if reps is None else reps
            weight = (ex.get("start_weight") or 0) if weight is None else weight
            reps = ex.get("reps", DEFAULT_REPS) if reps is None else reps
    return log_set(db_path, exercise, weight, reps, rpe, today=day)


def history_days(db_path, cfg):
    """Compact list of every training day: [day, sets, [groups]]."""
    group_of = {n: e["group"] for n, e in cfg["exercises"].items()}
    con = _connect(db_path)
    rows = con.execute("SELECT day, exercise, SUM(kind = 'work') AS n FROM sets GROUP BY day, exercise ORDER BY day").fetchall()
    con.close()
    out, cur = [], None
    for r in rows:
        if not cur or cur[0] != r["day"]:
            cur = [r["day"], 0, []]
            out.append(cur)
        cur[1] += r["n"]
        g = group_of.get(r["exercise"], "other")
        if g not in cur[2]:
            cur[2].append(g)
    order = {g: i for i, g in enumerate(cfg["groups"])}
    for d in out:
        d[2].sort(key=lambda g: order.get(g, 99))
    return out


# --------------------------------------------------------------------------
# Exercise notes
# --------------------------------------------------------------------------

def get_note(db_path, exercise):
    con = _connect(db_path)
    r = con.execute("SELECT note FROM exercise_notes WHERE name = ?", (exercise,)).fetchone()
    con.close()
    return r["note"] if r else ""


def set_note(db_path, exercise, note):
    """Save (or clear, with an empty note) the standing note for an exercise."""
    note = str(note or "").strip()[:500]
    con = _connect(db_path)
    with con:
        if note:
            con.execute(
                "INSERT INTO exercise_notes(name, note, updated) VALUES(?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET note = excluded.note, updated = excluded.updated",
                (exercise, note, datetime.now().isoformat(timespec="seconds")))
        else:
            con.execute("DELETE FROM exercise_notes WHERE name = ?", (exercise,))
    con.close()
    return note


# --------------------------------------------------------------------------
# Personal records
# --------------------------------------------------------------------------

def check_pr(db_path, exercise):
    """Did the most recent set of `exercise` beat every earlier working set?

    Returns None, or {"exercise", "weight", "reps", "set_id", "records": [...]}
    where each record is {"type", "label", "value", "previous"}. The very first
    session of an exercise doesn't count as a record.
    """
    con = _connect(db_path)
    last = con.execute(
        "SELECT * FROM sets WHERE exercise = ? ORDER BY id DESC LIMIT 1", (exercise,)).fetchone()
    if not last or last["kind"] != "work":
        con.close()
        return None
    prior = con.execute(
        "SELECT weight, reps, rpe FROM sets WHERE exercise = ? AND kind = 'work' AND id < ? AND day < ?",
        (exercise, last["id"], last["day"])).fetchall()
    same_day = con.execute(
        "SELECT weight, reps, rpe FROM sets WHERE exercise = ? AND kind = 'work' AND id < ? AND day = ?",
        (exercise, last["id"], last["day"])).fetchall()
    con.close()
    if not prior:
        return None
    earlier = list(prior) + list(same_day)
    best_w = max(r["weight"] for r in earlier)
    best_e = max(e1rm(r["weight"], r["reps"], r["rpe"]) for r in earlier)
    w, reps, rpe = last["weight"], last["reps"], last["rpe"]
    est = e1rm(w, reps, rpe)
    records = []
    if w > 0 and w > best_w + 1e-9:
        records.append({"type": "weight", "label": "Heaviest weight",
                        "value": w, "previous": best_w})
    if w > 0 and est > best_e + 0.05:
        records.append({"type": "e1rm", "label": "Best estimated max",
                        "value": round(est, 1), "previous": round(best_e, 1)})
    if w == 0:
        best_r = max((r["reps"] for r in earlier if r["weight"] == 0), default=0)
        if reps > best_r:
            records.append({"type": "reps", "label": "Most reps", "value": reps, "previous": best_r})
    if not records:
        return None
    return {"exercise": exercise, "weight": w, "reps": reps, "set_id": last["id"],
            "day": last["day"], "ts": last["ts"], "records": records}


# --------------------------------------------------------------------------
# Body weight and measurements
# --------------------------------------------------------------------------

METRIC_KEYS = [m[0] for m in METRICS]


def add_body(db_path, metric, value, day=None):
    metric = str(metric).strip().lower()
    if metric not in METRIC_KEYS:
        raise ValueError(f"unknown metric {metric}")
    day = _today(day)
    con = _connect(db_path)
    with con:
        # one value per metric per day: a second entry replaces the first
        con.execute("DELETE FROM body WHERE metric = ? AND day = ?", (metric, day))
        cur = con.execute("INSERT INTO body(day, metric, value) VALUES(?, ?, ?)",
                          (day, metric, float(value)))
    con.close()
    return cur.lastrowid


def delete_body(db_path, entry_id):
    con = _connect(db_path)
    with con:
        con.execute("DELETE FROM body WHERE id = ?", (int(entry_id),))
    con.close()


def body_stats(db_path, today=None, days=180):
    from datetime import timedelta
    day = _today(today)
    d0 = date.fromisoformat(day)
    since = (d0 - timedelta(days=days)).isoformat()
    month_ago = (d0 - timedelta(days=30)).isoformat()
    con = _connect(db_path)
    rows = con.execute("SELECT id, day, metric, value FROM body ORDER BY day, id").fetchall()
    con.close()
    metrics = []
    for key, label, unit in METRICS:
        entries = [r for r in rows if r["metric"] == key]
        if not entries:
            metrics.append({"key": key, "label": label, "unit": unit, "latest": None})
            continue
        latest = entries[-1]
        base = [r for r in entries if r["day"] <= month_ago]
        ref = base[-1] if base else entries[0]
        change = round(latest["value"] - ref["value"], 1) if ref["id"] != latest["id"] else None
        metrics.append({
            "key": key, "label": label, "unit": unit,
            "latest": latest["value"], "latest_day": latest["day"],
            "change": change, "change_since": ref["day"] if change is not None else None,
            "series": [[r["day"], r["value"]] for r in entries if r["day"] >= since],
        })
    recent = [{"id": r["id"], "day": r["day"], "metric": r["metric"], "value": r["value"]}
              for r in rows[-12:]][::-1]
    return {"metrics": metrics, "recent": recent, "derived": derived_measures(metrics)}


def _bmi_range(bmi):
    # WHO adult categories
    if bmi < 18.5:
        return "underweight range"
    if bmi < 25:
        return "healthy range"
    if bmi < 30:
        return "overweight range"
    return "obese range"


def derived_measures(metrics):
    """BMI, waist-to-height ratio, lean mass and FFMI from the latest values.
    Each needs height; the others need weight, waist or body fat logged."""
    m = {x["key"]: x for x in metrics}
    h = (m.get("height") or {}).get("latest")
    w = (m.get("weight") or {}).get("latest")
    waist = (m.get("waist") or {}).get("latest")
    bf = (m.get("bodyfat") or {}).get("latest")
    out = []
    if not h:
        return out
    hm = h / 100.0
    if w:
        bmi = w / (hm * hm)
        item = {"key": "bmi", "label": "BMI", "value": round(bmi, 1), "unit": "",
                "note": _bmi_range(bmi)}
        ch = (m.get("weight") or {}).get("change")
        if ch:
            item["change"] = round(bmi - (w - ch) / (hm * hm), 1)
            item["change_since"] = m["weight"]["change_since"]
        out.append(item)
    if waist:
        r = waist / h
        out.append({"key": "whtr", "label": "Waist to height", "value": round(r, 2), "unit": "",
                    "note": "below 0.5 is the usual target" if r >= 0.5 else "under 0.5"})
    if w and bf:
        lean = w * (1 - bf / 100.0)
        ffmi = lean / (hm * hm)
        out.append({"key": "lean", "label": "Lean mass", "value": round(lean, 1), "unit": "kg",
                    "note": f"{round(w - lean, 1)} kg fat"})
        out.append({"key": "ffmi", "label": "FFMI", "value": round(ffmi, 1), "unit": "",
                    "note": "lean mass for your height"})
    return out


# --------------------------------------------------------------------------
# Gym visits (zone arrival / departure)
# --------------------------------------------------------------------------

def open_visit(db_path):
    con = _connect(db_path)
    r = con.execute("SELECT * FROM gym_visits WHERE end IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    con.close()
    return dict(r) if r else None


def start_visit(db_path, now=None):
    """Arrived at the gym. A visit that's already open is kept, unless it is
    from an earlier day (a missed departure), which is closed first."""
    now = now or datetime.now()
    con = _connect(db_path)
    try:
        with con:
            r = con.execute("SELECT * FROM gym_visits WHERE end IS NULL ORDER BY id DESC LIMIT 1").fetchone()
            if r and r["start"][:10] == now.date().isoformat():
                return dict(r)
            if r:
                _close_stale(con, r)
            cur = con.execute("INSERT INTO gym_visits(start) VALUES(?)", (now.isoformat(timespec="seconds"),))
            return {"id": cur.lastrowid, "start": now.isoformat(timespec="seconds"), "end": None}
    finally:
        con.close()


def _close_stale(con, visit):
    """A visit left open (departure missed): end it at the last set logged
    that day plus 5 minutes, or drop it if nothing was logged."""
    from datetime import timedelta
    last = con.execute("SELECT MAX(ts) AS ts FROM sets WHERE day = ? AND ts >= ?",
                       (visit["start"][:10], visit["start"])).fetchone()["ts"]
    if last:
        end = datetime.fromisoformat(last) + timedelta(minutes=5)
        con.execute("UPDATE gym_visits SET end = ? WHERE id = ?",
                    (end.isoformat(timespec="seconds"), visit["id"]))
    else:
        con.execute("DELETE FROM gym_visits WHERE id = ?", (visit["id"],))


def end_visit(db_path, now=None, min_minutes=15):
    """Left the gym. Visits shorter than min_minutes (driving past) are dropped.
    Returns the closed visit with its minutes, or None."""
    now = now or datetime.now()
    con = _connect(db_path)
    try:
        with con:
            r = con.execute("SELECT * FROM gym_visits WHERE end IS NULL ORDER BY id DESC LIMIT 1").fetchone()
            if not r:
                return None
            minutes = (now - datetime.fromisoformat(r["start"])).total_seconds() / 60
            if minutes > 6 * 60:          # departure missed for hours: close at last set
                _close_stale(con, r)
                return None
            if minutes < min_minutes:     # drove past / brief stop
                con.execute("DELETE FROM gym_visits WHERE id = ?", (r["id"],))
                return None
            con.execute("UPDATE gym_visits SET end = ? WHERE id = ?",
                        (now.isoformat(timespec="seconds"), r["id"]))
            return {"id": r["id"], "start": r["start"], "end": now.isoformat(timespec="seconds"),
                    "minutes": round(minutes)}
    finally:
        con.close()


def visit_on(db_path, day):
    con = _connect(db_path)
    rows = con.execute("SELECT start, end FROM gym_visits WHERE substr(start, 1, 10) = ? ORDER BY id",
                       (day,)).fetchall()
    con.close()
    out = []
    for r in rows:
        mins = None
        if r["end"]:
            mins = round((datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).total_seconds() / 60)
        out.append({"start": r["start"][11:16], "end": r["end"][11:16] if r["end"] else None, "minutes": mins})
    return out


def day_minutes(db_path, since=None):
    """Minutes in the gym per day: from recorded visits, otherwise estimated
    from the first to the last logged set (+5 min). {day: (minutes, estimated)}"""
    from datetime import timedelta
    con = _connect(db_path)
    vis = con.execute("SELECT start, end FROM gym_visits WHERE end IS NOT NULL").fetchall()
    sets = con.execute("SELECT day, MIN(ts) AS a, MAX(ts) AS b, COUNT(*) AS n FROM sets GROUP BY day").fetchall()
    con.close()
    out = {}
    for v in vis:
        d = v["start"][:10]
        m = (datetime.fromisoformat(v["end"]) - datetime.fromisoformat(v["start"])).total_seconds() / 60
        out[d] = (out.get(d, (0, False))[0] + m, False)
    for r in sets:
        if r["day"] in out or r["n"] < 2:
            continue
        m = (datetime.fromisoformat(r["b"]) - datetime.fromisoformat(r["a"])).total_seconds() / 60 + 5
        if 10 <= m <= 240:
            out[r["day"]] = (m, True)
    if since:
        out = {d: v for d, v in out.items() if d >= since}
    return out


def gym_stats(db_path, today=None, weeks=12):
    from datetime import timedelta
    day = _today(today)
    d0 = date.fromisoformat(day)
    week0 = d0 - timedelta(days=d0.weekday())
    start = week0 - timedelta(weeks=weeks - 1)
    per_day = day_minutes(db_path, since=start.isoformat())
    rows = []
    for w in range(weeks):
        ws = start + timedelta(weeks=w)
        days = [(ws + timedelta(days=i)).isoformat() for i in range(7)]
        mins = sum(per_day[d][0] for d in days if d in per_day)
        est = sum(per_day[d][0] for d in days if d in per_day and per_day[d][1])
        visits = sum(1 for d in days if d in per_day)
        rows.append({"start": ws.isoformat(), "minutes": round(mins), "estimated_minutes": round(est),
                     "visits": visits})
    # Average over complete weeks since tracking started (current week excluded)
    first = next((i for i, r in enumerate(rows) if r["minutes"]), None)
    complete = rows[first:-1] if first is not None else []
    avg_week = round(sum(r["minutes"] for r in complete) / len(complete)) if complete else None
    sessions = [v[0] for v in per_day.values()]
    avg_session = round(sum(sessions) / len(sessions)) if sessions else None
    return {"weeks": rows, "avg_week_minutes": avg_week, "avg_weeks_counted": len(complete),
            "avg_session_minutes": avg_session,
            "this_week_minutes": rows[-1]["minutes"] if rows else 0,
            "open_visit": open_visit(db_path)}


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def export_csv(db_path, cfg, out_dir, prefix="workout"):
    """Write CSVs of all sets, body entries and gym visits to out_dir.
    Old exports in that folder are removed. Returns the file names."""
    import csv
    import secrets
    os.makedirs(out_dir, exist_ok=True)
    for f in os.listdir(out_dir):
        if f.startswith(prefix + "-") and f.endswith(".csv"):
            os.remove(os.path.join(out_dir, f))
    tag = datetime.now().strftime("%Y%m%d") + "-" + secrets.token_hex(6)
    con = _connect(db_path)
    files = []

    def write(name, header, rows):
        fn = f"{prefix}-{name}-{tag}.csv"
        with open(os.path.join(out_dir, fn), "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
        files.append({"name": name, "file": fn, "rows": len(rows)})

    group_of = {n: e["group"] for n, e in cfg["exercises"].items()}
    sets = con.execute("SELECT * FROM sets ORDER BY day, id").fetchall()
    write("sets", ["date", "time", "exercise", "group", "set", "type", "weight_kg", "reps", "rpe", "estimated_max_kg"],
          [[r["day"], r["ts"][11:16], r["exercise"], group_of.get(r["exercise"], ""), r["set_no"],
            r["kind"], r["weight"], r["reps"], "" if r["rpe"] is None else r["rpe"],
            round(e1rm(r["weight"], r["reps"], r["rpe"]), 1)] for r in sets])
    body = con.execute("SELECT day, metric, value FROM body ORDER BY day, metric").fetchall()
    units = {k: u for k, _, u in METRICS}
    write("body", ["date", "metric", "value", "unit"],
          [[r["day"], r["metric"], r["value"], units.get(r["metric"], "")] for r in body])
    vis = con.execute("SELECT start, end FROM gym_visits ORDER BY start").fetchall()
    write("gym-visits", ["start", "end", "minutes"],
          [[r["start"], r["end"] or "",
            round((datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])).total_seconds() / 60)
            if r["end"] else ""] for r in vis])
    con.close()
    return {"files": files, "created": datetime.now().isoformat(timespec="seconds")}
