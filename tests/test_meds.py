"""Medication schedules, doses, reminders and the Echo."""
import asyncio
import os
import sys
import tempfile
import types
import unittest
from datetime import date, datetime, timedelta

HERE = os.path.dirname(__file__)
if "workout_tracker" not in sys.modules:
    pkg = types.ModuleType("workout_tracker")
    pkg.__path__ = [os.path.join(HERE, "..", "custom_components", "workout_tracker")]
    sys.modules["workout_tracker"] = pkg
from workout_tracker import meds as md  # noqa: E402
from workout_tracker.tracker import Hub  # noqa: E402

CFG = os.path.join(HERE, "..", "custom_components", "workout_tracker", "exercises.yaml")
MON = date(2026, 10, 19)


def at(day_offset, hm):
    h, m = map(int, hm.split(":"))
    return datetime.combine(MON + timedelta(days=day_offset), datetime.min.time()).replace(hour=h, minute=m)


class ScheduleTest(unittest.TestCase):
    def test_daily_every_n(self):
        s = md.clean_schedule({"repeat": "daily", "every": 2, "times": ["8:00", "20:00", "08:00"],
                               "start": MON.isoformat()})
        self.assertEqual(s["times"], ["08:00", "20:00"])
        self.assertEqual([md.due_on(s, MON + timedelta(days=i)) for i in range(5)], [True, False, True, False, True])
        self.assertFalse(md.due_on(s, MON - timedelta(days=2)))
        self.assertEqual(md.describe(s), "Every 2 days at 08:00 and 20:00")

    def test_weekly_with_end(self):
        s = md.clean_schedule({"repeat": "weekly", "every": 2, "days": [0, 3], "times": ["09:00"],
                               "start": (MON + timedelta(days=2)).isoformat(),
                               "end": (MON + timedelta(days=30)).isoformat()})
        due = [i for i in range(35) if md.due_on(s, MON + timedelta(days=i))]
        # Thu of week 0 (Mon of week 0 is before the start), then Mon+Thu every other week, until day 30
        self.assertEqual(due, [3, 14, 17, 28])
        self.assertEqual(md.describe(s), "Every 2 weeks on Mon, Thu at 09:00, until 18 Nov")
        self.assertEqual(md.describe(md.clean_schedule({"repeat": "weekly", "days": [0, 1, 2, 3, 4], "times": ["07:30"]})),
                         "Every week on weekday at 07:30")

    def test_as_needed_and_errors(self):
        s = md.clean_schedule({"repeat": "none", "times": ["08:00"]})
        self.assertEqual((s["times"], md.due_on(s, MON), md.describe(s)), ([], False, "As needed"))
        for bad in ({"repeat": "daily", "times": []}, {"repeat": "weekly", "times": ["08:00"]},
                    {"repeat": "daily", "times": ["25:00"]}, {"repeat": "monthly", "times": ["08:00"]},
                    {"repeat": "daily", "times": ["08:00"], "start": "2026-10-10", "end": "2026-10-01"}):
            with self.assertRaises(ValueError):
                md.clean_schedule(bad)


