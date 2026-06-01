#!/usr/bin/env python3
"""Daily competitor-share snapshot.

For every keyword in keywords.txt, fetch the top-20 results (page 0) and record
each provider's count and best position. Cheap (~22 requests). Answers:

  "For each keyword, who dominates the first page and where do we sit?"

Outputs (under data/):
  latest_competitor_share.csv   overwritten every run (long format)
  competitor_share_history.csv  appended every run
"""

from __future__ import annotations

import csv
import json
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TODAY = date.today().isoformat()

FIELDS = ["snapshot_date", "keyword", "provider", "count_top_n", "share_pct", "best_rank", "is_brand", "total_results"]


def load_config() -> dict:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    cfg["providers"] = [p for p in cfg.get("providers", []) if p and "REPLACE" not in p]
    if not cfg["providers"]:
        sys.exit("No providers configured in config.json.")
    return cfg


def load_keywords() -> list[str]:
    p = ROOT / "keywords.txt"
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def is_brand(name: str, providers: list[str]) -> bool:
    n = (name or "").casefold()
    return any(p.casefold() in n for p in providers)


def fetch(session, host, key, page=0, size=20):
    url = f"{host}/pc/v1/bildungsangebot"
    params = {"sw": key, "page": page, "size": size}
    for attempt in range(4):
        try:
            r = session.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
            print(f"  retry ({exc})", file=sys.stderr)
    return {}


def main() -> int:
    cfg = load_config()
    settings = cfg["settings"]
    top_n = int(cfg.get("keyword_tracker", {}).get("top_n", 20))
    host = settings["backend_host"].rstrip("/")
    delay = float(settings.get("request_delay_seconds", 0.4))
    providers = cfg["providers"]

    session = requests.Session()
    session.headers.update({
        "X-API-Key": settings["api_key"],
        "User-Agent": "Mozilla/5.0 (ecomex-competitor-share)",
        "Accept": "application/json",
    })

    rows: list[dict] = []
    for kw in load_keywords():
        data = fetch(session, host, kw, page=0, size=min(top_n, 20))
        listings = (data.get("_embedded") or {}).get("bildungsangebotDTOList") or []
        total = data.get("page", {}).get("totalElements", 0)
        counts: Counter = Counter()
        best: dict[str, int] = {}
        for i, c in enumerate(listings[:top_n]):
            name = (c.get("bildungsanbieter") or {}).get("name", "") or "(unknown)"
            counts[name] += 1
            best.setdefault(name, i + 1)
        scanned = max(sum(counts.values()), 1)
        for name, cnt in counts.most_common():
            rows.append({
                "snapshot_date": TODAY, "keyword": kw, "provider": name,
                "count_top_n": cnt,
                "share_pct": round(100 * cnt / scanned, 1),
                "best_rank": best[name],
                "is_brand": 1 if is_brand(name, providers) else 0,
                "total_results": total,
            })
        time.sleep(delay)
        brand_hit = next((r for r in rows[-len(counts):] if r["is_brand"]), None)
        share_str = f"share {brand_hit['share_pct']}% @ #{brand_hit['best_rank']}" if brand_hit else "not in top"
        print(f"[share] {kw}: {len(counts)} providers in top {scanned}, ecomex {share_str}")

    DATA.mkdir(parents=True, exist_ok=True)
    latest = DATA / "latest_competitor_share.csv"
    with latest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    history = DATA / "competitor_share_history.csv"
    new = not history.exists()
    with history.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} provider×keyword rows -> latest_competitor_share.csv (+history)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
