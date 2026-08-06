#!/usr/bin/env python3
"""Shared brief-parsing library for Signal Stacking.

Single source of truth for reading `research/*.md` briefs and the account
data files. Imported by both `scripts/build_dashboard.py` (static dashboard)
and `scripts/scout_server.py` (Signal Scout app) so the two never disagree.

`slugify()` is the CANONICAL company→filename rule. The prospect-researcher
agent must produce the same slugs (lowercase, non-alphanumerics → hyphen,
collapsed); if this rule ever changes, change it here and in
`.claude/agents/prospect-researcher.md` together.
"""

import csv
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESEARCH = ROOT / "research"
ACCOUNTS = ROOT / "accounts"
STALE_DAYS = 30
FRESH_DAYS = 14  # matches the orchestrator's freshness cache

TIER_ORDER = {"🔥": 0, "🟡": 1, "⚪": 2, "—": 3}
TIER_LABEL = {"🔥": "In-market now", "🟡": "Warming", "⚪": "Monitor", "—": "Untiered"}

# Legal suffixes stripped when matching company names across files.
_SUFFIXES = r"(?:inc|incorporated|ltd|limited|llc|llp|gmbh|co|corp|corporation|plc|sa|ag|bv|oy|ab|pty|holdings)"


def slugify(company):
    """Company name → brief filename stem. Canonical rule — see module docstring."""
    s = re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def normalize_company(name):
    """Loose key for joining company names across CSVs ('Apollo Inc.' ≈ 'apollo')."""
    s = name.lower().strip()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(rf"\b{_SUFFIXES}\b\.?", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def section(md, *names):
    """Return the body of the first matching ## section (case-insensitive)."""
    for name in names:
        m = re.search(
            rf"^##\s+{re.escape(name)}[^\n]*\n(.*?)(?=^##[^#]|\Z)",
            md, re.M | re.S | re.I,
        )
        if m:
            return m.group(1).strip()
    return ""


def subsection(md, *names):
    for name in names:
        m = re.search(
            rf"^###\s+{re.escape(name)}[^\n]*\n(.*?)(?=^###|^##[^#]|\Z)",
            md, re.M | re.S | re.I,
        )
        if m:
            return m.group(1).strip()
    return ""


def first_line(text):
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def parse_touch1_variants(seq):
    """All `### Touch 1 …` blocks in a sequence section → [{label, body}].

    New-format briefs carry three variants (`### Touch 1 — Variant A (signal-led)`);
    legacy briefs carry one plain `### Touch 1 — Email (day 0)` and yield a single
    entry. Empty list if the section has no Touch 1 heading at all.
    """
    out = []
    for m in re.finditer(
        r"^###\s+(Touch 1[^\n]*)\n(.*?)(?=^###|^##[^#]|\Z)",
        seq, re.M | re.S | re.I,
    ):
        label = m.group(1).strip()
        body = m.group(2).strip()
        if body:
            out.append({"label": label, "body": body})
    return out


def parse_brief(path, today, email_map):
    md = path.read_text(encoding="utf-8")
    m = re.search(r"^#\s+(?:Signal Stacking Brief\s*—\s*|)(.+?)(?:\s*—\s*Prospecting Brief)?\s*$", md, re.M)
    company = m.group(1).strip() if m else path.stem.replace("-", " ").title()

    m = re.search(r"_(?:Researched|Generated):\s*(\d{4}-\d{2}-\d{2})", md)
    researched = m.group(1) if m else ""
    age_days = None
    if researched:
        try:
            age_days = (today - datetime.strptime(researched, "%Y-%m-%d").date()).days
        except ValueError:
            pass

    m = re.search(r"Tier:\s*(🔥|🟡|⚪)", md)
    tier = m.group(1) if m else "—"

    m = re.search(r"Fit:\s*(\w+)", md)
    fit = m.group(1) if m else ""
    m = re.search(r"Timing:\s*(-?\d+)\s*pts", md)
    timing = int(m.group(1)) if m else None

    # Optional `· Entry: <Name>` in the metadata line (new-format briefs).
    m = re.search(r"·\s*Entry:\s*([^·\n]+)", md)
    entry_meta = m.group(1).strip() if m else ""

    why_now = first_line(section(md, "Why now"))
    if not why_now:  # legacy brief: fall back to the angle's first sentence
        angle = section(md, "Suggested Outreach Angle", "Stacked Angles")
        why_now = first_line(angle)[:220]

    # New sections (all optional — "" on legacy briefs).
    committee = section(md, "Buying Committee")
    entry_point = section(md, "Best Point of Entry")
    filings = section(md, "Financial Filings & Earnings", "Financial Filings")
    closed_lost = section(md, "Closed-Lost History")

    # Contact: Best Point of Entry first, then committee/people buckets, then legacy.
    contact = ""
    m = re.search(r"\*\*(.+?)\*\*", entry_point)
    if m:
        contact = m.group(1).strip().rstrip("—-, ")
    if not contact:
        people = section(md, "Buying Committee", "Key People", "Target Contact")
        m = re.search(r"\*\*Economic buyer:\*\*\s*([^\n—-]+(?:—|,|-)?[^\n[]*)", people)
        if m:
            contact = m.group(1).strip().rstrip("—-, ")
        else:
            m = re.search(r"\*\*(.+?)\*\*\s*—\s*([^\n(]+)", people)
            if m:
                contact = f"{m.group(1).strip()}, {m.group(2).strip()}"
    contact = re.sub(r"\s*\[.*?\]\(.*?\)", "", contact).strip()

    switch = "yes" if re.search(r"SWITCH PLAY", md, re.I) else ""
    m = re.search(r"suppressed-until-(\S+)|Re-check:\s*(\S+)", md, re.I)
    recheck = (m.group(1) or m.group(2)) if m else ""

    seq = section(md, "Outreach Sequence", "Drafted Email")
    touch1_variants = parse_touch1_variants(seq)
    touch1 = (touch1_variants[0]["body"] if touch1_variants
              else (seq if seq and "### " not in seq else ""))
    touch2 = subsection(seq, "Touch 2")
    touch3 = subsection(seq, "Touch 3")

    flags = []
    if tier == "—":
        flags.append("untiered — re-run for scoring")
    if age_days is not None and age_days > STALE_DAYS:
        flags.append(f"{age_days}d old — re-verify contact")
    if recheck:
        flags.append(f"re-check {recheck}")

    email = email_map.get(company.lower(), "")

    return {
        "company": company,
        "slug": path.stem,
        "file": path.name,
        "tier": tier,
        "tierLabel": TIER_LABEL[tier],
        "fit": fit,
        "timing": timing,
        "whyNow": why_now,
        "contact": contact,
        "email": email,
        "switchPlay": switch,
        "researched": researched,
        "ageDays": age_days,
        "flags": flags,
        "touch1": touch1,
        "touch1Variants": touch1_variants,
        "touch2": touch2,
        "touch3": touch3,
        "entry": entry_meta or (contact if entry_point else ""),
        "entryPoint": entry_point,
        "committee": committee,
        "filings": filings,
        "closedLost": closed_lost,
        "markdown": md,
    }


def load_email_map():
    """company (lower) → email, from any accounts CSV that has both columns."""
    out = {}
    if not ACCOUNTS.is_dir():
        return out
    for p in sorted(ACCOUNTS.glob("*.csv")):
        try:
            rows = list(csv.DictReader(p.open(encoding="utf-8-sig")))
        except Exception:
            continue
        if not rows:
            continue
        cols = {c.lower().strip(): c for c in rows[0].keys() if c}
        comp = next((cols[c] for c in cols if "company" in c and "domain" not in c), None)
        mail = next((cols[c] for c in cols if "email" in c and "?" not in c and "all" not in c), None)
        if not comp or not mail:
            continue
        for r in rows:
            c, e = (r.get(comp) or "").strip(), (r.get(mail) or "").strip()
            if c and e and "@" in e:
                out.setdefault(c.lower(), e)
    return out


def load_closed_lost(path=None):
    """accounts/closed-lost.csv → {join_key: row}.

    Each row is keyed by BOTH its domain (lowercased, if present) and its
    normalized company name, so callers can join domain-first with a
    name fallback. Missing file → {}.
    """
    p = Path(path) if path else ACCOUNTS / "closed-lost.csv"
    out = {}
    if not p.is_file():
        return out
    try:
        rows = list(csv.DictReader(p.open(encoding="utf-8-sig")))
    except Exception:
        return out
    for r in rows:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        if not row.get("company") and not row.get("domain"):
            continue
        if row.get("domain"):
            out[row["domain"].lower()] = row
        if row.get("company"):
            out.setdefault(normalize_company(row["company"]), row)
    return out


def load_briefs(today, email_map=None):
    """Parse every brief in research/, hottest first. Bad files are skipped."""
    if email_map is None:
        email_map = load_email_map()
    briefs = []
    for p in sorted(RESEARCH.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            briefs.append(parse_brief(p, today, email_map))
        except Exception as e:  # never let one bad brief kill the caller
            print(f"  ! skipped {p.name}: {e}")
    briefs.sort(key=lambda b: (TIER_ORDER[b["tier"]], -(b["timing"] or -99), b["company"].lower()))
    return briefs
