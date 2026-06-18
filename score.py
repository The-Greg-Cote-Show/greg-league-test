#!/usr/bin/env python3
"""
score.py — League of Gregs Scoring Script
==========================================
Pulls news headlines from GNews for each person in the league,
identifies discrete events, scores them against the locked rubric,
and flags ambiguous events for commissioner review.

USAGE:
    python score.py --date 2026-06-17          # score a specific date
    python score.py --today                     # score today
    python score.py --month 2025-01             # score a full month (retroactive)
    python score.py --retroactive               # score all missing months Jan 2025-May 2026
    python score.py --review                    # show pending commissioner flags

SETUP:
    pip install requests
    Set GNEWS_API_KEY environment variable:
        Windows: set GNEWS_API_KEY=your_key_here
        Mac/Linux: export GNEWS_API_KEY=your_key_here

Get a free key at: https://gnews.io (100 requests/day free tier)
"""

import os
import sys
import json
import time
import argparse
import requests
from datetime import datetime, date, timedelta
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────────────────
SCORES_FILE = Path(__file__).parent / "scores.json"
GNEWS_API_KEY = os.environ.get("GNEWS_API_KEY", "")
GNEWS_BASE = "https://gnews.io/api/v4/search"
REQUEST_DELAY = 1.5  # seconds between API calls (stay under rate limit)

# ── RUBRIC ────────────────────────────────────────────────────────────────────
RUBRIC = {
    "major_award":       15,
    "major_project":     12,
    "life_event":        10,
    "career_milestone":   8,
    "major_appearance":   6,
    "notable_statement":  3,
    "incidental_mention": 1,
    "fired_cut":         -3,
    "arrested":         -15,
    "misconduct":       -20,
    "meltdown":          -8,
    "quiet_tax":         -3,
}

# ── KEYWORD CLASSIFIERS ───────────────────────────────────────────────────────
# Each category has a list of keywords. Headlines are matched case-insensitively.
# ORDER MATTERS — more specific categories are checked before general ones.
CLASSIFIERS = [
    ("arrested", [
        "arrested", "charged with", "indicted", "criminal charges",
        "taken into custody", "under arrest", "faces charges"
    ]),
    ("misconduct", [
        "sexual misconduct", "sexual assault", "harassment allegations",
        "abuse allegations", "misconduct confirmed", "found guilty",
        "convicted of"
    ]),
    ("meltdown", [
        "meltdown", "outburst", "caught on camera", "shocking behavior",
        "public embarrassment", "viral argument", "rant caught",
        "breaks down", "loses it"
    ]),
    ("fired_cut", [
        "fired", "let go", "released by", "cut from", "dropped by",
        "dismissed from", "no longer with", "parts ways with",
        "terminated", "exit from"
    ]),
    ("major_award", [
        "wins grammy", "wins oscar", "wins emmy", "wins golden globe",
        "wins tony", "wins bafta", "awarded the", "receives lifetime",
        "hall of fame", "wins award", "takes home", "championship",
        "super bowl win", "world series win", "nba champion", "world cup"
    ]),
    ("life_event", [
        "engaged", "engagement", "married", "wedding", "gives birth",
        "welcomes baby", "expecting", "pregnant", "announces pregnancy",
        "retires", "retirement announced", "divorce", "passes away",
        "death of"
    ]),
    ("major_project", [
        "album out", "new album", "releases album", "drops album",
        "movie opens", "film premieres", "box office", "debuts at number",
        "new book", "book release", "season premiere", "new series",
        "launches show", "new film", "starring in"
    ]),
    ("career_milestone", [
        "signs with", "signed to", "joins", "named as", "appointed",
        "promoted to", "lands role", "cast in", "deal with",
        "contract extension", "new deal", "traded to", "drafted"
    ]),
    ("major_appearance", [
        "super bowl", "oscars", "emmys", "grammys", "met gala",
        "state of the union", "keynote", "commencement", "halftime",
        "tonight show", "late night", "saturday night live", "snl",
        "world series", "nba finals", "headline", "headlining",
        "performs at", "concert tour"
    ]),
    ("notable_statement", [
        "speaks out", "opens up about", "interview with", "tells",
        "responds to", "reacts to", "breaks silence", "addresses",
        "op-ed", "essay", "memoir", "podcast appearance"
    ]),
    ("incidental_mention", [
        # Catch-all — any mention that passed through without matching above
        # Applied programmatically, not via keywords
    ]),
]

