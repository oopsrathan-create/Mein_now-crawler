#!/usr/bin/env python3
"""Keyword rank tracker for mein-now.de (modelled on an SEO/SERP rank tracker).

For each keyword in keywords.txt this records, per run:

  1. Brand rank      - where the configured brand (ecomex) ranks, best position
                       and how many of its listings appear.
  2. Market landscape- total results and the providers occupying the top results.
  3. Competitor share- for the visible "first page" (top_n), each provider's share
                       of the results and their best position.
  4. Discovery       - candidate keywords mined from the brand's own course titles,
                       scored by how visibly the brand ranks for them, so you can
                       promote good ones into keywords.txt.

Outputs (under data/):
  keyword_ranks.csv         appended every run - brand rank history per keyword
  keyword_competitors.csv   appended every run - competitor share per keyword
  keyword_discovery.csv     overwritten every run - scored candidate keywords
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TODAY = date.today().isoformat()

_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß0-9&\-]{2,}")


def clean_text(s: str, cap: int = 1500) -> str:
    s = _TAG_RE.sub(" ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:cap]
_STOP = {
    "und", "fuer", "für", "mit", "der", "die", "das", "von", "im", "in", "zum",
    "zur", "den", "des", "auf", "als", "bei", "the", "and", "for", "with",
}


def load_config() -> dict:
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    providers = [p for p in cfg.get("providers", []) if p and "REPLACE" not in p]
    if not providers:
        sys.exit("No providers configured in config.json.")
    cfg["providers"] = providers
    return cfg


def load_keywords() -> list[str]:
    out = []
    for line in (ROOT / "keywords.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


class Client:
    def __init__(self, settings: dict):
        self.host = settings["backend_host"].rstrip("/")
        self.size = int(settings.get("page_size", 20))
        self.delay = float(settings.get("request_delay_seconds", 0.4))
        self.extra = settings.get("search_params") or {}   # mirror the website's default filters
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": settings["api_key"],
            "User-Agent": "Mozilla/5.0 (brand-keyword-tracker)",
            "Accept": "application/json",
        })

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
                time.sleep(2 ** attempt)
                print(f"  retry ({exc})", file=sys.stderr)
        return {}


def listings(data: dict) -> list:
    return (data.get("_embedded") or {}).get("bildungsangebotDTOList") or []


def matches(name: str, providers: list[str]) -> bool:
    n = (name or "").casefold()
    return any(p.casefold() in n for p in providers)


def analyse_keyword(client: Client, keyword: str, providers: list[str], top_n: int, scan_depth: int):
    """Return (rank_row, competitor_rows, catalog_obs) for one keyword.

    catalog_obs is one entry per scanned course (any provider) so callers can
    build a full competitor catalogue — the titles are already in the responses
    we fetch here, so capturing them costs no extra requests."""
    first = client.page(keyword, 0)
    total = first.get("page", {}).get("totalElements", 0)
    total_pages = first.get("page", {}).get("totalPages", 1)

    rank = 0
    brand_best = None
    brand_course = ""
    brand_in_top_n = 0
    brand_in_scan = 0
    top_n_provider_counts: Counter = Counter()
    top_n_provider_best: dict[str, int] = {}
    catalog_obs: list[dict] = []

    def consume(data: dict):
        nonlocal rank, brand_best, brand_course, brand_in_top_n, brand_in_scan
        for c in listings(data):
            name = (c.get("bildungsanbieter") or {}).get("name", "")
            pos = rank + 1
            catalog_obs.append({
                "course_id": c.get("id"),
                "title": c.get("titel", ""),
                "provider": name,
                "weiterbildungsart": c.get("weiterbildungsart", ""),
                "description": c.get("inhalt") or "",
                "pos": pos,
            })
            if rank < top_n:
                top_n_provider_counts[name] += 1
                top_n_provider_best.setdefault(name, pos)
            if matches(name, providers):
                brand_in_scan += 1
                if rank < top_n:
                    brand_in_top_n += 1
                if brand_best is None:
                    brand_best = pos
                    brand_course = c.get("titel", "")
            rank += 1

    consume(first)
    max_pages = min(total_pages, (scan_depth + client.size - 1) // client.size)
    for p in range(1, max_pages):
        consume(client.page(keyword, p))

    rank_row = {
        "snapshot_date": TODAY,
        "keyword": keyword,
        "total_results": total,
        "brand_best_rank": brand_best if brand_best is not None else "",
        "brand_course": brand_course,
        "brand_in_top_n": brand_in_top_n,
        "brand_in_scan": brand_in_scan,
        "top_n": top_n,
        "scanned": rank,
    }
    comp_rows = []
    for name, cnt in top_n_provider_counts.most_common():
        comp_rows.append({
            "snapshot_date": TODAY,
            "keyword": keyword,
            "provider": name,
            "count_top_n": cnt,
            "share_pct": round(100 * cnt / max(rank if rank < top_n else top_n, 1), 1),
            "best_rank": top_n_provider_best.get(name, ""),
        })
    return rank_row, comp_rows, catalog_obs


def discover_candidates(client: Client, providers: list[str], limit: int) -> list[str]:
    """Mine candidate keywords from the brand's own course titles."""
    brand = providers[0]
    data = client.page(brand, 0)
    titles = [c.get("titel", "") for c in listings(data) if matches((c.get("bildungsanbieter") or {}).get("name"), providers)]
    counts: Counter = Counter()
    for t in titles:
        words = [w for w in _WORD_RE.findall(t) if w.casefold() not in _STOP]
        counts.update(w.title() for w in words)
        for i in range(len(words) - 1):  # bigrams
            counts[f"{words[i].title()} {words[i+1].title()}"] += 1
    return [kw for kw, _ in counts.most_common(limit)]


