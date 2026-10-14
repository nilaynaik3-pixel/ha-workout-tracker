# Workout Tracker for Home Assistant

Adds a **Workout** page to the sidebar: log sets with progression advice,
warm-ups, notes, a rest timer and live personal-record alerts; exercise demos
and a searchable library of 870+ exercises; a weekly overview with
charts, a calendar to check or fix any day, body measurements with BMI,
time-in-gym tracking and CSV export.

## Install

1. Put this `workout_tracker` folder in `/config/custom_components/`
   (or use the one-line command you were sent, in the Terminal & SSH add-on).
2. Restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Workout Tracker*.
   Pick your weekly target, and optionally your gym zone and person for
   arrival/departure tracking. Change these later with **Configure**.
4. Open **Workout** in the sidebar.

Gym tracking needs a zone for your gym (Settings → Areas, labels & zones →
Zones) and the Home Assistant app on your phone with location set to *Always*.

## Several people

Everyone with a Home Assistant login gets their own tracker: their own
rotation, sets, records, body measurements and gym time. Each person sees
their own data when they open **Workout**; a name picker at the top of the
page switches to someone else's (handy on a shared tablet), and the app
remembers the choice on that device.

Gym tracking and phone alerts work per person automatically, as long as the
person (Settings → People) is linked to their Home Assistant user and has
the Home Assistant app on their phone. The setup's *owner* is whose data
already existed; everyone else starts fresh.

Data is stored in `/config/workout_tracker/` (one file per person) and is included
in Home Assistant backups. To update, replace this folder and restart.
