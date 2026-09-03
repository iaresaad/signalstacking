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
# A brief with no `Tier:` metadata line predates scoring entirely — it is not a
# scoring failure, and no technographic data exists to score it offline. Say so
# rather than implying the scorer looked at it and returned nothing.
LEGACY_LABEL = "Legacy — never scored"

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


def strip_md(s):
    """Plain text from inline markdown — the table shows this raw, unrendered."""
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)   # [label](url) -> label
    s = re.sub(r"(\*\*|__|\*|_|`)", "", s)
    return re.sub(r"\s{2,}", " ", s).strip()


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


COMMITTEE_RE = re.compile(
    r"^-\s+\*\*(?P<role>[^:*]+):\*\*\s*(?P<rest>.+)$", re.M)


def _looks_like_name(s):
    """Cheap guard against narrative text being mistaken for a person."""
    if not s or any(ch.isdigit() for ch in s):
        return False
    words = s.split()
    if not (2 <= len(words) <= 5):
        return False
    return all(w[:1].isupper() or w.lower() in ("de", "van", "der", "von", "da", "di")
               for w in words if w[:1].isalpha())


def _split_people(rest):
    """Split a bullet that genuinely holds several people.

    Only ';' that terminates a person's own [LinkedIn](…) link counts, and only
    when the bullet carries more than one such link. Narrative fields legitimately
    contain semicolons ("activity: posted a req, 2026-08-09; reshared the MQ
    news, 2026-07-22"), and splitting on those invents contacts.
    """
    if len(re.findall(r"\[LinkedIn\]\(", rest, re.I)) < 2:
        return [rest]
    return [s.strip() for s in re.split(r"(?<=\))\s*;\s*", rest) if s.strip()]


def _clean_title(title):
    """Trim a title down to something Apollo can match on.

    Drops parenthesised asides ("(since Apr 2022)", "(ex-6sense VP)") including
    an unbalanced trailing one left by an earlier split, plus markdown links.
    """
    title = re.sub(r"\s*\[[^\]]*\]\([^)]*\)", "", title)
    title = re.sub(r"\s*\([^)]*\)", "", title)
    title = re.sub(r"\s*\(.*$", "", title)
    title = re.sub(r"\s*`[^`]*`", "", title)
    return re.sub(r"\s{2,}", " ", title).strip(" .,;—-")


def _split_name_title(rest):
    """'Sean Esna, Director, Field Sales — needs: …' -> ('Sean Esna', 'Director, Field Sales')."""
    rest = re.sub(r"\s*\[[^\]]*\]\([^)]*\)", "", rest)          # strip md links
    head = re.split(r"\s+—\s+|\s+\bneeds:|\s+\bactivity:", rest)[0].strip()
    head = re.sub(r"\s*\((?:in seat|since)[^)]*\)", "", head).strip(" .—-")
    if "," in head:
        name, title = head.split(",", 1)
        return name.strip(), _clean_title(title)
    return head.strip(), ""


def parse_committee(md):
    """`## Buying Committee` bullets -> [{role, name, title, linkedin}].

    Only `- **Role:** …` bullets are read, which deliberately excludes the
    free-text "Notable departures" paragraph — those people have left and must
    never be enriched or contacted.
    """
    body = section(md, "Buying Committee", "Key People")
    out = []
    for m in COMMITTEE_RE.finditer(body):
        role = m.group("role").strip()
        for chunk in _split_people(m.group("rest").strip()):
            name, title = _split_name_title(chunk)
            if not _looks_like_name(name):    # a role bullet with no real person
                continue
            li = re.search(r"\[LinkedIn\]\((https?://[^)]+)\)", chunk, re.I)
            out.append({
                "role": role,
                "name": name,
                "title": title,
                "linkedin": li.group(1) if li else "",
                "entry": "ENTRY POINT" in role.upper(),
            })
    return out


def parse_target_contact(md):
    """Legacy `## Target Contact` -> [{role, name, title, linkedin}].

    Format is `**Name** — Title`. Italic `_Note: …_` paragraphs are dropped
    first: they exist precisely to name people who are NOT at the company
    (Clay's note flags a competitor's CRO quoted in a testimonial).
    """
    body = section(md, "Target Contact", "Key Contact")
    # Drop italic annotation lines only (line-scoped): they name people who are
    # explicitly NOT the target — a competitor's exec quoted in a testimonial,
    # or "verified" context about someone else.
    body = re.sub(r"^_[^\n]*_\s*$", "", body, flags=re.M)
    matches = list(re.finditer(
        r"\*\*(?P<name>[^*\n]+?)\*\*\s*(?:\([^)]*\)\s*)?(?:—|--|-)\s*(?P<title>[^\n]+)", body))
    out = []
    for i, m in enumerate(matches):
        name = _clean_title(m.group("name"))
        if not _looks_like_name(name):
            continue
        # scope the LinkedIn lookup to this person's own slice of the section
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        li = re.search(r"(https?://(?:www\.)?linkedin\.com/in/[^\s)]+)",
                       body[m.end():stop], re.I)
        out.append({"role": "Target Contact", "name": name,
                    "title": _clean_title(m.group("title")),
                    "linkedin": li.group(1) if li else "", "entry": i == 0})
    return out