# ── AMBIGUITY THRESHOLDS ──────────────────────────────────────────────────────
# These event types always get flagged for commissioner review
ALWAYS_FLAG = {"major_award", "major_project", "life_event", "career_milestone"}

# ── HELPERS ───────────────────────────────────────────────────────────────────
def load_scores():
    with open(SCORES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_scores(data):
    with open(SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  Saved: {SCORES_FILE}")

def classify_headline(headline, description=""):
    """Match a headline against the classifier keywords.
    Returns (category, confidence) where confidence is 'high' or 'flag'."""
    text = (headline + " " + description).lower()
    for category, keywords in CLASSIFIERS:
        if not keywords:
            continue
        for kw in keywords:
            if kw in text:
                confidence = "flag" if category in ALWAYS_FLAG else "high"
                return category, confidence, kw
    return None, None, None

def deduplicate_events(articles):
    """Group articles about the same event. Simple approach: articles
    within 48 hours sharing a keyword cluster are the same event."""
    events = []
    used = set()
    for i, a in enumerate(articles):
        if i in used:
            continue
        cat, conf, kw = classify_headline(a.get("title",""), a.get("description",""))
        if not cat:
            continue
        event = {
            "headline": a.get("title", ""),
            "source": a.get("source", {}).get("name", "Unknown"),
            "url": a.get("url", ""),
            "published": a.get("publishedAt", "")[:10],
            "category": cat,
            "confidence": conf,
            "keyword_matched": kw,
            "article_count": 1
        }
        # Group subsequent articles with same keyword match
        for j, b in enumerate(articles[i+1:], i+1):
            if j in used:
                continue
            _, _, kw2 = classify_headline(b.get("title",""), b.get("description",""))
            if kw2 == kw:
                event["article_count"] += 1
                used.add(j)
        events.append(event)
        used.add(i)
    return events

def fetch_gnews(person_name, from_date, to_date):
    """Fetch news articles for a person within a date range."""
    if not GNEWS_API_KEY:
        print("  WARNING: No GNEWS_API_KEY set. Using empty results.")
        print("  Set it with: set GNEWS_API_KEY=your_key (Windows)")
        print("  Get a free key at: https://gnews.io")
        return []
    params = {
        "q": f'"{person_name}"',
        "from": from_date,
        "to": to_date,
        "lang": "en",
        "max": 10,
        "apikey": GNEWS_API_KEY,
        "sortby": "relevance"
    }
    try:
        resp = requests.get(GNEWS_BASE, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("articles", [])
    except requests.exceptions.HTTPError as e:
        if resp.status_code == 429:
            print(f"  Rate limited. Waiting 60s...")
            time.sleep(60)
            return fetch_gnews(person_name, from_date, to_date)
        print(f"  HTTP error for {person_name}: {e}")
        return []
    except Exception as e:
        print(f"  Error fetching {person_name}: {e}")
        return []

def score_person_day(person_name, target_date, articles):
    """Score a single person for a single day."""
    if not articles:
        return {
            "total": RUBRIC["quiet_tax"],
            "events": [{
                "type": "quiet_tax",
                "points": RUBRIC["quiet_tax"],
                "note": "No scoreable events found",
                "headline": "",
                "source": "",
                "flagged": False
            }]
        }

    events = deduplicate_events(articles)
    if not events:
        return {
            "total": RUBRIC["quiet_tax"],
            "events": [{
                "type": "quiet_tax",
                "points": RUBRIC["quiet_tax"],
                "note": "Articles found but no scoreable events identified",
                "headline": "",
                "source": "",
                "flagged": False
            }]
        }

    scored_events = []
    total = 0

    for ev in events:
        cat = ev["category"]
        pts = RUBRIC.get(cat, 0)
        flagged = ev["confidence"] == "flag"
        scored_events.append({
            "type": cat,
            "points": pts,
            "note": ev["headline"],
            "source": ev["source"],
            "url": ev["url"],
            "article_count": ev["article_count"],
            "keyword": ev["keyword_matched"],
            "flagged": flagged
        })
        if not flagged:
            total += pts

    return {"total": total, "events": scored_events}

def score_day(target_date_str):
    """Score all 15 people for a single day."""
    print(f"\nScoring: {target_date_str}")
    print("=" * 50)

    data = load_scores()
    people = list(data["scores"].keys())
    flags_generated = []

    for person in people:
        print(f"\n  {person}...")
        articles = fetch_gnews(person, target_date_str, target_date_str)
        result = score_person_day(person, target_date_str, articles)

        # Store score
        if target_date_str not in data["scores"][person]:
            data["scores"][person][target_date_str] = result
        else:
            print(f"    Already scored. Skipping. (Use --overwrite to force)")
            continue

        # Collect flags
        for ev in result["events"]:
            if ev.get("flagged"):
                flags_generated.append({
                    "date": target_date_str,
                    "person": person,
                    "event": ev,
                    "suggested_points": ev["points"],
                    "status": "pending"
                })

        pts = result["total"]
        flag_count = sum(1 for e in result["events"] if e.get("flagged"))
        print(f"    Score: {'+' if pts >= 0 else ''}{pts} | Events: {len(result['events'])} | Flags: {flag_count}")
        time.sleep(REQUEST_DELAY)

    # Add new flags
    data["flags"].extend(flags_generated)
    save_scores(data)

    if flags_generated:
        print(f"\n{'='*50}")
        print(f"COMMISSIONER REVIEW NEEDED: {len(flags_generated)} flag(s)")
        print(f"Run: python score.py --review")
        print(f"{'='*50}")

def score_month(year_month_str):
    """Score all 15 people for a full calendar month (retroactive)."""
    print(f"\nRetroactive scoring: {year_month_str}")
    print("=" * 50)

    year, month = map(int, year_month_str.split("-"))
    # Get last day of month
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)

    from_date = f"{year_month_str}-01"
    to_date = str(last_day)

    data = load_scores()
    people = list(data["scores"].keys())
    flags_generated = []

    for person in people:
        print(f"\n  {person}...")

        # Skip if already scored this month
        if year_month_str in data["scores"][person]:
            print(f"    Already scored. Skipping.")
            continue

        articles = fetch_gnews(person, from_date, to_date)
        result = score_person_day(person, year_month_str, articles)
        data["scores"][person][year_month_str] = result

        for ev in result["events"]:
            if ev.get("flagged"):
                flags_generated.append({
                    "date": year_month_str,
                    "person": person,
                    "event": ev,
                    "suggested_points": ev["points"],
                    "status": "pending"
                })

        pts = result["total"]
        flag_count = sum(1 for e in result["events"] if e.get("flagged"))
        print(f"    Score: {'+' if pts >= 0 else ''}{pts} | Events: {len(result['events'])} | Flags: {flag_count}")
        time.sleep(REQUEST_DELAY)

    data["flags"].extend(flags_generated)
    data["meta"]["last_updated"] = str(date.today())
    save_scores(data)

    if flags_generated:
        print(f"\n{'='*50}")
        print(f"COMMISSIONER REVIEW NEEDED: {len(flags_generated)} flag(s)")
        print(f"Run: python score.py --review")
        print(f"{'='*50}")

def score_retroactive():
    """Score all months from Jan 2025 through May 2026."""
    months = []
    # Jan 2025 through May 2026
    for year in [2025, 2026]:
        for month in range(1, 13):
            if year == 2026 and month > 5:
                break
            months.append(f"{year}-{month:02d}")

    print(f"Retroactive scoring: {months[0]} through {months[-1]}")
    print(f"Total months: {len(months)} × 15 people = {len(months)*15} scoring runs")
    print(f"Estimated time at {REQUEST_DELAY}s/request: ~{len(months)*15*REQUEST_DELAY/60:.0f} minutes")
    print("\nStarting in 5 seconds. Ctrl+C to cancel.")
    time.sleep(5)

    for ym in months:
        score_month(ym)
        print(f"\nCompleted {ym}. Pausing 3s before next month...")
        time.sleep(3)

    print("\nRetroactive scoring complete.")

def show_review():
    """Display all pending commissioner flags."""
    data = load_scores()
    pending = [f for f in data["flags"] if f["status"] == "pending"]

    if not pending:
        print("\nNo pending flags. All scores confirmed.")
        return

    print(f"\nCOMMISSIONER REVIEW — {len(pending)} pending flag(s)")
    print("=" * 60)

    for i, flag in enumerate(pending):
        ev = flag["event"]
        print(f"\n[{i+1}] {flag['person']} — {flag['date']}")
        print(f"  Headline:  {ev.get('note','')[:80]}")
        print(f"  Source:    {ev.get('source','')}")
        print(f"  Category:  {ev['type']} ({'+' if ev['points']>=0 else ''}{ev['points']} pts)")
        print(f"  Keyword:   {ev.get('keyword','')}")
        print(f"  Suggested: {'+' if flag['suggested_points']>=0 else ''}{flag['suggested_points']} pts")
        print()

    print("=" * 60)
    print("To rule on a flag, edit scores.json directly:")
    print("  1. Find the flag in the 'flags' array")
    print("  2. Change 'status' from 'pending' to 'confirmed' or 'overridden'")
    print("  3. If overriding, add 'override_points' with the correct value")
    print("  4. Add the points to the person's score for that date/month manually")
    print()
    print("(A commissioner CLI is coming — for now, edit the JSON directly)")

def show_standings():
    """Print current standings to console."""
    data = load_scores()

    totals = {}
    for person in data["scores"]:
        total = 0
        for date_key, day_data in data["scores"][person].items():
            total += day_data.get("total", 0)
        totals[person] = total

    owner_totals = {}
    for owner, info in data["owners"].items():
        owner_total = sum(totals.get(m, 0) for m in info["members"])
        owner_totals[owner] = owner_total

    print("\nLEAGUE OF GREGS — CURRENT STANDINGS")
    print("=" * 40)
    for owner, total in sorted(owner_totals.items(), key=lambda x: -x[1]):
        print(f"  {owner:<10} {total:+d}")

    print("\nINDIVIDUAL SCORES")
    print("-" * 40)
    for person, total in sorted(totals.items(), key=lambda x: -x[1]):
        owner = next(o for o, info in data["owners"].items() if person in info["members"])
        print(f"  {person:<25} {total:+5d}  ({owner})")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="League of Gregs Scoring Script")
    parser.add_argument("--today", action="store_true", help="Score today")
    parser.add_argument("--date", type=str, help="Score a specific date (YYYY-MM-DD)")
    parser.add_argument("--month", type=str, help="Score a full month retroactively (YYYY-MM)")
    parser.add_argument("--retroactive", action="store_true", help="Score all months Jan 2025-May 2026")
    parser.add_argument("--review", action="store_true", help="Show pending commissioner flags")
    parser.add_argument("--standings", action="store_true", help="Print current standings")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing scores")
    args = parser.parse_args()

    if args.review:
        show_review()
    elif args.standings:
        show_standings()
    elif args.today:
        score_day(str(date.today()))
    elif args.date:
        score_day(args.date)
    elif args.month:
        score_month(args.month)
    elif args.retroactive:
        score_retroactive()
    else:
        parser.print_help()
        print("\nQuick start:")
        print("  python score.py --today          # score today")
        print("  python score.py --standings      # view current standings")
        print("  python score.py --review         # review flagged events")

if __name__ == "__main__":
    main()
