#!/usr/bin/env python3
"""Per-course performance table (the "Kurstitel-Performance" dashboard tab).

Called by keyword_tracker.py after every daily scan. For every course that ranks
on the first page (top 20) for at least one tracked keyword — plus every ecomex
course — it records the keywords it ranks for, description length, number of
course dates and a visibility score, today and averaged over 30 days.

Visibility score: for each keyword a course ranks for on page 1, add the typical
share of clicks that position gets (rank 1 = 30, rank 2 = 16, ... rank 11-20 = 1),
summed over all tracked keywords. Read it as "expected clicks per 100 searches,
if every tracked keyword were searched equally often". There is no search-volume
weighting: mein-now doesn't publish search counts.

Outputs (under data/):
  latest_course_performance.csv   overwritten every run, one row per course
  course_visibility_history.csv   daily score per course, last HISTORY_DAYS days

Run directly to backfill the history from the latest_competitor_catalog.csv
snapshots already committed to git:  python course_performance.py --backfill
"""

from __future__ import annotations

import csv
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LATEST = DATA / "latest_course_performance.csv"
HISTORY = DATA / "course_visibility_history.csv"

PAGE_ONE = 20        # a keyword "counts" when the course ranks in the top 20
AVG_DAYS = 30
HISTORY_DAYS = 35    # a little slack beyond the 30-day window
# Typical click share per result position (%), a standard organic CTR curve.
_CTR_TOP10 = [30, 16, 11, 8, 7, 5, 4, 3, 3, 2.5]


def ctr(pos: int) -> float:
    if pos <= 10:
        return _CTR_TOP10[pos - 1]
    return 1.0 if pos <= PAGE_ONE else 0.0


def visibility(kw_pos: dict[str, int]) -> float:
    return round(sum(ctr(p) for p in kw_pos.values()), 2)


FIELDS = ["snapshot_date", "provider", "is_brand", "course_id", "title",
          "ranking_keywords", "ranking_keyword_count", "description_words",
          "anzahl_termine", "visibility", "visibility_avg_30d", "link"]
HIST_FIELDS = ["snapshot_date", "course_id", "visibility"]


def _read_history() -> list[dict]:
    if not HISTORY.exists():
        return []
    with HISTORY.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_history(rows: list[dict], today: str) -> None:
    cutoff = (date.fromisoformat(today) - timedelta(days=HISTORY_DAYS)).isoformat()
    rows = sorted((r for r in rows if r["snapshot_date"] > cutoff),
                  key=lambda r: (r["snapshot_date"], r["course_id"]))
    with HISTORY.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HIST_FIELDS)
        w.writeheader()
        w.writerows(rows)


def _avg_30d(history: list[dict], today: str) -> dict[str, float]:
    """Mean daily score over the snapshot days in the last 30 days, counted from
    the course's first appearance (so a newly listed course isn't averaged down
    by days before it existed). A missing day after that counts as 0."""
    start = (date.fromisoformat(today) - timedelta(days=AVG_DAYS - 1)).isoformat()
    window = [r for r in history if start <= r["snapshot_date"] <= today]
    days = sorted({r["snapshot_date"] for r in window})
    total: dict[str, float] = defaultdict(float)
    first: dict[str, str] = {}
    for r in window:
        total[r["course_id"]] += float(r["visibility"])
        first[r["course_id"]] = min(first.get(r["course_id"], r["snapshot_date"]), r["snapshot_date"])
    return {cid: round(s / sum(d >= first[cid] for d in days), 2) for cid, s in total.items()}


def write(catalog: dict, today: str) -> None:
    """catalog: course_id -> aggregated record from keyword_tracker.main()."""
    today_rows, perf = [], []
    for cid, a in catalog.items():
        page_one = {k: p for k, p in a["kw_pos"].items() if p <= PAGE_ONE}
        if not page_one and not a["is_brand"]:
            continue
        score = visibility(page_one)
        if score:
            today_rows.append({"snapshot_date": today, "course_id": str(cid), "visibility": score})
        perf.append((cid, a, page_one, score))

    history = [r for r in _read_history() if r["snapshot_date"] != today] + today_rows
    _write_history(history, today)
    avg = _avg_30d(history, today)

    rows = []
    for cid, a, page_one, score in perf:
        kws = sorted(page_one.items(), key=lambda kv: kv[1])
        rows.append({
            "snapshot_date": today,
            "provider": a["provider"],
            "is_brand": a["is_brand"],
            "course_id": cid,
            "title": a["title"],
            "ranking_keywords": "; ".join(f"{k} (#{p})" for k, p in kws),
            "ranking_keyword_count": len(kws),
            "description_words": a.get("description_words", 0),
            "anzahl_termine": a.get("anzahl_termine", 0),
            "visibility": score,
            "visibility_avg_30d": avg.get(str(cid), 0),
            "link": f"https://mein-now.de/weiterbildungssuche/suche/{cid}",
        })
    rows.sort(key=lambda r: -r["visibility"])
    with LATEST.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} courses -> latest_course_performance.csv "
          f"({len(today_rows)} with page-1 visibility)")