RANK_FIELDS = ["snapshot_date", "keyword", "total_results", "brand_best_rank", "brand_course", "brand_in_top_n", "brand_in_scan", "top_n", "scanned"]
COMP_FIELDS = ["snapshot_date", "keyword", "provider", "count_top_n", "share_pct", "best_rank"]
DISC_FIELDS = ["keyword", "brand_best_rank", "brand_course", "total_results"]
CATALOG_FIELDS = ["snapshot_date", "provider", "is_brand", "major", "course_id", "title", "weiterbildungsart", "keyword_count", "best_rank", "best_page", "keywords", "description"]


def append_csv(path: Path, fields: list[str], rows: list[dict]):
    DATA.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerows(rows)


def write_csv(path: Path, fields: list[str], rows: list[dict]):
    DATA.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    cfg = load_config()
    kt = cfg.get("keyword_tracker", {})
    top_n = int(kt.get("top_n", 20))
    scan_depth = int(kt.get("rank_scan_depth", 500))
    top_competitors = int(kt.get("top_competitors", 15))
    discovery_max = int(kt.get("discovery_max", 40))

    client = Client(cfg["settings"])
    providers = cfg["providers"]
    keywords = load_keywords()

    rank_rows, comp_rows = [], []
    catalog: dict = {}   # course_id -> aggregated course record across all keywords
    for kw in keywords:
        rr, cr, cat = analyse_keyword(client, kw, providers, top_n, scan_depth)
        rank_rows.append(rr)
        comp_rows.extend(cr[:top_competitors])
        for obs in cat:
            cid = obs["course_id"]
            if cid is None or cid == "":
                continue
            a = catalog.get(cid)
            if a is None:
                catalog[cid] = {
                    "provider": obs["provider"],
                    "is_brand": 1 if matches(obs["provider"], providers) else 0,
                    "title": obs["title"],
                    "weiterbildungsart": obs["weiterbildungsart"],
                    "description": obs.get("description", ""),
                    "best_rank": obs["pos"],
                    "kw_pos": {kw: obs["pos"]},   # keyword -> best position
                }
            else:
                if obs["pos"] < a["kw_pos"].get(kw, 10**9):
                    a["kw_pos"][kw] = obs["pos"]
                if obs["pos"] < a["best_rank"]:
                    a["best_rank"] = obs["pos"]
                if not a["description"] and obs.get("description"):
                    a["description"] = obs["description"]
        print(f"[rank] {kw}: brand best={rr['brand_best_rank'] or '—'} of {rr['total_results']}")

    append_csv(DATA / "keyword_ranks.csv", RANK_FIELDS, rank_rows)
    append_csv(DATA / "keyword_competitors.csv", COMP_FIELDS, comp_rows)
    print(f"wrote {len(rank_rows)} rank rows, {len(comp_rows)} competitor rows")

    # Competitor catalogue: one row per unique course (any provider) seen across
    # the keyword scans, deduped by id. Powers the separate competitors page.
    # "major" = the top-N providers by course count; only those carry full course
    # descriptions, so the dataset (and the page) stays fast.
    major_n = int(kt.get("major_competitors", 25))
    prov_counts: Counter = Counter()
    for a in catalog.values():
        prov_counts[a["provider"]] += 1
    major_providers = {p for p, _ in prov_counts.most_common(major_n)}

    def page_of(pos):
        return (int(pos) - 1) // top_n + 1

    cat_rows = []
    for cid, a in catalog.items():
        is_major = a["provider"] in major_providers
        # keyword -> best position (rank), best keyword first. Format "keyword (#rank)";
        # the page is derivable as ceil(rank / top_n) in the dashboard.
        kw_items = sorted(a["kw_pos"].items(), key=lambda kv: kv[1])
        kw_str = "; ".join(f"{k} (#{p})" for k, p in kw_items[:15])
        cat_rows.append({
            "snapshot_date": TODAY,
            "provider": a["provider"],
            "is_brand": a["is_brand"],
            "major": 1 if is_major else 0,
            "course_id": cid,
            "title": a["title"],
            "weiterbildungsart": a["weiterbildungsart"],
            "keyword_count": len(a["kw_pos"]),
            "best_rank": a["best_rank"],
            "best_page": page_of(a["best_rank"]),
            "keywords": kw_str,
            "description": clean_text(a["description"]) if is_major else "",
        })
    cat_rows.sort(key=lambda r: (r["provider"].casefold(), r["best_rank"]))
    write_csv(DATA / "latest_competitor_catalog.csv", CATALOG_FIELDS, cat_rows)
    print(f"wrote {len(cat_rows)} catalogue courses across {len(prov_counts)} providers "
          f"({len(major_providers)} major w/ descriptions) -> latest_competitor_catalog.csv")

    # Discovery: candidates mined from brand titles, not already tracked.
    tracked = {k.casefold() for k in keywords}
    candidates = [c for c in discover_candidates(client, providers, discovery_max) if c.casefold() not in tracked]
    disc_rows = []
    for kw in candidates:
        rr, _, _ = analyse_keyword(client, kw, providers, top_n, scan_depth)
        if rr["brand_best_rank"] != "":
            disc_rows.append({
                "keyword": kw,
                "brand_best_rank": rr["brand_best_rank"],
                "brand_course": rr["brand_course"],
                "total_results": rr["total_results"],
            })
    disc_rows.sort(key=lambda r: r["brand_best_rank"])
    write_csv(DATA / "keyword_discovery.csv", DISC_FIELDS, disc_rows)
    print(f"discovery: {len(disc_rows)} candidate keywords where brand ranks")
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