def brief_contacts(md, entry_meta=""):
    """Every real person worth enriching, entry contact first, deduped by name."""
    people = parse_committee(md) or parse_target_contact(md)
    for p in people:
        if entry_meta and p["name"].lower() == entry_meta.lower():
            p["entry"] = True
    if entry_meta and not any(p["entry"] for p in people):
        people.insert(0, {"role": "Best Point of Entry", "name": entry_meta,
                          "title": "", "linkedin": "", "entry": True})
    seen, out = set(), []
    for p in sorted(people, key=lambda x: not x["entry"]):
        k = p["name"].lower()
        if k not in seen:
            seen.add(k)
            out.append(p)
    return out


HOST_RE = re.compile(r"https?://(?:www\.)?([a-z0-9][a-z0-9.-]*\.[a-z]{2,})", re.I)
PAREN_DOMAIN_RE = re.compile(
    r"\(((?:[a-z0-9][a-z0-9-]*\.)+(?:com|io|ai|co|dev|app|net|org|so|xyz))\)", re.I)
# Hosts that appear as citations/job boards, never as the account's own domain.
_NOT_COMPANY = {
    "linkedin.com", "twitter.com", "x.com", "github.com", "youtube.com", "medium.com",
    "techcrunch.com", "prnewswire.com", "globenewswire.com", "businesswire.com",
    "substack.com", "stockanalysis.com", "morningstar.com", "fortune.com", "reuters.com",
    "bloomberg.com", "forbes.com", "cnbc.com", "seekingalpha.com", "marketscreener.com",
    "marketbeat.com", "icims.com", "greenhouse.io", "lever.co", "ashbyhq.com",
    "jobs-radar.com", "repvue.com", "glassdoor.com", "indeed.com", "crunchbase.com",
    "siliconangle.com", "venturebeat.com", "theinformation.com", "wsj.com",
}


def _registrable(host):
    """news.datadoghq.com -> datadoghq.com (good enough for these TLDs)."""
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def brief_domain(md, company=""):
    """The account's own web domain, for Apollo matching. '' when not inferable.

    Prefers an explicitly parenthesised domain, then the most-cited host whose
    name actually corresponds to the company — never a guess like
    `<slug>.com`, because a wrong domain silently produces a wrong Apollo match.
    """
    for m in PAREN_DOMAIN_RE.finditer(section(md, "Company Overview") or md[:1500]):
        d = m.group(1).lower()
        if _registrable(d) not in _NOT_COMPANY:
            return _registrable(d)

    compact = re.sub(r"[^a-z0-9]", "", normalize_company(company))
    counts = {}
    for m in HOST_RE.finditer(md):
        d = _registrable(m.group(1))
        if d in _NOT_COMPANY:
            continue
        counts[d] = counts.get(d, 0) + 1
    if not compact:
        return ""
    best, best_n = "", 0
    for d, n in counts.items():
        sld = d.split(".")[0]
        if sld.startswith(compact) or compact.startswith(sld):
            if n > best_n:
                best, best_n = d, n
    return best


def parse_brief(path, today, email_map):
    md = path.read_text(encoding="utf-8")
    m = re.search(
        r"^#\s+(?:(?:Signal Stacking|Prospecting)\s+Brief\s*—\s*)?"
        r"(.+?)(?:\s*—\s*(?:Signal Stacking|Prospecting)\s+Brief)?\s*$", md, re.M)
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
    legacy = not re.search(r"Tier:", md)  # no Tier line at all -> never scored

    m = re.search(r"Fit:\s*(\w+)", md)
    fit = m.group(1) if m else ""
    m = re.search(r"Timing:\s*(-?\d+)\s*pts", md)
    timing = int(m.group(1)) if m else None

    # Optional `· Entry: <Name>` in the metadata line (new-format briefs).
    m = re.search(r"·\s*Entry:\s*([^·\n]+)", md)
    entry_meta = m.group(1).strip() if m else ""

    why_now = strip_md(first_line(section(md, "Why now")))
    if not why_now:  # legacy brief: fall back to the angle's first sentence
        angle = section(md, "Suggested Outreach Angle", "Stacked Angles")
        why_now = strip_md(first_line(angle))[:220]

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
    if legacy:
        flags.append("legacy format — re-run to score")
    elif tier == "—":
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
        "tierLabel": LEGACY_LABEL if legacy else TIER_LABEL[tier],
        "legacy": legacy,
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
        "contacts": brief_contacts(md, entry_meta),
        "domain": brief_domain(md, company),
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


def load_domain_map():
    """normalized company -> domain, from any accounts CSV that names both.

    User-supplied data outranks anything inferred from a brief's citation links.
    """
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
        dom = next((cols[c] for c in cols if "domain" in c or "website" in c), None)
        if not comp or not dom:
            continue
        for r in rows:
            c, d = (r.get(comp) or "").strip(), (r.get(dom) or "").strip().lower()
            d = re.sub(r"^https?://(?:www\.)?", "", d).split("/")[0]
            if c and d:
                out.setdefault(normalize_company(c), d)
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
    domain_map = load_domain_map()
    briefs = []
    for p in sorted(RESEARCH.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            b = parse_brief(p, today, email_map)
            b["domain"] = domain_map.get(normalize_company(b["company"])) or b["domain"]
            briefs.append(b)
        except Exception as e:  # never let one bad brief kill the caller
            print(f"  ! skipped {p.name}: {e}")
    briefs.sort(key=lambda b: (TIER_ORDER[b["tier"]], -(b["timing"] or -99), b["company"].lower()))
    return briefs
