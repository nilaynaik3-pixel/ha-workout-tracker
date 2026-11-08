"""Rolling plan (splits, goals) and cardio."""
import os
import sys
import tempfile
import types
import unittest
from datetime import date, timedelta

HERE = os.path.dirname(__file__)
if "workout_tracker" not in sys.modules:
    pkg = types.ModuleType("workout_tracker")
    pkg.__path__ = [os.path.join(HERE, "..", "custom_components", "workout_tracker")]
    sys.modules["workout_tracker"] = pkg
from workout_tracker import cardio as cd  # noqa: E402
from workout_tracker import core as wc  # noqa: E402

CFG = os.path.join(HERE, "..", "custom_components", "workout_tracker", "exercises.yaml")
TODAY = date(2026, 11, 9)


def ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "w.db")
        self.cfg = wc.load_config(CFG, self.db)
        self.ex = {g: next(n for n, e in self.cfg["exercises"].items() if e["group"] == g) for g in self.cfg["groups"]}

    def tearDown(self):
        self.tmp.cleanup()

    def train(self, days_ago, *groups):
        for g in groups:
            wc.log_set(self.db, self.ex[g], 20, 10, None, ago(days_ago))

    def plan(self, split, goal="own"):
        return wc.plan(self.db, self.cfg, TODAY.isoformat(), {"split": split, "goal": goal})

    def test_rotation_default(self):
        p = wc.plan(self.db, self.cfg, TODAY.isoformat())
        self.assertEqual(p["program"], {"split": "rotation", "goal": "own"})
        self.assertEqual(p["next_groups"], ["chest", "biceps"])
        self.assertEqual(p["queue"], [])

    def test_ppl_rolls_on(self):
        self.assertEqual(self.plan("ppl")["next_day"], "push")
        self.train(3, "chest", "shoulders", "triceps")
        self.assertEqual(self.plan("ppl")["next_day"], "pull")
        self.train(2, "back", "biceps")
        p = self.plan("ppl")
        self.assertEqual((p["next_day"], p["next_groups"]), ("legs", ["legs", "abs"]))
        self.train(1, "legs", "abs")
        p = self.plan("ppl")
        self.assertEqual(p["queue"], ["push", "pull", "legs"])
        self.assertEqual((p["next_main"], p["next_addon"]), ("chest", "triceps"))

    def test_off_plan_and_today(self):
        # back trained (off plan) after push: pull is no longer the stalest
        self.train(3, "legs")
        self.train(2, "chest")
        self.train(1, "back")
        self.assertEqual(self.plan("ppl")["next_day"], "legs")
        self.train(0, "legs")          # training today doesn't move today's plan
        self.assertEqual(self.plan("ppl")["next_day"], "legs")

    def test_bad_program_falls_back(self):
        self.assertEqual(wc.clean_program({"split": "x", "goal": "y"}), {"split": "rotation", "goal": "own"})

    def test_goal_sets_reps(self):
        cfg = wc.apply_goal(wc.load_config(CFG, self.db), {"goal": "strength"})
        self.assertTrue(all((e["sets"], e["reps"]) == (4, 6) for e in cfg["exercises"].values()))
        cfg = wc.apply_goal(wc.load_config(CFG, self.db), {"goal": "own"})
        self.assertTrue(all(e["reps"] == 15 for e in cfg["exercises"].values()))


class CardioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "w.db")
        self.cfg = wc.load_config(CFG, self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_log_and_summaries(self):
        cd.log_cardio(self.db, "treadmill", 30, 5, 2, 300, 140, TODAY.isoformat())
        cd.log_cardio(self.db, "stairs", 10, 3, 6, None, None, TODAY.isoformat())   # no distance on stairs
        cd.log_cardio(self.db, "bike", 20, None, None, None, None, ago(3))
        part = cd.cardio_part(self.db, TODAY.isoformat())
        self.assertEqual(part["week_minutes"], 40)
        self.assertEqual(len(part["today"]), 2)
        self.assertEqual(part["today"][0]["pace"], 6.0)
        self.assertIsNone(part["today"][1]["distance"])
        self.assertEqual(part["weeks"][-2]["minutes"], 20)
        self.assertEqual(part["last"]["treadmill"]["level"], 2)
        st = wc.stats(self.db, self.cfg, TODAY.isoformat())
        self.assertEqual(st["workouts_this_week"], 1)          # a cardio-only day counts
        self.assertEqual(st["calendar"][-1]["cardio"], 40)
        self.assertEqual(len(wc.day_detail(self.db, self.cfg, TODAY.isoformat())["cardio"]), 2)
        cd.delete_cardio(self.db, part["today"][0]["id"])
        self.assertEqual(cd.cardio_part(self.db, TODAY.isoformat())["week_minutes"], 10)

    def test_own_activity(self):
        key = cd.add_activity(self.db, "  Spin class ", True, "level")
        self.assertEqual(key, "c_spin_class")
        cd.log_cardio(self.db, key, 45, 18, 7, day=TODAY.isoformat())
        part = cd.cardio_part(self.db, TODAY.isoformat())
        act = next(a for a in part["activities"] if a["key"] == key)
        self.assertEqual((act["name"], act["distance"], act["level"], act["custom"]), ("Spin class", True, "level", True))
        self.assertEqual(part["names"][key], "Spin class")
        self.assertEqual(part["today"][0]["level"], 7)
        cd.hide_activity(self.db, key)
        cd.hide_activity(self.db, "rope")
        keys = [a["key"] for a in cd.cardio_part(self.db, TODAY.isoformat())["activities"]]
        self.assertNotIn(key, keys)
        self.assertNotIn("rope", keys)
        self.assertEqual(cd.cardio_part(self.db, TODAY.isoformat())["week_minutes"], 45)   # history kept
        cd.add_activity(self.db, "Spin class", False)
        self.assertIn(key, [a["key"] for a in cd.cardio_part(self.db, TODAY.isoformat())["activities"]])
        self.assertIn("Spin class", open(wc.export_csv(self.db, self.cfg, os.path.join(self.tmp.name, "e"))
                                         and [os.path.join(self.tmp.name, "e", f) for f in os.listdir(os.path.join(self.tmp.name, "e")) if "cardio" in f][0]).read())
        with self.assertRaises(ValueError):
            cd.add_activity(self.db, "   ")

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            cd.log_cardio(self.db, "flying", 10)
        with self.assertRaises(ValueError):
            cd.log_cardio(self.db, "run", 0)

    def test_export(self):
        cd.log_cardio(self.db, "run", 25, 5)
        out = wc.export_csv(self.db, self.cfg, os.path.join(self.tmp.name, "exp"))
        self.assertIn("cardio", [f["name"] for f in out["files"]])



class DutchTest(unittest.TestCase):
    def test_texts(self):
        from workout_tracker import meds as md
        from workout_tracker.texts import tr
        self.assertEqual(tr("nl", "Today: {plan}", plan="Borst"), "Vandaag: Borst")
        self.assertEqual(tr("en-GB", "Today: {plan}", plan="Chest"), "Today: Chest")
        dose = {"name": "Vitamine D", "dose": "1 tablet", "note": "bij het eten"}
        normal, lines = md.spoken(dose, False, "nl")
        self.assertEqual(normal, "Tijd voor je Vitamine D, 1 tablet. bij het eten.")
        self.assertIn("Vitamine D", lines["rocky"][0])
        self.assertTrue(md.spoken(dose, True, "en")[0].startswith("Reminder"))


if __name__ == "__main__":
    unittest.main()
