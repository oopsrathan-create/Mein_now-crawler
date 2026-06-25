#!/usr/bin/env python3
"""Crawler for tracking a brand's course listings on mein-now.de.

Pulls data from the mein-NOW / KURSNET search backend (the same API the public
search page uses). Two passes per run:

  1. Inventory  - for each tracked provider, collect every one of their courses.
  2. Ranking    - for each tracked keyword, record where our courses appear in
                  the result order (visibility).

Outputs (all under data/):
  snapshots/inventory_YYYY-MM-DD.csv    one row per course, the daily snapshot
  snapshots/inventory_YYYY-MM-DD.jsonl  same data, full detail, one JSON per line
  rankings.csv                          appended every run, long-format rank history
  changes/changes_YYYY-MM-DD.md         human-readable diff vs the previous snapshot
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SNAPSHOTS = DATA / "snapshots"
CHANGES = DATA / "changes"
TODAY = date.today().isoformat()

_TAG_RE = re.compile(r"<[^>]+>")


def load_config() -> dict:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    providers = [p for p in cfg.get("providers", []) if p and "REPLACE" not in p]
    if not providers:
        sys.exit(
            "No providers configured. Edit config.json and set 'providers' to your "
            "brand's name(s) as they appear on mein-now.de."
        )
    cfg["providers"] = providers
    return cfg


class Client:
    def __init__(self, settings: dict):
        self.host = settings["backend_host"].rstrip("/")
        self.size = int(settings.get("page_size", 100))
        self.delay = float(settings.get("request_delay_seconds", 0.4))
        self.extra = settings.get("search_params") or {}   # mirror the website's default filters
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-API-Key": settings["api_key"],
                "User-Agent": "Mozilla/5.0 (brand-course-tracker)",
                "Accept": "application/json",
            }
        )

    def page(self, keyword: str, page: int) -> dict:
        url = f"{self.host}/pc/v1/bildungsangebot"
        params = {"sw": keyword, "page": page, "size": self.size, **self.extra}
        for attempt in range(4):
            try:
                r = self.session.get(url, params=params, timeout=30)
                r.raise_for_status()
                time.sleep(self.delay)
                return r.json()
            except requests.RequestException as exc:
                if attempt == 3:
                    raise
                wait = 2 ** attempt
                print(f"  request failed ({exc}); retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
        return {}

    def iter_listings(self, keyword: str, max_pages: int):
        """Yield (global_rank, listing) for a keyword search, in result order."""
        first = self.page(keyword, 0)
        total_pages = min(first.get("page", {}).get("totalPages", 1), max_pages)
        total_elements = first.get("page", {}).get("totalElements", 0)
        rank = 0
        for listing in _listings(first):
            yield rank, listing, total_elements
            rank += 1
        for p in range(1, total_pages):
            data = self.page(keyword, p)
            for listing in _listings(data):
                yield rank, listing, total_elements
                rank += 1


def _listings(data: dict) -> list:
    return (data.get("_embedded") or {}).get("bildungsangebotDTOList") or []


def provider_matches(name: str, providers: list[str]) -> bool:
    n = (name or "").casefold()
    return any(p.casefold() in n for p in providers)


def ms_to_date(ms) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def flatten(listing: dict) -> dict:
    anbieter = listing.get("bildungsanbieter") or {}
    adresse = anbieter.get("adresse") or {}
    termine = listing.get("termine") or []

    locations = sorted({(t.get("adresse") or {}).get("ort", "") for t in termine if t.get("adresse")})
    cost_bands = sorted({t.get("kostenWert", "") for t in termine if t.get("kostenWert")})
    formats = sorted({(t.get("unterrichtsform") or {}).get("bezeichnung", "") for t in termine if t.get("unterrichtsform")})
    schedules = sorted({(t.get("unterrichtszeit") or {}).get("bezeichnung", "") for t in termine if t.get("unterrichtszeit")})
    starts = sorted({ms_to_date(t.get("beginn")) for t in termine if t.get("beginn")})
    starts = [s for s in starts if s]

    description = _TAG_RE.sub(" ", listing.get("inhalt") or "")
    description = re.sub(r"\s+", " ", description).strip()

    return {
        "course_id": listing.get("id"),
        "title": listing.get("titel", ""),
        "provider": anbieter.get("name", ""),
        "provider_ort": adresse.get("ort", ""),
        "provider_plz": adresse.get("plz", ""),
        "weiterbildungsart": listing.get("weiterbildungsart", ""),
        "anzahl_termine": listing.get("anzahlTermine", 0),
        "locations": "; ".join(locations),
        "cost_bands": "; ".join(cost_bands),
        "formats": "; ".join(formats),
        "schedules": "; ".join(schedules),
        "next_start": starts[0] if starts else "",
        "start_dates": "; ".join(starts[:10]),
        "description": description,
    }


INVENTORY_FIELDS = [
    "snapshot_date", "course_id", "title", "provider", "provider_ort", "provider_plz",
    "weiterbildungsart", "anzahl_termine", "locations", "cost_bands", "formats",
    "schedules", "next_start", "start_dates", "description",
]
RANKING_FIELDS = ["snapshot_date", "keyword", "provider", "course_id", "title", "rank", "total_results"]


def inventory_search_terms(cfg: dict) -> list[str]:
    """The mein-NOW API has no provider filter, and searching the provider name
    only returns courses that literally mention it. So we enumerate a provider's
    catalogue by aggregating provider-name matches across a broad set of search
    terms (provider names + tracked keywords + keywords.txt) and deduping by id."""
    terms: list[str] = list(cfg["providers"]) + list(cfg.get("ranking_keywords", []))
    kw_file = ROOT / "keywords.txt"
    if kw_file.exists():
        for line in kw_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    seen, ordered = set(), []
    for t in terms:
        if t.casefold() not in seen:
            seen.add(t.casefold())
            ordered.append(t)
    return ordered


def run_inventory(client: Client, cfg: dict) -> dict[int, dict]:
    """Return {course_id: flattened} for all of our providers' courses."""
    found: dict[int, dict] = {}
    max_pages = int(cfg["settings"].get("max_pages_per_query", 50))
    providers = cfg["providers"]
    for term in inventory_search_terms(cfg):
        before = len(found)
        for _rank, listing, _total in client.iter_listings(term, max_pages):
            if provider_matches((listing.get("bildungsanbieter") or {}).get("name"), providers):
                cid = listing.get("id")
                if cid not in found:
                    found[cid] = flatten(listing)
        print(f"[inventory] '{term}': +{len(found) - before} new (total {len(found)})")
    return found