# --- one-off backfill from committed catalog snapshots ------------------------

_KW_RE = re.compile(r"^(.*) \(#(\d+)\)$")


def backfill() -> int:
    """Rebuild course_visibility_history.csv from the git history of
    latest_competitor_catalog.csv. Its 'keywords' column lists each course's 15
    best-ranked keywords, which covers practically every page-1 ranking."""
    rel = "meinnow_crawler/data/latest_competitor_catalog.csv"
    # Scores are only comparable on the same keyword set, so start after the
    # last change to keywords.txt.
    kw_changed = subprocess.run(["git", "log", "-1", "--format=%cI", "--", "meinnow_crawler/keywords.txt"],
                                cwd=ROOT.parent, capture_output=True, text=True, check=True).stdout.strip()
    since = max(kw_changed, (date.today() - timedelta(days=HISTORY_DAYS + 1)).isoformat())
    log = subprocess.run(["git", "log", "--format=%H", f"--since={since}", "--", rel],
                         cwd=ROOT.parent, capture_output=True, text=True, check=True).stdout.split()
    by_day: dict[str, list[dict]] = {}
    for sha in log:
        blob = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=ROOT.parent, capture_output=True, check=True).stdout
        rows = list(csv.DictReader(blob.decode("utf-8").splitlines()))
        if not rows:
            continue
        day = rows[0]["snapshot_date"]
        if day in by_day:        # several commits on one day: keep the newest (log is newest-first)
            continue
        out = []
        for r in rows:
            kw_pos = {}
            for part in (r.get("keywords") or "").split("; "):
                m = _KW_RE.match(part)
                if m and int(m.group(2)) <= PAGE_ONE:
                    kw_pos[m.group(1)] = int(m.group(2))
            score = visibility(kw_pos)
            if score:
                out.append({"snapshot_date": day, "course_id": r["course_id"], "visibility": score})
        by_day[day] = out
        print(f"[backfill] {day}: {len(out)} courses with page-1 visibility")
    existing = [r for r in _read_history() if r["snapshot_date"] not in by_day]
    history = existing + [r for rows in by_day.values() for r in rows]
    _write_history(history, max(by_day) if by_day else date.today().isoformat())
    print(f"backfilled {len(by_day)} days -> course_visibility_history.csv")
    _bootstrap_latest()
    return 0


def _bootstrap_latest() -> None:
    """First latest_course_performance.csv, from the current catalog, so the
    dashboard has data before the next daily run. Description length and course
    dates come from the ecomex inventory; competitors get them from that run on."""
    with (DATA / "latest_competitor_catalog.csv").open(encoding="utf-8", newline="") as f:
        cat = list(csv.DictReader(f))
    with (DATA / "latest_inventory.csv").open(encoding="utf-8", newline="") as f:
        inv = {r["course_id"]: r for r in csv.DictReader(f)}
    catalog = {}
    for r in cat:
        kw_pos = {}
        for part in (r.get("keywords") or "").split("; "):
            m = _KW_RE.match(part)
            if m:
                kw_pos[m.group(1)] = int(m.group(2))
        i = inv.get(r["course_id"], {})
        catalog[r["course_id"]] = {
            "provider": r["provider"], "is_brand": int(r["is_brand"]), "title": r["title"],
            "kw_pos": kw_pos,
            "description_words": len(i["description"].split()) if i else "",
            "anzahl_termine": i.get("anzahl_termine", ""),
        }
    write(catalog, cat[0]["snapshot_date"])


if __name__ == "__main__":
    if "--backfill" in sys.argv:
        raise SystemExit(backfill())
    print(__doc__)
