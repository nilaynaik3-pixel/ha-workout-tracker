"""Texts the server sends to phones and the Echo, in English or Dutch.

The language is Home Assistant's (Settings > System > General > Language):
"nl" gives Dutch, anything else English.
"""

from __future__ import annotations

NL = {
    "Rest over": "Rust voorbij",
    "Time for your next set.": "Tijd voor je volgende set.",
    "{ex}: {n} sets done. On to the next exercise.": "{ex}: {n} sets gedaan. Door naar de volgende oefening.",
    "{ex}, set {i} of {n}: {load}": "{ex}, set {i} van {n}: {load}",
    "{n} reps": "{n} herhalingen",
    "At the gym": "In de sportschool",
    "Today: {plan}": "Vandaag: {plan}",
    "Workout done": "Training klaar",
    "No sets logged": "Geen sets opgeslagen",
    "{m} min cardio": "{m} min cardio",
    ", new record on {ex}": ", nieuw record op {ex}",
    "{m} min, {n} sets: {groups}{prs}": "{m} min, {n} sets: {groups}{prs}",
    "Still to take: {name}": "Nog innemen: {name}",
    "Time for {name}": "Tijd voor {name}",
    "{what} at {time}": "{what} om {time}",
    "Taken": "Ingenomen",
    "Skip": "Overslaan",
    "Reminder: you haven't taken your {what} yet.{note}": "Herinnering: je hebt je {what} nog niet ingenomen.{note}",
    "Time for your {what}.{note}": "Tijd voor je {what}.{note}",
    "{n} steps to go": "Nog {n} stappen",
    "{s} of {g} so far today. About {m} minutes of walking gets you there.":
        "{s} van {g} tot nu toe vandaag. Zo'n {m} minuten wandelen en je bent er.",
    # muscle groups and plan days, for the gym notification
    "Chest": "Borst", "Shoulders": "Schouders", "Back": "Rug", "Legs": "Benen",
    "Biceps": "Biceps", "Triceps": "Triceps", "Abs": "Buik",
}

LINES_NL = {
    False: {
        "rocky": ["Tijd voor {name}, vraag? Ja! Neem {what}. Lichaam wordt sterker. Geweldig!{note}",
                  "Rocky herinnert vriend: {what}. Goed voor mensenlichaam. Nu nemen.{note}",
                  "{name}-tijd! Rocky kijkt. Rocky trots als vriend het neemt.{note}"],
        "roast": ["Tijd voor je {what}. Je lichaam draait op reservebrandstof, help het even.{note}",
                  "{name}-uur. Zelfs jij krijgt het voor elkaar om één ding op tijd door te slikken.{note}",
                  "Neem je {what}. Ik zou het voor je doen, maar ik ben een slimme speaker, geen tovenaar.{note}"],
    },
    True: {
        "rocky": ["Vraag? {name} nog niet genomen. Rocky wacht. Rocky niet geduldig. Nu nemen!",
                  "Vriend vergeet {name}. Slecht slecht slecht. Neem het, dan Rocky blij.",
                  "Dertig minuten voorbij, vraag? {name} nog in potje. Oplossen, oplossen!"],
        "roast": ["Nog steeds geen {name}? Gedurfde strategie voor iemand die bij elkaar wordt gehouden door snacks en hoop.",
                  "Dertig minuten later en de {name} wacht nog steeds. Het is een pil, geen verbintenis.",
                  "Je {name} belde. Hij voelt zich genegeerd. Eerlijk gezegd, ik ook."],
    },
}


def is_nl(lang):
    return str(lang or "").lower().startswith("nl")


def tr(lang, text, **kw):
    t = NL.get(text, text) if is_nl(lang) else text
    return t.format(**kw) if kw else t


def group(lang, g):
    return tr(lang, str(g).title())