def run_ranking(client: Client, cfg: dict) -> list[dict]:
    rows: list[dict] = []
    max_pages = int(cfg["settings"].get("ranking_max_pages", 25))
    for keyword in cfg.get("ranking_keywords", []):
        print(f"[ranking] keyword: {keyword}")
        hits = 0
        for rank, listing, total in client.iter_listings(keyword, max_pages):
            name = (listing.get("bildungsanbieter") or {}).get("name")
            if provider_matches(name, cfg["providers"]):
                rows.append({
                    "snapshot_date": TODAY,
                    "keyword": keyword,
                    "provider": name,
                    "course_id": listing.get("id"),
                    "title": listing.get("titel", ""),
                    "rank": rank + 1,  # 1-based position in results
                    "total_results": total,
                })
                hits += 1
        print(f"  -> {hits} of our courses found in top {max_pages * client.size} results")
    return rows


def write_snapshot(inventory: dict[int, dict]) -> Path:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    csv_path = SNAPSHOTS / f"inventory_{TODAY}.csv"
    jsonl_path = SNAPSHOTS / f"inventory_{TODAY}.jsonl"
    rows = sorted(inventory.values(), key=lambda r: (r["provider"], r["title"]))
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INVENTORY_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({"snapshot_date": TODAY, **r})
    with jsonl_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({"snapshot_date": TODAY, **r}, ensure_ascii=False) + "\n")
    # Stable-named copy so the dashboard can fetch it without knowing the date.
    latest = DATA / "latest_inventory.csv"
    with latest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=INVENTORY_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({"snapshot_date": TODAY, **r})
    print(f"wrote {len(rows)} courses -> {csv_path.name} (+ latest_inventory.csv)")
    return csv_path


