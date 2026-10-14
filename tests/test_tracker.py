"""Tracker tests with a stand-in for Home Assistant."""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
# import the package modules without Home Assistant installed
pkg = types.ModuleType("workout_tracker")
pkg.__path__ = [os.path.join(HERE, "..", "custom_components", "workout_tracker")]
sys.modules["workout_tracker"] = pkg
from workout_tracker.tracker import Hub, Tracker  # noqa: E402

CFG = os.path.join(HERE, "..", "custom_components", "workout_tracker", "exercises.yaml")


class FakeHost:
    def __init__(self, states=None, services=()):
        self.states = states or {}
        self.services = set(services)
        self.calls, self.events, self.timers = [], [], []

    async def run(self, func, *args):
        return func(*args)

    def entity_ids(self, domain):
        return [e for e in self.states if e.startswith(domain + ".")]

    def attributes(self, entity_id):
        return self.states.get(entity_id) if entity_id else None

    def has_service(self, d, s):
        return f"{d}.{s}" in self.services

    async def call_service(self, d, s, data):
        self.calls.append((d, s, data))

    def fire(self, ev, data):
        self.events.append((ev, data))

    def later(self, seconds, fn):
        timer = {"seconds": seconds, "fn": fn, "cancelled": False}
        self.timers.append(timer)
        return lambda: timer.update(cancelled=True)

    async def run_timers(self):
        due, self.timers = self.timers, []
        for t in due:
            if not t["cancelled"]:
                await t["fn"]()


