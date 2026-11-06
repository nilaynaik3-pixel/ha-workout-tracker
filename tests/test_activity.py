"""Steps and calories tests."""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
if "workout_tracker" not in sys.modules:
    pkg = types.ModuleType("workout_tracker")
    pkg.__path__ = [os.path.join(HERE, "..", "custom_components", "workout_tracker")]
    sys.modules["workout_tracker"] = pkg
from workout_tracker import activity as act  # noqa: E402
from workout_tracker import core as wc  # noqa: E402
from workout_tracker.tracker import Tracker  # noqa: E402

CFG = os.path.join(HERE, "..", "custom_components", "workout_tracker", "exercises.yaml")


class Host:
    """Stand-in for Home Assistant with states and their update times."""

    def __init__(self):
        self.attrs = {"person.alex": {"friendly_name": "Alex", "device_trackers": ["device_tracker.alex_phone"]},
                      "zone.gym": {"friendly_name": "Gym"}}
        self.values, self.updated = {}, {}
        self.services = {"notify.mobile_app_alex_phone"}
        self.calls, self.events = [], []
        self.devices = {"device_tracker.alex_phone": ["device_tracker.alex_phone", "sensor.alex_phone_steps",
                                                     "sensor.alex_phone_health_steps",
                                                     "sensor.alex_phone_active_energy",
                                                     "sensor.alex_phone_battery_level"]}

    def put(self, entity_id, value, when=None):
        self.values[entity_id] = str(value)
        self.updated[entity_id] = when or datetime.now()

    async def run(self, func, *args):
        return func(*args)

    def entity_ids(self, domain):
        return [e for e in list(self.attrs) + list(self.values) if e.startswith(domain + ".")]

    def attributes(self, entity_id):
        return self.attrs.get(entity_id)

    def state(self, entity_id):
        return self.values.get(entity_id)

    def last_updated(self, entity_id):
        return self.updated.get(entity_id)

    def device_entities(self, entity_id):
        return self.devices.get(entity_id, [])

    def has_service(self, d, s):
        return f"{d}.{s}" in self.services

    async def call_service(self, d, s, data):
        self.calls.append((d, s, data))

    def fire(self, ev, data):
        self.events.append((ev, data))

    def later(self, seconds, fn):
        return lambda: None


def run(coro):
    return asyncio.run(coro)


class EstimateTest(unittest.TestCase):
    def test_estimate_is_active_only(self):
        e = act.estimate(94, 8000, 60)
        self.assertEqual(e, {"total": 376 + 329, "walking": 376, "gym": 329})

    def test_estimate_needs_weight(self):
        self.assertEqual(act.estimate(None, 1000, 30), {"total": None, "walking": None, "gym": None})

    def test_settings_cleaned(self):
        s = act.clean_settings({"goal": "7500", "age": "27", "sex": "Male", "nudge": "5:00", "steps": ""})
        self.assertEqual((s["goal"], s["age"], s["sex"], s["steps"]), (7500, 27, "male", "auto"))
        self.assertEqual(s["nudge"], "05:00")
        self.assertEqual(act.clean_settings({"nudge": ""})["nudge"], "")
        self.assertEqual(act.clean_settings({"steps": "sensor.a, sensor.b"})["steps"], ["sensor.a", "sensor.b"])
        self.assertIsNone(act.clean_settings({"age": 3})["age"])


class GymMinutesTest(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "w.db")
        self.day = datetime.now().date().isoformat()

    def test_from_sets_when_no_visit(self):
        t0 = datetime.combine(datetime.now().date(), datetime.min.time()) + timedelta(hours=10)
        for i in range(6):
            wc.log_set(self.db, "Bench press", 60, 10, now=t0 + timedelta(minutes=5 * i))
        self.assertEqual(act.gym_minutes_on(self.db, self.day, t0 + timedelta(hours=2)), (30, "sets"))

    def test_single_set(self):
        wc.log_set(self.db, "Bench press", 60, 10)
        self.assertEqual(act.gym_minutes_on(self.db, self.day), (2, "sets"))

    def test_visit_counts_live(self):
        start = datetime.now() - timedelta(minutes=40)
        wc.start_visit(self.db, start)
        minutes, basis = act.gym_minutes_on(self.db, self.day, start + timedelta(minutes=40))
        self.assertEqual((minutes, basis), (40, "visit"))


class TrackerActivityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = Host()
        self.t = Tracker(self.host, os.path.join(self.tmp, "w.db"), CFG, os.path.join(self.tmp, "exp"),
                         "/x", {"gym_zone": "zone.gym", "person": "auto", "notify": "auto"})
        run(self.t.start())

    def a(self):
        return self.t.bundle["activity"]["attributes"]

    def test_sources_found_on_the_phone(self):
        src = self.t.activity_sources()
        self.assertEqual(src["steps"], ["sensor.alex_phone_health_steps", "sensor.alex_phone_steps"])
        self.assertEqual(src["energy"], ["sensor.alex_phone_active_energy"])

    def test_higher_step_count_wins_and_yesterday_ignored(self):
        self.host.put("sensor.alex_phone_steps", 4200)
        self.host.put("sensor.alex_phone_health_steps", 9000, datetime.now() - timedelta(days=1))
        run(self.t.activity_tick())
        self.assertEqual(self.a()["steps"], 4200)
        self.host.put("sensor.alex_phone_health_steps", 5100)
        run(self.t.activity_tick())
        self.assertEqual(self.a()["steps"], 5100)

    def test_calories_need_only_weight(self):
        self.host.put("sensor.alex_phone_steps", 2000)
        run(self.t.activity_tick())
        self.assertEqual(self.a()["missing"], ["weight"])
        self.assertIsNone(self.a()["kcal"]["total"])
        run(self.t.log_body("weight", 94))
        a = self.a()
        self.assertEqual(a["missing"], [])
        self.assertEqual(a["kcal"], {"total": 94, "walking": 94, "gym": 0})
        self.assertEqual(a["history"]["days"][-1]["kcal"], 94)

    def test_nudge_once_when_below_goal(self):
        self.host.put("sensor.alex_phone_steps", 3000)
        five = datetime.now().replace(hour=17, minute=0, second=0, microsecond=0)
        self.host.updated["sensor.alex_phone_steps"] = five
        run(self.t.activity_tick(five))
        run(self.t.activity_tick(five))
        notes = [c for c in self.host.calls if c[0] == "notify"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][2]["title"], "5,000 steps to go")
        self.assertIn("50 minutes", notes[0][2]["message"])

    def test_no_nudge_when_goal_met_or_off(self):
        five = datetime.now().replace(hour=17, minute=0, second=0, microsecond=0)
        self.host.put("sensor.alex_phone_steps", 9000, five)
        run(self.t.activity_tick(five))
        self.host.put("sensor.alex_phone_steps", 100, five)
        run(self.t.activity_settings(nudge=""))
        self.t._nudged_day = None
        run(self.t.activity_tick(five))
        self.assertFalse([c for c in self.host.calls if c[0] == "notify"])


if __name__ == "__main__":
    unittest.main()