class DoseTest(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "w.db")
        self.vit = md.save_med(self.db, "Vitamin D", {"repeat": "daily", "times": ["08:00", "20:00"],
                                                      "start": (MON - timedelta(days=3)).isoformat()},
                               kind="supplement", dose="1 tablet", note="with food")
        self.para = md.save_med(self.db, "Paracetamol", {"repeat": "none"}, dose="500 mg")

    def test_statuses_and_adherence(self):
        now = at(0, "12:00")
        md.log_dose(self.db, self.vit, "taken", f"{MON} 08:00", now=at(0, "08:05"))
        md.log_dose(self.db, self.para, "taken", now=at(0, "10:00"))
        today = md.doses_on(md.list_meds(self.db), MON, md._logs(self.db, "2026-10-01"), now)
        self.assertEqual([(d["name"], d["time"], d["status"]) for d in today],
                         [("Vitamin D", "08:00", "taken"), ("Paracetamol", "10:00", "taken"),
                          ("Vitamin D", "20:00", "upcoming")])
        self.assertTrue(today[1]["as_needed"])
        # yesterday: nothing logged -> missed; skip one of the days before
        md.log_dose(self.db, self.vit, "skipped", f"{MON - timedelta(days=2)} 20:00", now=now)
        o = md.overview(self.db, now)
        vit = next(m for m in o["meds"] if m["id"] == self.vit)
        # 3 past days x 2 doses = 6, minus 1 skipped = 5 missed; 1 taken today
        self.assertEqual(vit["totals"], {"taken": 1, "missed": 5, "skipped": 1})
        self.assertEqual(vit["adherence"], 17)
        self.assertEqual(vit["summary"], "Every day at 08:00 and 20:00")
        self.assertEqual(len(vit["history"]), 14)
        para = next(m for m in o["meds"] if m["id"] == self.para)
        self.assertIsNone(para["adherence"])
        # logging the same dose again replaces it; undo removes it
        lid = md.log_dose(self.db, self.vit, "skipped", f"{MON} 08:00", now=now)
        today = md.doses_on(md.list_meds(self.db), MON, md._logs(self.db, "2026-10-01"), now)
        self.assertEqual(today[0]["status"], "skipped")
        md.undo_log(self.db, lid)
        today = md.doses_on(md.list_meds(self.db), MON, md._logs(self.db, "2026-10-01"), now)
        self.assertEqual(today[0]["status"], "due")

    def test_taken_at_and_editing_logs(self):
        now = at(1, "09:00")
        # yesterday's 20:00 dose was missed; mark it taken at the dose time
        lid = md.log_dose(self.db, self.vit, "taken", f"{MON} 20:00", now=now, at=f"{MON} 20:00")
        logs = md._logs(self.db, "2026-10-01")
        self.assertEqual(logs[-1]["ts"], f"{MON} 20:00")
        # change it: taken at 21:15 (time only = on the dose's day)
        md.update_log(self.db, lid, "taken", "21:15")
        self.assertEqual(md._logs(self.db, "2026-10-01")[-1]["ts"], f"{MON} 21:15")
        md.update_log(self.db, lid, "skipped")
        d = md.doses_on(md.list_meds(self.db), MON, md._logs(self.db, "2026-10-01"), now)
        self.assertEqual([(x["time"], x["status"], x["at"]) for x in d if x["name"] == "Vitamin D"],
                         [("08:00", "missed", None), ("20:00", "skipped", "21:15")])
        # an as-needed dose: change its time
        aid = md.log_dose(self.db, self.para, "taken", now=at(1, "10:00"))
        md.update_log(self.db, aid, at="09:30")
        self.assertEqual(md._logs(self.db, "2026-10-01")[-1]["ts"], f"{MON + timedelta(days=1)} 09:30")
        with self.assertRaises(ValueError):
            md.update_log(self.db, 9999, "taken")
        with self.assertRaises(ValueError):
            md.update_log(self.db, aid, "eaten")
        # the overview has two weeks of days, newest first
        o = md.overview(self.db, now)
        self.assertEqual(len(o["days"]), 14)
        self.assertEqual(o["days"][0]["day"], (MON + timedelta(days=1)).isoformat())
        self.assertEqual(o["days"][1]["day"], MON.isoformat())

    def test_pause_edit_delete(self):
        md.set_paused(self.db, self.vit, True)
        self.assertEqual(md.doses_on(md.list_meds(self.db), MON, [], at(0, "09:00")), [])
        md.set_paused(self.db, self.vit, False)
        md.save_med(self.db, "Vitamin D3", {"repeat": "daily", "times": ["09:00"]}, med_id=self.vit)
        self.assertEqual(md.get_med(self.db, self.vit)["name"], "Vitamin D3")
        md.delete_med(self.db, self.vit)
        self.assertEqual([m["name"] for m in md.list_meds(self.db)], ["Paracetamol"])

    def test_reminder_timing(self):
        due = lambda now: [(d["time"], w) for d, w in md.reminders_due(self.db, now)]  # noqa: E731
        self.assertEqual(due(at(0, "07:59")), [])
        self.assertEqual(due(at(0, "08:00")), [("08:00", 1)])
        md.mark_reminded(self.db, self.vit, f"{MON} 08:00", 1, at(0, "08:00"))
        self.assertEqual(due(at(0, "08:10")), [])
        self.assertEqual(due(at(0, "08:30")), [("08:00", 2)])
        md.mark_reminded(self.db, self.vit, f"{MON} 08:00", 2, at(0, "08:30"))
        self.assertEqual(due(at(0, "09:30")), [])          # only one extra reminder
        # taken before the second reminder: no second reminder
        md.mark_reminded(self.db, self.vit, f"{MON} 20:00", 1, at(0, "20:00"))
        md.log_dose(self.db, self.vit, "taken", f"{MON} 20:00", now=at(0, "20:10"))
        self.assertEqual(due(at(0, "20:30")), [])
        # Home Assistant was off at dose time: no reminder hours later
        self.assertEqual(due(at(1, "12:00")), [])
        self.assertEqual(due(at(1, "09:30")), [("08:00", 1)])