def append_rankings(rows: list[dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "rankings.csv"
    new_file = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RANKING_FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"appended {len(rows)} ranking rows -> rankings.csv")


def previous_snapshot() -> dict[int, dict] | None:
    if not SNAPSHOTS.exists():
        return None
    files = sorted(p for p in SNAPSHOTS.glob("inventory_*.csv") if TODAY not in p.name)
    if not files:
        return None
    prev = {}
    with files[-1].open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            prev[int(row["course_id"])] = row
    return prev


def write_changes(inventory: dict[int, dict]) -> None:
    prev = previous_snapshot()
    if prev is None:
        print("no previous snapshot; skipping diff")
        return
    CHANGES.mkdir(parents=True, exist_ok=True)
    cur_ids = set(inventory)
    prev_ids = set(prev)
    added = cur_ids - prev_ids
    removed = prev_ids - cur_ids

    lines = [f"# Changes for {TODAY}", ""]
    lines.append(f"- Courses now listed: **{len(cur_ids)}** (was {len(prev_ids)})")
    lines.append(f"- New: **{len(added)}**, Dropped: **{len(removed)}**")
    lines.append("")
    if added:
        lines.append("## New listings")
        for cid in sorted(added):
            c = inventory[cid]
            lines.append(f"- {c['title']} — {c['provider']} (id {cid})")
        lines.append("")
    if removed:
        lines.append("## Dropped listings")
        for cid in sorted(removed):
            c = prev[cid]
            lines.append(f"- {c['title']} — {c['provider']} (id {cid})")
        lines.append("")
    (CHANGES / f"changes_{TODAY}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote diff: +{len(added)} / -{len(removed)}")


COVERAGE_FIELDS = [
    "snapshot_date", "type", "tracked_title", "status", "match_title",
    "course_id", "locations", "next_start", "cost_bands", "weiterbildungsart",
]


def _normalise(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").casefold())


def run_coverage(client: Client, cfg: dict, inventory: dict[int, dict]) -> list[dict]:
    """For each tracked course title, find a matching ecomex listing on mein-now.

    Strategy:
      1. Try to match against the existing inventory (case- and punctuation-
         insensitive substring either way).
      2. If no match, run a targeted sw=<title> search and filter to ecomex.
      3. Record found/missing with the best matching listing's metadata.
    """
    tracked_path = ROOT / "tracked_courses.csv"
    if not tracked_path.exists():
        print("no tracked_courses.csv; skipping coverage pass")
        return []
    tracked = list(csv.DictReader(tracked_path.open(encoding="utf-8")))
    providers = cfg["providers"]
    max_pages = int(cfg["settings"].get("max_pages_per_query", 50))

    # Pre-index inventory by normalised title for fast matching.
    inv_by_norm = {}
    for c in inventory.values():
        inv_by_norm.setdefault(_normalise(c["title"]), []).append(c)

    rows = []
    for t in tracked:
        tt = (t.get("tracked_title") or "").strip()
        if not tt:
            continue
        ttn = _normalise(tt)
        match = None
        # exact normalised match
        if ttn in inv_by_norm:
            match = inv_by_norm[ttn][0]
        else:
            # substring either direction across inventory
            for norm, cs in inv_by_norm.items():
                if ttn and (ttn in norm or norm in ttn):
                    match = cs[0]
                    break
        if not match:
            # targeted search (best-effort; skip if the API rejects this term)
            try:
                for p in range(0, max_pages):
                    data = client.page(tt, p)
                    for listing in _listings(data):
                        name = (listing.get("bildungsanbieter") or {}).get("name")
                        if provider_matches(name, providers):
                            ln = _normalise(listing.get("titel", ""))
                            if ln == ttn or (ttn and (ttn in ln or ln in ttn)):
                                match = flatten(listing)
                                break
                    if match or not _listings(data):
                        break
            except requests.RequestException as exc:
                print(f"  coverage lookup failed for '{tt}': {exc}", file=sys.stderr)

        if match:
            rows.append({
                "snapshot_date": TODAY, "type": t["type"], "tracked_title": tt,
                "status": "found", "match_title": match["title"],
                "course_id": match["course_id"], "locations": match["locations"],
                "next_start": match["next_start"], "cost_bands": match["cost_bands"],
                "weiterbildungsart": match["weiterbildungsart"],
            })
        else:
            rows.append({
                "snapshot_date": TODAY, "type": t["type"], "tracked_title": tt,
                "status": "missing", "match_title": "",
                "course_id": "", "locations": "", "next_start": "", "cost_bands": "",
                "weiterbildungsart": "",
            })
    found = sum(1 for r in rows if r["status"] == "found")
    print(f"[coverage] {found}/{len(rows)} tracked courses found on mein-now")
    return rows


def write_coverage(rows: list[dict]):
    if not rows:
        return
    DATA.mkdir(parents=True, exist_ok=True)
    hist = DATA / "coverage.csv"
    new = not hist.exists()
    with hist.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COVERAGE_FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)
    latest = DATA / "latest_coverage.csv"
    with latest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COVERAGE_FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote coverage -> coverage.csv (+ latest_coverage.csv)")


def main() -> int:
    cfg = load_config()
    client = Client(cfg["settings"])
    inventory = run_inventory(client, cfg)
    write_snapshot(inventory)
    write_changes(inventory)
    coverage = run_coverage(client, cfg, inventory)
    write_coverage(coverage)
    rankings = run_ranking(client, cfg)
    append_rankings(rankings)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
