#!/usr/bin/env python3
"""Daily data health check, run at the end of the daily crawl workflow.

A crawl can "succeed" and still produce bad data (e.g. mein-NOW changes its
search and everything returns nothing). This checks today's output against
what a normal day looks like and writes the verdict to data/health.json,
which the dashboard shows as a status line.

  python health_check.py            write data/health.json (always exits 0)
  python health_check.py --enforce  exit 1 if health.json isn't "ok", printing
                                    the failed checks (so the alert issue shows them)
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HEALTH = DATA / "health.json"
TODAY = date.today().isoformat()

MIN_KEYWORD_SHARE = 0.95   # at least 95% of tracked keywords scanned today
MIN_COURSE_SHARE = 0.80    # ecomex course count may not drop by more than 20%
MIN_RANKED_SHARE = 0.70    # keywords where ecomex ranks may not drop by more than 30%


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _keywords() -> list[str]:
    lines = (ROOT / "keywords.txt").read_text(encoding="utf-8").splitlines()
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]


def run_checks() -> dict:
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    n_kw = len(_keywords())

    # 1) Course inventory: today's snapshot exists and didn't collapse.
    snaps = sorted((DATA / "snapshots").glob("inventory_*.csv"))
    today_snap = DATA / "snapshots" / f"inventory_{TODAY}.csv"
    courses = len(_rows(today_snap))
    prev = [s for s in snaps if s.name < today_snap.name]
    prev_courses = len(_rows(prev[-1])) if prev else 0
    if not today_snap.exists():
        check("Kursliste", False, "keine Kursliste von heute")
    elif prev_courses and courses < MIN_COURSE_SHARE * prev_courses:
        check("Kursliste", False, f"{courses} Kurse, vorher {prev_courses} (Einbruch > 20 %)")
    else:
        check("Kursliste", courses > 0, f"{courses} Kurse" + (f" (vorher {prev_courses})" if prev_courses else ""))

    # 2) + 3) Keyword scans (normal and Bildungsgutschein) cover the keyword list.
    ranks = _rows(DATA / "keyword_ranks.csv")
    today_ranks = [r for r in ranks if r["snapshot_date"] == TODAY]
    scanned = len({r["keyword"] for r in today_ranks})
    check("Keyword-Scan", scanned >= MIN_KEYWORD_SHARE * n_kw, f"{scanned}/{n_kw} Keywords")

    bg = _rows(DATA / "keyword_ranks_bg.csv")
    bg_scanned = len({r["keyword"] for r in bg if r["snapshot_date"] == TODAY})
    check("Bildungsgutschein-Scan", bg_scanned >= MIN_KEYWORD_SHARE * n_kw, f"{bg_scanned}/{n_kw} Keywords")

    # 4) ecomex still ranks for a similar number of keywords as on the previous crawl day.
    ranked_by_day: Counter = Counter()
    for r in ranks:
        if r["brand_best_rank"]:
            ranked_by_day[r["snapshot_date"]] += 1
    ranked = ranked_by_day.get(TODAY, 0)
    prev_days = sorted(d for d in ranked_by_day if d < TODAY)
    prev_ranked = ranked_by_day[prev_days[-1]] if prev_days else 0
    if prev_ranked and ranked < MIN_RANKED_SHARE * prev_ranked:
        check("ecomex-Sichtbarkeit", False, f"rankt für {ranked} Keywords, vorher {prev_ranked} (Einbruch > 30 %)")
    else:
        check("ecomex-Sichtbarkeit", ranked > 0, f"rankt für {ranked} Keywords" + (f" (vorher {prev_ranked})" if prev_ranked else ""))

    # 5) Competitor share covers the keyword list.
    share = _rows(DATA / "competitor_share_history.csv")
    share_kw = len({r["keyword"] for r in share if r["snapshot_date"] == TODAY})
    check("Wettbewerber-Anteil", share_kw >= MIN_KEYWORD_SHARE * n_kw, f"{share_kw}/{n_kw} Keywords")

    failed = [c for c in checks if not c["ok"]]
    return {
        "date": TODAY,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "status": "ok" if not failed else "error",
        "keywords": n_kw,
        "keywords_scanned": scanned,
        "bg_keywords_scanned": bg_scanned,
        "courses": courses,
        "checks": checks,
    }


def main() -> int:
    if "--enforce" in sys.argv:
        h = json.loads(HEALTH.read_text(encoding="utf-8")) if HEALTH.exists() else {}
        if h.get("date") != TODAY:
            print(f"::error::health check missing for {TODAY} (last: {h.get('date')})")
            return 1
        failed = [c for c in h["checks"] if not c["ok"]]
        for c in failed:
            print(f"::error::{c['name']}: {c['detail']}")
        return 1 if failed else 0

    h = run_checks()
    DATA.mkdir(parents=True, exist_ok=True)
    HEALTH.write_text(json.dumps(h, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for c in h["checks"]:
        print(f"[health] {'OK  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}")
    print(f"[health] status: {h['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