class Host:
    def __init__(self, home=True, say=True):
        self.calls, self.events, self.home, self.say = [], [], home, say

    async def run(self, func, *args):
        return func(*args)

    def entity_ids(self, d):
        return {"person": ["person.me"], "notify": ["notify.echo_pop_announce", "notify.echo_pop_speak"]}.get(d, [])

    def attributes(self, e):
        return {"device_trackers": ["device_tracker.iphone"]} if e == "person.me" else None

    def state(self, e):
        return ("home" if self.home else "not_home") if e == "person.me" else None

    def has_service(self, d, s):
        return (d, s) == ("notify", "mobile_app_iphone") or ((d, s) == ("script", "say") and self.say)

    async def call_service(self, d, s, data):
        self.calls.append((d, s, data))

    def fire(self, ev, data):
        self.events.append(ev)

    def later(self, secs, fn):
        return lambda: None


class TrackerMedsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        self.loop.close()

    def make(self, **kw):
        self.host = Host(**kw)
        hub = Hub(self.host, self.tmp, os.path.join(self.tmp, "w.db"), CFG, self.tmp, "/x",
                  {"person": "auto", "notify": "auto"}, "u1")
        self.hub = hub
        t = self.loop.run_until_complete(hub.get())
        self.loop.run_until_complete(t.med_save("Vitamin D", {"repeat": "daily", "times": ["08:00"],
                                                              "start": (MON - timedelta(days=1)).isoformat()},
                                                "supplement", "1 tablet", "with food", True))
        return t

    def test_phone_and_echo_when_home(self):
        t = self.make()
        self.host.calls.clear()
        self.loop.run_until_complete(t.meds_tick(at(0, "08:00")))
        phone = [c for c in self.host.calls if c[1] == "mobile_app_iphone"]
        say = [c for c in self.host.calls if c[:2] == ("script", "say")]
        self.assertEqual(len(phone), 1)
        self.assertEqual(phone[0][2]["title"], "Time for Vitamin D")
        self.assertEqual(phone[0][2]["message"], "Vitamin D (1 tablet) at 08:00. with food")
        acts = phone[0][2]["data"]["actions"]
        self.assertEqual([a["title"] for a in acts], ["Taken", "Skip"])
        self.assertEqual(acts[0]["action"], f"WT_MED_TAKEN|u1|1|{MON} 08:00")
        self.assertEqual(len(say), 1)
        self.assertEqual(say[0][2]["to"], "echo")
        self.assertEqual(say[0][2]["message"], "Time for your Vitamin D, 1 tablet. with food.")
        self.assertEqual(set(say[0][2]["lines"]), {"rocky", "roast"})
        # the button on the phone logs it and clears the notification
        self.host.calls.clear()
        self.assertTrue(self.loop.run_until_complete(self.hub.med_action(acts[0]["action"])))
        doses = md.doses_on(md.list_meds(t.db), MON, md._logs(t.db, "2026-10-01"), at(0, "08:20"))
        self.assertEqual(doses[0]["status"], "taken")
        self.assertEqual(self.host.calls[0][2]["message"], "clear_notification")
        # nothing more after that
        self.host.calls.clear()
        self.loop.run_until_complete(t.meds_tick(at(0, "08:30")))
        self.assertEqual(self.host.calls, [])

    def test_away_means_phone_only_and_second_reminder(self):
        t = self.make(home=False)
        self.loop.run_until_complete(t.meds_tick(at(0, "08:00")))
        self.loop.run_until_complete(t.meds_tick(at(0, "08:30")))
        titles = [c[2]["title"] for c in self.host.calls if c[1] == "mobile_app_iphone"]
        self.assertEqual(titles, ["Time for Vitamin D", "Still to take: Vitamin D"])
        self.assertFalse([c for c in self.host.calls if c[0] == "script"])

    def test_echo_fallback_and_echo_off(self):
        t = self.make(say=False)
        self.loop.run_until_complete(t.meds_tick(at(0, "08:00")))
        ann = [c for c in self.host.calls if c[:2] == ("notify", "send_message")]
        self.assertEqual(ann[0][2]["entity_id"], "notify.echo_pop_announce")
        self.assertEqual(t.bundle["meds"]["attributes"]["echo"]["via"], "announce")
        # echo switched off for this medicine
        mid = t.bundle["meds"]["attributes"]["meds"][0]["id"]
        self.loop.run_until_complete(t.med_save("Vitamin D", {"repeat": "daily", "times": ["20:00"],
                                                              "start": (MON - timedelta(days=1)).isoformat()},
                                                echo=False, med_id=mid))
        self.host.calls.clear()
        self.loop.run_until_complete(t.meds_tick(at(0, "20:00")))
        self.assertEqual([c[1] for c in self.host.calls], ["mobile_app_iphone"])

    def test_bad_actions_ignored(self):
        self.make()
        self.assertFalse(self.loop.run_until_complete(self.hub.med_action("SOMETHING_ELSE")))
        self.assertFalse(self.loop.run_until_complete(self.hub.med_action("WT_MED_TAKEN|u1")))


if __name__ == "__main__":
    unittest.main()