def run(coro):
    return asyncio.run(coro)


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = FakeHost(
            states={"person.alex": {"friendly_name": "Alex", "device_trackers": ["device_tracker.alex_iphone"]},
                    "person.sam": {"friendly_name": "Sam", "device_trackers": ["device_tracker.sam_laptop"]},
                    "zone.gym": {"friendly_name": "Gym"}},
            services={"notify.mobile_app_alex_iphone"})
        self.pushes = []
        self.t = Tracker(self.host, os.path.join(self.tmp, "w.db"), CFG, os.path.join(self.tmp, "exp"),
                         "/workout_tracker/export",
                         {"gym_zone": "zone.gym", "person": "auto", "notify": "auto", "days_per_week": 4})
        self.t.subscribe(lambda b: self.pushes.append(b))
        run(self.t.start())

    def b(self, part):
        return self.t.bundle[part]

    def test_start_publishes_everything(self):
        for part in ("plan", "view", "selected", "day", "stats", "export"):
            self.assertIn(part, self.t.bundle)
        self.assertEqual(self.b("plan")["attributes"]["current_main"], "chest")
        self.assertEqual(self.b("plan")["attributes"]["current_addons"], ["biceps"])
        self.assertTrue(self.b("plan")["attributes"]["gym_tracking"])
        self.assertEqual(self.b("stats")["attributes"]["target_days"], 4)
        self.assertEqual(self.b("selected")["state"], "Incline Barbell Bench Press")
        self.assertTrue(self.pushes)

    def test_auto_person_and_notify(self):
        self.assertEqual(self.t.person, "person.alex")      # the one with a phone
        self.assertEqual(self.t.notify, "notify.mobile_app_alex_iphone")

    def test_selection(self):
        run(self.t.select_main("Back"))
        run(self.t.toggle_addon("abs"))
        run(self.t.toggle_addon("biceps"))
        a = self.b("plan")["attributes"]
        self.assertEqual((a["current_main"], a["current_addons"]), ("back", ["abs"]))
        self.assertEqual(self.b("view")["attributes"]["groups"], ["back", "abs"])
        self.assertEqual(self.b("selected")["state"], "Lat Pulldown")   # moved off a chest exercise
        run(self.t.select_exercise("Crunches"))
        self.assertEqual(self.b("selected")["state"], "Crunches")
        run(self.t.suggest())
        self.assertEqual(self.b("plan")["attributes"]["current_main"], "chest")

    def test_log_warmup_pr_and_undo(self):
        ex = "Incline Barbell Bench Press"
        yday = (date.today() - timedelta(days=3)).isoformat()
        from workout_tracker import core as wc
        for _ in range(3):
            wc.log_set(self.t.db, ex, 40, 15, 8, today=yday)
        run(self.t.log_set(ex, 25, 15, 0, warmup=True))
        sel = self.b("selected")["attributes"]
        self.assertEqual([x["kind"] for x in sel["today"]], ["warmup"])
        self.assertEqual(sel["today_done"], 0)
        run(self.t.log_set(ex, 45, 15, 8))
        sel = self.b("selected")["attributes"]
        self.assertEqual(sel["pr"]["records"][0]["type"], "weight")
        self.assertEqual(self.host.events[-1][0], "workout_pr")
        run(self.t.undo(ex))
        self.assertEqual(len(self.b("selected")["attributes"]["today"]), 1)

    def test_notes_body_export(self):
        run(self.t.set_note("Lat Pulldown", "Seat 3"))
        run(self.t.select_main("back"))
        run(self.t.select_exercise("Lat Pulldown"))
        self.assertEqual(self.b("selected")["attributes"]["note"], "Seat 3")
        run(self.t.log_body("height", 180))
        run(self.t.log_body("weight", 81))
        derived = {d["key"]: d for d in self.b("stats")["attributes"]["body"]["derived"]}
        self.assertEqual(derived["bmi"]["value"], 25.0)
        run(self.t.export())
        files = self.b("export")["attributes"]["files"]
        self.assertEqual(len(files), 3)
        self.assertTrue(files[0]["url"].startswith("/workout_tracker/export/workout-"))

    def test_calendar_editing(self):
        day = (date.today() - timedelta(days=2)).isoformat()
        run(self.t.open_day_(day))
        self.assertEqual(self.b("day")["state"], day)
        self.assertTrue(self.b("day")["attributes"]["catalog"])
        run(self.t.add_set(day, "Leg Press"))
        d = self.b("day")["attributes"]
        self.assertEqual(d["sets"], 1)
        sid = d["exercises"][0]["sets"][0]["id"]
        run(self.t.update_set(sid, weight=100, reps=12, rpe=8))
        run(self.t.update_set(sid, warmup=True))
        s = self.b("day")["attributes"]["exercises"][0]["sets"][0]
        self.assertEqual((s["weight"], s["reps"], s["rpe"], s["kind"]), (100, 12, 8, "warmup"))
        run(self.t.delete_set(sid))
        self.assertEqual(self.b("day")["attributes"]["exercises"], [])
        run(self.t.add_set((date.today() + timedelta(days=1)).isoformat(), "Leg Press"))  # future: ignored
        run(self.t.open_day_(""))
        self.assertEqual(self.b("day")["state"], "none")

    def test_gym_visit_and_notifications(self):
        run(self.t.person_changed("home", "Gym"))
        self.assertIsNotNone(self.b("plan")["attributes"]["gym_visit"])
        self.assertEqual(self.host.calls[-1][1], "mobile_app_alex_iphone")
        self.assertIn("Today:", self.host.calls[-1][2]["message"])
        self.assertEqual(self.host.calls[-1][2]["data"]["url"], "/workout")
        # leaving right away counts as driving past: no summary
        run(self.t.person_changed("Gym", "not_home"))
        self.assertEqual(len(self.host.calls), 1)
        self.assertIsNone(self.b("plan")["attributes"]["gym_visit"])

    def test_exercises_add_hide(self):
        run(self.t.add_exercise("Cable Fly", "chest", 2.5))
        names = [c["name"] for c in self.b("view")["attributes"]["cards"]]
        self.assertIn("Cable Fly", names)
        self.assertEqual(self.b("selected")["state"], "Cable Fly")
        run(self.t.hide_exercise("Cable Fly"))
        names = [c["name"] for c in self.b("view")["attributes"]["cards"]]
        self.assertNotIn("Cable Fly", names)

    def test_demos(self):
        sel = self.b("selected")["attributes"]
        self.assertEqual(sel["name"], "Incline Barbell Bench Press")
        self.assertEqual(sel["demo"], "Barbell_Incline_Bench_Press_-_Medium_Grip")
        run(self.t.add_exercise("Cable Fly", "chest", 5, "Cable_Crossover"))
        self.assertEqual(self.b("selected")["attributes"]["demo"], "Cable_Crossover")
        run(self.t.set_demo("Cable Fly", "Flat_Bench_Cable_Flyes"))
        self.assertEqual(self.b("selected")["attributes"]["demo"], "Flat_Bench_Cable_Flyes")
        run(self.t.set_demo("Cable Fly", ""))
        self.assertEqual(self.b("selected")["attributes"]["demo"], "")
        # changing a built-in exercise's demo, and a bad id
        run(self.t.set_demo("Pec Deck", "Machine_Fly"))
        cards = {c["name"]: c for c in self.b("view")["attributes"]["cards"]}
        self.assertEqual(cards["Pec Deck"]["demo"], "Machine_Fly")
        with self.assertRaises(ValueError):
            run(self.t.set_demo("Pec Deck", "../etc/passwd"))
        # re-adding without a demo keeps the one it had
        run(self.t.add_exercise("Cable Fly", "chest", 5))
        run(self.t.set_demo("Cable Fly", "Cable_Crossover"))
        run(self.t.add_exercise("Cable Fly", "chest", 5))
        self.assertEqual(self.b("selected")["attributes"]["demo"], "Cable_Crossover")

    def test_old_config_gets_builtin_demos(self):
        old = os.path.join(self.tmp, "old.yaml")
        with open(old, "w") as fh:
            fh.write("groups: {main: [legs], addon: [abs]}\nexercises:\n"
                     "  Leg Press: {group: legs, step: 5}\n  My Squat: {group: legs}\n")
        t = Tracker(self.host, os.path.join(self.tmp, "o.db"), old, self.tmp, "/x", {})
        run(t.start())
        cards = {c["name"]: c for c in t.bundle["view"]["attributes"]["cards"]}
        self.assertEqual(cards["Leg Press"]["demo"], "Leg_Press")
        self.assertEqual(cards["My Squat"]["demo"], "")

    def test_rest_timer(self):
        rest = self.b("rest")
        self.assertEqual(rest["state"], "idle")
        self.assertEqual(rest["attributes"]["seconds"], 90)
        self.host.calls.clear()
        run(self.t.log_set(weight=40, reps=15))
        rest = self.b("rest")["attributes"]
        self.assertEqual(self.b("rest")["state"], "running")
        self.assertEqual(rest["exercise"], "Incline Barbell Bench Press")
        self.assertAlmostEqual((rest["ends"] - rest["started"]) / 1000, 90, delta=1)
        self.assertEqual(self.host.timers[-1]["seconds"], 90)
        # a new set restarts the timer; only the latest one fires
        run(self.t.log_set(weight=40, reps=15))
        run(self.host.run_timers())
        self.assertEqual(self.b("rest")["state"], "idle")
        self.assertEqual(len(self.host.calls), 1)
        msg = self.host.calls[0][2]
        self.assertEqual(msg["title"], "Rest over")
        self.assertIn("set 3 of 3", msg["message"])
        self.assertEqual(msg["data"]["tag"], "workout-rest")
        self.assertEqual(self.host.events[-1][0], "workout_rest_over")
        # adding time keeps the start, stopping cancels
        run(self.t.rest_start(60))
        started = self.b("rest")["attributes"]["started"]
        run(self.t.rest_add(30))
        r = self.b("rest")["attributes"]
        self.assertEqual((r["started"], r["length"]), (started, 90))
        self.assertAlmostEqual((r["ends"] - started) / 1000, 90, delta=1)
        run(self.t.rest_add(-200))
        self.assertEqual(self.b("rest")["state"], "idle")
        run(self.t.rest_start())
        run(self.t.rest_stop())
        run(self.host.run_timers())
        self.assertEqual(len(self.host.calls), 1)
        # settings: shorter rest, no phone message, 0 = off
        run(self.t.rest_settings(seconds=120, notify=False))
        self.assertEqual(self.b("rest")["attributes"]["seconds"], 120)
        self.assertFalse(self.b("rest")["attributes"]["notify"])
        run(self.t.log_set(weight=40, reps=15))
        run(self.host.run_timers())
        self.assertEqual(len(self.host.calls), 1)
        run(self.t.rest_settings(seconds=0))
        run(self.t.log_set(weight=40, reps=15))
        self.assertEqual(self.b("rest")["state"], "idle")
        # settings survive a restart
        t2 = Tracker(self.host, self.t.db, CFG, self.tmp, "/x", {})
        run(t2.start())
        self.assertEqual(t2.bundle["rest"]["attributes"]["seconds"], 0)

    def test_no_rest_when_logging_a_past_day(self):
        run(self.t.set_day("yesterday"))
        run(self.t.log_set(weight=40, reps=15))
        self.assertEqual(self.b("rest")["state"], "idle")

    def test_midnight(self):
        run(self.t.select_main("legs"))
        run(self.t.midnight())
        self.assertEqual(self.b("plan")["attributes"]["current_main"], "chest")

    def test_no_people_no_tracking(self):
        host = FakeHost()
        t = Tracker(host, os.path.join(self.tmp, "w2.db"), CFG, self.tmp, "/x", {"notify": "auto"})
        run(t.start())
        self.assertEqual((t.person, t.notify), ("", ""))
        self.assertFalse(t.bundle["plan"]["attributes"]["gym_tracking"])


class HubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = FakeHost(
            states={"person.nilay": {"user_id": "u-nilay", "device_trackers": ["device_tracker.nilay_iphone"]},
                    "person.ana": {"user_id": "u-ana", "device_trackers": ["device_tracker.ana_phone"]},
                    "zone.gym": {"friendly_name": "Gym"}},
            services={"notify.mobile_app_nilay_iphone", "notify.mobile_app_ana_phone"})
        self.owner_db = os.path.join(self.tmp, "workout.db")
        from workout_tracker import core as wc
        wc.log_set(self.owner_db, "Leg Press", 100, 15, 8, today="2026-10-01")  # existing history
        self.hub = Hub(self.host, self.tmp, self.owner_db, CFG, os.path.join(self.tmp, "export"),
                       "/workout_tracker/export", {"gym_zone": "zone.gym", "person": "auto", "notify": "auto"},
                       "u-nilay")

    def test_profiles_have_separate_data(self):
        nilay = run(self.hub.get("u-nilay"))
        ana = run(self.hub.get("u-ana"))
        self.assertIs(run(self.hub.get(None)), nilay)          # no user -> owner
        self.assertEqual(nilay.db, self.owner_db)              # owner keeps the existing data
        self.assertTrue(ana.db.endswith(os.path.join("users", "u-ana.db")))
        self.assertEqual(nilay.bundle["stats"]["attributes"]["total_workouts"], 1)
        self.assertEqual(ana.bundle["stats"]["attributes"]["total_workouts"], 0)
        run(ana.log_set("Lat Pulldown", 40, 15, 8))
        self.assertEqual(ana.bundle["stats"]["attributes"]["total_workouts"], 1)
        self.assertEqual(run(self.hub.get("u-nilay")).bundle["stats"]["attributes"]["total_workouts"], 1)
        self.assertNotEqual(nilay.export_url, ana.export_url)

    def test_each_profile_uses_its_own_person_and_phone(self):
        nilay, ana = run(self.hub.get("u-nilay")), run(self.hub.get("u-ana"))
        self.assertEqual((nilay.person, nilay.notify), ("person.nilay", "notify.mobile_app_nilay_iphone"))
        self.assertEqual((ana.person, ana.notify), ("person.ana", "notify.mobile_app_ana_phone"))
        run(self.hub.person_changed("person.ana", "home", "Gym"))
        self.assertIsNotNone(ana.bundle["plan"]["attributes"]["gym_visit"])
        self.assertIsNone(nilay.bundle["plan"]["attributes"]["gym_visit"])
        self.assertEqual(self.host.calls[-1][1], "mobile_app_ana_phone")

    def test_start_known_only_starts_people_with_data(self):
        run(self.hub.start_known(["u-nilay", "u-ana", "u-guest"]))
        self.assertEqual(set(self.hub.trackers), {"u-nilay"})
        run(self.hub.get("u-ana"))                              # Ana opens the page once
        hub2 = Hub(self.host, self.tmp, self.owner_db, CFG, self.tmp, "/x", {}, "u-nilay")
        run(hub2.start_known(["u-nilay", "u-ana", "u-guest"]))
        self.assertEqual(set(hub2.trackers), {"u-nilay", "u-ana"})

    def test_unlinked_user_has_no_gym_tracking(self):
        guest = run(self.hub.get("u-guest"))
        self.assertEqual((guest.person, guest.notify), ("", ""))


if __name__ == "__main__":
    unittest.main()
