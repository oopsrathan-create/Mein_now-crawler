#!/usr/bin/env python3
"""Full competitor catalogues via provider-name search.

The mein-now API has no provider filter, so — exactly like crawl.py builds our
own inventory — we search each competitor's provider name and keep the results
whose provider matches. This yields their (near-)complete mein-now catalogue and
the accurate "X Weiterbildungsangebote" total, not just the courses that surface
for our marketing keywords.

Input  : data/latest_competitor_catalog.csv  (footprint written by keyword_tracker.py:
         provider list + per-course keyword rankings)
Outputs: data/latest_competitor_catalog.csv   (rewritten, enriched — every collected
                                               course, with rankings merged where known)
         data/latest_competitor_providers.csv  (per provider: accurate totals + counts)

Bounded by config["provider_catalog"] so the daily crawl can't run away:
  max_providers          (default 150) — providers to crawl, most courses first
  max_pages_per_provider (default 15)  — pages of their catalogue to page through
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from crawl import Client, flatten, load_config, provider_matches

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TODAY = date.today().isoformat()

FOOTPRINT = DATA / "latest_competitor_catalog.csv"
CAT_FIELDS = ["snapshot_date", "provider", "is_brand", "major", "course_id", "title",
              "weiterbildungsart", "keyword_count", "best_rank", "best_page",
              "keywords", "description", "in_scope"]
PROV_FIELDS = ["snapshot_date", "provider", "is_brand", "major",
               "catalog_courses", "search_total", "in_scope_courses", "best_rank"]


def read_footprint():
    """Return (providers ordered by footprint size, {course_id: ranking row})."""
    if not FOOTPRINT.exists():
        sys.exit("No footprint catalogue yet — run keyword_tracker.py first.")
    counts = defaultdict(int)
    meta = {}            # provider -> {"major":bool, "is_brand":bool}
    rankings = {}        # course_id -> {keywords, keyword_count, best_rank, best_page}
    with FOOTPRINT.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            p = r["provider"]
            counts[p] += 1
            meta.setdefault(p, {"major": r.get("major") == "1", "is_brand": r.get("is_brand") == "1"})
            rankings[r["course_id"]] = {
                "keywords": r.get("keywords", ""),
                "keyword_count": r.get("keyword_count", "0"),
                "best_rank": r.get("best_rank", ""),
                "best_page": r.get("best_page", ""),
            }
    providers = sorted(counts, key=lambda p: counts[p], reverse=True)
    return providers, meta, rankings


def main() -> int:
    cfg = load_config()
    pc = cfg.get("provider_catalog", {})
    max_providers = int(pc.get("max_providers", 150))
    max_pages = int(pc.get("max_pages_per_provider", 15))
    brand = cfg["providers"]

    providers, meta, rankings = read_footprint()
    providers = providers[:max_providers]
    client = Client(cfg["settings"])

    cat_rows, prov_rows = [], []
    for pi, prov in enumerate(providers, 1):
        is_major = meta.get(prov, {}).get("major", False)
        is_brand = bool(provider_matches(prov, brand))
        seen, search_total = {}, 0
        for rank, listing, total in client.iter_listings(prov, max_pages):
            search_total = total or search_total
            name = (listing.get("bildungsanbieter") or {}).get("name")
            if not provider_matches(name, [prov]):
                continue
            cid = listing.get("id")
            if cid in seen:
                continue
            seen[cid] = flatten(listing)
        # build rows, merging keyword rankings where the course is in our scope
        best = None
        in_scope = 0
        for cid, c in seen.items():
            rk = rankings.get(str(cid)) or rankings.get(cid)
            scope = 1 if rk else 0
            in_scope += scope
            br = rk["best_rank"] if rk else ""
            try:
                if br != "" and (best is None or int(br) < best):
                    best = int(br)
            except ValueError:
                pass
            cat_rows.append({
                "snapshot_date": TODAY, "provider": prov,
                "is_brand": 1 if is_brand else 0, "major": 1 if is_major else 0,
                "course_id": cid, "title": c.get("title", ""),
                "weiterbildungsart": c.get("weiterbildungsart", ""),
                "keyword_count": rk["keyword_count"] if rk else 0,
                "best_rank": br, "best_page": rk["best_page"] if rk else "",
                "keywords": rk["keywords"] if rk else "",
                "description": c.get("description", "") if is_major else "",
                "in_scope": scope,
            })
        prov_rows.append({
            "snapshot_date": TODAY, "provider": prov,
            "is_brand": 1 if is_brand else 0, "major": 1 if is_major else 0,
            "catalog_courses": len(seen), "search_total": search_total,
            "in_scope_courses": in_scope, "best_rank": best if best is not None else "",
        })
        print(f"[{pi}/{len(providers)}] {prov}: {len(seen)} Kurse (Suchtreffer {search_total}, im Bereich {in_scope})")

    DATA.mkdir(parents=True, exist_ok=True)
    cat_rows.sort(key=lambda r: (r["provider"].casefold(), r["best_rank"] == "", r["title"]))
    with (DATA / "latest_competitor_catalog.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAT_FIELDS); w.writeheader(); w.writerows(cat_rows)
    prov_rows.sort(key=lambda r: r["catalog_courses"], reverse=True)
    with (DATA / "latest_competitor_providers.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PROV_FIELDS); w.writeheader(); w.writerows(prov_rows)
    print(f"wrote {len(cat_rows)} courses across {len(prov_rows)} providers "
          f"-> latest_competitor_catalog.csv (+ latest_competitor_providers.csv)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
