#!/usr/bin/env python3
"""Competitor analysis crawler for mein-now.de.

Two jobs, one crawl:

  1. CATALOGUE — discover every Anbieter (provider) competing in our field and,
     under each, every course they list: id, title, full description (copied
     as-is, not summarised) and a link to it on mein-now.

     mein-now's API has no "list all providers" or provider-filter endpoint, so
     we (a) discover the providers in our field by scanning the field's search
     terms (keywords.txt), then (b) pull each provider's full catalogue via a
     provider-name search — the same mechanism crawl.py uses for our own
     inventory.

  2. TOP KEYWORDS — the most common terms across all those course titles: what
     the competitive field is actually selling.

Outputs (under data/):
  competitors/providers.csv            one row per Anbieter: course count + top terms
  competitors/<slug>.json              that Anbieter's full course list incl. descriptions
  competitor_top_keywords.csv          top terms across the whole field

Bounds (config["competitor_analysis"]): discovery_depth, max_providers,
max_pages_per_provider, top_keywords.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from crawl import Client, flatten, load_config, provider_matches

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
COMP = DATA / "competitors"
TODAY = date.today().isoformat()

_WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß0-9&\-]{2,}")
_STOP = {
    "und", "fuer", "für", "mit", "der", "die", "das", "von", "im", "in", "zum",
    "zur", "den", "des", "auf", "als", "bei", "the", "and", "for", "with", "ihk",
    "online", "kurs", "weiterbildung", "schulung", "seminar",
}
PROV_FIELDS = ["snapshot_date", "provider", "slug", "is_brand", "courses", "top_terms"]
KW_FIELDS = ["term", "courses", "providers"]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:70] or "anbieter"


def field_terms() -> list[str]:
    """The search terms that define our field — keywords.txt (+ listing keywords)."""
    terms: list[str] = []
    for fn in ("keywords.txt", "listing_keywords.txt"):
        p = ROOT / fn
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    terms.append(line)
    seen, out = set(), []
    for t in terms:
        if t.casefold() not in seen:
            seen.add(t.casefold()); out.append(t)
    return out


def discover_providers(client: Client, terms: list[str], depth: int) -> list[str]:
    """Distinct Anbieter appearing in the field's search results, most-frequent first."""
    freq: Counter = Counter()
    for t in terms:
        for _rank, listing, _total in client.iter_listings(t, depth):
            name = (listing.get("bildungsanbieter") or {}).get("name")
            if name:
                freq[name] += 1
        print(f"[discover] '{t}': {len(freq)} providers so far")
    return [p for p, _ in freq.most_common()]


def course_link(course_id, title: str) -> str:
    # Public Weiterbildungssuche; opens the course/search on mein-now.
    if course_id:
        return f"https://mein-now.de/weiterbildungssuche/suche/{course_id}"
    return f"https://mein-now.de/weiterbildungssuche/suche?sw={title.replace(' ', '+')}"


def crawl_provider(client: Client, provider: str, max_pages: int) -> list[dict]:
    """Every course listed under `provider` (deduped by id), with full description."""
    out: dict = {}
    for _rank, listing, _total in client.iter_listings(provider, max_pages):
        name = (listing.get("bildungsanbieter") or {}).get("name")
        if not provider_matches(name, [provider]):
            continue
        cid = listing.get("id")
        if cid in out:
            continue
        flat = flatten(listing)          # description = full inhalt, tags stripped, not truncated
        out[cid] = {
            "id": cid,
            "title": flat.get("title", ""),
            "weiterbildungsart": flat.get("weiterbildungsart", ""),
            "locations": flat.get("locations", ""),
            "next_start": flat.get("next_start", ""),
            "link": course_link(cid, flat.get("title", "")),
            "description": flat.get("description", ""),
        }
    return list(out.values())


def top_terms(titles: list[str], n: int) -> list[tuple[str, int]]:
    c: Counter = Counter()
    for t in titles:
        words = [w for w in _WORD_RE.findall(t) if w.casefold() not in _STOP]
        c.update(w.title() for w in words)
        for i in range(len(words) - 1):
            c[f"{words[i].title()} {words[i+1].title()}"] += 1
    return c.most_common(n)


def main() -> int:
    cfg = load_config()
    ca = cfg.get("competitor_analysis", {})
    depth = int(ca.get("discovery_depth", 10))            # pages per field term when discovering
    max_providers = int(ca.get("max_providers", 200))
    max_pages = int(ca.get("max_pages_per_provider", 25))
    top_n = int(ca.get("top_keywords", 60))
    brand = cfg["providers"]

    client = Client(cfg["settings"])
    COMP.mkdir(parents=True, exist_ok=True)

    providers = discover_providers(client, field_terms(), depth)[:max_providers]
    print(f"discovered {len(providers)} providers in field")

    prov_rows = []
    field_titles: list[str] = []
    term_courses: Counter = Counter()
    term_providers: defaultdict[str, set] = defaultdict(set)

    for i, prov in enumerate(providers, 1):
        courses = crawl_provider(client, prov, max_pages)
        if not courses:
            continue
        titles = [c["title"] for c in courses]
        field_titles += titles
        tt = top_terms(titles, 8)
        # accumulate field-wide term stats
        for c in courses:
            terms_in = {w.title() for w in _WORD_RE.findall(c["title"]) if w.casefold() not in _STOP}
            for term in terms_in:
                term_courses[term] += 1
                term_providers[term].add(prov)
        slug = slugify(prov)
        (COMP / f"{slug}.json").write_text(
            json.dumps({"snapshot_date": TODAY, "provider": prov, "courses": courses},
                       ensure_ascii=False), encoding="utf-8")
        prov_rows.append({
            "snapshot_date": TODAY, "provider": prov, "slug": slug,
            "is_brand": 1 if provider_matches(prov, brand) else 0,
            "courses": len(courses),
            "top_terms": "; ".join(t for t, _ in tt),
        })
        print(f"[{i}/{len(providers)}] {prov}: {len(courses)} Kurse")

    prov_rows.sort(key=lambda r: r["courses"], reverse=True)
    with (COMP / "providers.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PROV_FIELDS); w.writeheader(); w.writerows(prov_rows)

    # field-wide top keywords: by number of courses, then providers
    rows = [{"term": t, "courses": term_courses[t], "providers": len(term_providers[t])}
            for t in term_courses]
    rows.sort(key=lambda r: (r["courses"], r["providers"]), reverse=True)
    with (DATA / "competitor_top_keywords.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=KW_FIELDS); w.writeheader(); w.writerows(rows[:top_n])

    print(f"done: {len(prov_rows)} providers, {len(field_titles)} courses, "
          f"{min(top_n, len(rows))} top keywords")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
