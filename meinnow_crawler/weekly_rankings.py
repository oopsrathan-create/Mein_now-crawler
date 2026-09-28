#!/usr/bin/env python3
"""Weekly comprehensive per-course ranking report.

For every keyword in keywords.txt, scans the top results and records every
ecomex course appearance with its position. Output is the answer to:

  "For each of our courses, exactly where does it rank, across every keyword?"

Outputs (under data/):
  weekly_rankings/YYYY-WWW.csv     dated weekly snapshot (long format)
  latest_course_rankings.csv       stable copy for the dashboard
"""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import date
from pathlib import Path

import requests

from crawl import RETRY_WAITS, is_retryable

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
WEEKLY = DATA / "weekly_rankings"
iso = date.today().isocalendar()
WEEK_LABEL = f"{iso.year}-W{iso.week:02d}"
TODAY = date.today().isoformat()

FIELDS = ["week", "snapshot_date", "course_id", "title", "keyword", "rank", "total_results"]


def load_config() -> dict:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["providers"] = [p for p in cfg.get("providers", []) if p and "REPLACE" not in p]
    if not cfg["providers"]:
        sys.exit("No providers configured in config.json.")
    return cfg


def load_keywords() -> list[str]:
    p = ROOT / "keywords.txt"
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


class Client:
    def __init__(self, settings: dict):
        self.host = settings["backend_host"].rstrip("/")
        self.size = int(settings.get("page_size", 20))
        self.delay = float(settings.get("request_delay_seconds", 0.4))
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": settings["api_key"],
            "User-Agent": "Mozilla/5.0 (ecomex-weekly-rankings)",
            "Accept": "application/json",
        })

    def page(self, keyword: str, page: int) -> dict:
        url = f"{self.host}/pc/v1/bildungsangebot"
        params = {"sw": keyword, "page": page, "size": self.size}
        for attempt in range(len(RETRY_WAITS) + 1):
            try:
                r = self.session.get(url, params=params, timeout=30)
                r.raise_for_status()
                time.sleep(self.delay)
                return r.json()
            except requests.RequestException as exc:
                if attempt == len(RETRY_WAITS) or not is_retryable(exc):
                    raise
                print(f"  retry in {RETRY_WAITS[attempt]}s ({exc})", file=sys.stderr)
                time.sleep(RETRY_WAITS[attempt])
        return {}


def matches(name: str, providers: list[str]) -> bool:
    n = (name or "").casefold()
    return any(p.casefold() in n for p in providers)


def main() -> int:
    cfg = load_config()
    providers = cfg["providers"]
    settings = cfg["settings"]
    kt = cfg.get("keyword_tracker", {})
    scan_depth = int(kt.get("rank_scan_depth", 500))
    keywords = load_keywords()
    client = Client(settings)

    rows: list[dict] = []
    skipped = []
    for kw in keywords:
        start = len(rows)
        try:
            first = client.page(kw, 0)
            total = first.get("page", {}).get("totalElements", 0)
            total_pages = first.get("page", {}).get("totalPages", 1)
            rank = 0
            hits = 0
            def consume(data):
                nonlocal rank, hits
                for c in (data.get("_embedded") or {}).get("bildungsangebotDTOList") or []:
                    name = (c.get("bildungsanbieter") or {}).get("name", "")
                    if matches(name, providers):
                        rows.append({
                            "week": WEEK_LABEL, "snapshot_date": TODAY,
                            "course_id": c.get("id"), "title": c.get("titel", ""),
                            "keyword": kw, "rank": rank + 1, "total_results": total,
                        })
                        hits += 1
                    rank += 1
            consume(first)
            max_pages = min(total_pages, (scan_depth + client.size - 1) // client.size)
            for p in range(1, max_pages):
                consume(client.page(kw, p))
        except requests.RequestException as exc:
            del rows[start:]   # drop a half-scanned keyword rather than record wrong ranks
            skipped.append(kw)
            print(f"[weekly] {kw}: SKIPPED ({exc})", file=sys.stderr)
            continue
        print(f"[weekly] {kw}: {hits} appearances (of {total} results scanned to depth {rank})")

    if keywords and len(skipped) == len(keywords):
        print("every keyword failed - API unreachable; keeping last week's files", file=sys.stderr)
        return 1
    if skipped:
        print(f"WARNING: {len(skipped)} keyword(s) skipped: {', '.join(skipped)}", file=sys.stderr)

    DATA.mkdir(parents=True, exist_ok=True)
    WEEKLY.mkdir(parents=True, exist_ok=True)
    weekly_path = WEEKLY / f"{WEEK_LABEL}.csv"
    latest_path = DATA / "latest_course_rankings.csv"
    for path in (weekly_path, latest_path):
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
    # Long-running history file: append week's rows for cross-week trends.
    history = DATA / "course_rankings_history.csv"
    new = not history.exists()
    with history.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} appearances -> {weekly_path.name} (+ latest_course_rankings.csv, +history)")
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
