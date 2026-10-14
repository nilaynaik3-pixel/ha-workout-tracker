"""Build the exercise library shipped with the integration.

Source: free-exercise-db (https://github.com/yuhonas/free-exercise-db),
public domain (The Unlicense).

    git clone --depth 1 https://github.com/yuhonas/free-exercise-db
    python tools/build_library.py free-exercise-db

Writes custom_components/workout_tracker/www/library.json and
custom_components/workout_tracker/demos.zip: two small WebP pictures per
exercise (start and end position), kept in one file so installs stay quick.
"""

import json
import os
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor

from PIL import Image

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
WWW = os.path.join(ROOT, "custom_components", "workout_tracker", "www")
WIDTH, QUALITY = 360, 50

# library muscle -> tracker group
GROUP = {
    "chest": "chest",
    "shoulders": "shoulders", "traps": "shoulders", "neck": "shoulders",
    "lats": "back", "middle back": "back", "lower back": "back",
    "quadriceps": "legs", "hamstrings": "legs", "glutes": "legs", "calves": "legs",
    "adductors": "legs", "abductors": "legs",
    "biceps": "biceps", "forearms": "biceps",
    "triceps": "triceps",
    "abdominals": "abs",
}
# smallest weight jump by equipment, kg
STEP = {"barbell": 2.5, "e-z curl bar": 2.5, "dumbbell": 2, "kettlebells": 4,
        "machine": 5, "cable": 5}


def convert(job):
    src, dst = job
    if os.path.exists(dst):
        return
    im = Image.open(src).convert("RGB")
    im.thumbnail((WIDTH, WIDTH * 2))
    im.save(dst, "WEBP", quality=QUALITY, method=6)


def main(src_root):
    data = json.load(open(os.path.join(src_root, "dist", "exercises.json"), encoding="utf-8"))
    tmp = tempfile.mkdtemp()
    out, jobs = [], []
    for x in sorted(data, key=lambda e: e["name"].lower()):
        prim = x.get("primaryMuscles") or []
        eq = x.get("equipment") or ""
        frames = len(x.get("images") or [])
        out.append({
            "id": x["id"],
            "n": x["name"],
            "g": GROUP.get(prim[0], "other") if prim else "other",
            "m": prim,
            "s": x.get("secondaryMuscles") or [],
            "e": eq,
            "c": x.get("category") or "",
            "l": x.get("level") or "",
            "st": STEP.get(eq, 2.5),
            "f": frames,
            "i": x.get("instructions") or [],
        })
        if frames:
            for k, rel in enumerate(x["images"][:2]):
                jobs.append((os.path.join(src_root, "exercises", rel),
                             os.path.join(tmp, f"{x['id']}__{k}.webp")))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(convert, jobs))
    with open(os.path.join(WWW, "library.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))
    notice = ("Exercise pictures and instructions: free-exercise-db\n"
              "https://github.com/yuhonas/free-exercise-db\n"
              "Released into the public domain under The Unlicense.\n")
    target = os.path.join(WWW, "..", "demos.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as z:
        z.writestr("LICENSE.md", notice)
        for _, dst in sorted(jobs):
            ex_id, frame = os.path.basename(dst)[:-5].rsplit("__", 1)
            z.write(dst, f"{ex_id}/{frame}.webp")
    print(len(out), "exercises,", len(jobs), "pictures")


if __name__ == "__main__":
    main(sys.argv[1])
