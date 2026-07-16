#!/usr/bin/env python3
"""Deterministic technographic in-market scorer for Signal Stacking.

Zero-token scoring: the research agents DETECT which tools an account uses
(Clay technographics column, job postings, case studies); this script COMPUTES
the score identically every time.

Usage:
    python3 scoring/score.py "Gong, Salesloft, hubspot.com, GetAccept"
    python3 scoring/score.py --json '["gong.io", "Slack", "PandaDoc"]'
    python3 scoring/score.py --test

Output: JSON on stdout (see scoreAccount docstring for fields).

Matching: domain-first, then exact (case-insensitive) name equality — never
substring, so common-word names like "Clay", "Journey", "Arcade", "Close"
can't false-positive. Tools on the ignore list are excluded entirely.
"""

import json
import re
import sys
from pathlib import Path

SIGNALS_PATH = Path(__file__).parent / "trumpet-signals.json"

# Tooling (fit) can raise the ceiling but never mint a hot tier alone:
# cap its contribution so a 12-tool stack can't outrank a real buying trigger.
TOOLING_CAP = 4
DENSITY_BONUS_THRESHOLD = 3  # 3+ matched technologies → +1 (tool-buying culture)


def _norm(s):
    return s.strip().lower()


def _norm_domain(s):
    """Normalize a tool string to a bare domain if it looks like one."""
    s = _norm(s)
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/")[0].split("?")[0]
    return s


def _load_signals():
    with open(SIGNALS_PATH) as f:
        return json.load(f)


def score_account(tools, signals=None):
    """Score an account's tool stack against the Trumpet signal data.

    Args:
        tools: list of tool names and/or domains the account uses.
        signals: parsed trumpet-signals.json (loaded from disk if None).

    Returns dict:
        competitorScore     sum of matched competitor points
        toolingScore        sum of matched technology points + density bonus
        toolingScoreCapped  min(toolingScore, TOOLING_CAP) — use for tiering
        fitScore            competitorScore + toolingScoreCapped
        fitBand             high (>=4 or switch play) | medium (2-3) | low (0-1)
        isSwitchPlay        True ONLY if a pure_dsr competitor matched
        needsRecencyCheck   True when isSwitchPlay — agent must check adoption
                            date before outreach (fresh adoption → suppress)
        matchedCompetitors  [{name, domain, bucket, points, flames, play}]
        matchedTools        [{name, domain, points, flames}]
        plays               ordered, deduped bucket plays for the email stage
        ignored             input tools excluded as near-universal
        unrecognized        input tools that matched nothing (grow the data file)
    """
    signals = signals or _load_signals()
    ignore = {_norm(t) for t in signals.get("ignore", [])}
    play_text = signals.get("plays", {})

    comp_by_domain = {c["domain"].lower(): c for c in signals["competitors"]}
    comp_by_name = {_norm(c["name"]): c for c in signals["competitors"]}
    tech_by_domain = {t["domain"].lower(): t for t in signals["technologies"]}
    tech_by_name = {_norm(t["name"]): t for t in signals["technologies"]}

    matched_comps, matched_tools = {}, {}
    ignored, unrecognized = [], []

    for raw in tools:
        if not raw or not str(raw).strip():
            continue
        raw = str(raw)
        name_key, domain_key = _norm(raw), _norm_domain(raw)

        if name_key in ignore or domain_key in ignore:
            ignored.append(raw.strip())
            continue

        # Domain first, then exact name equality.
        hit = comp_by_domain.get(domain_key) or comp_by_name.get(name_key)
        if hit:
            matched_comps[hit["domain"]] = hit
            continue
        hit = tech_by_domain.get(domain_key) or tech_by_name.get(name_key)
        if hit:
            matched_tools[hit["domain"]] = hit
            continue
        unrecognized.append(raw.strip())

    competitor_score = sum(c["points"] for c in matched_comps.values())
    tooling_score = sum(t["points"] for t in matched_tools.values())
    if len(matched_tools) >= DENSITY_BONUS_THRESHOLD:
        tooling_score += 1  # density bonus: a GTM org that buys tools

    tooling_capped = min(tooling_score, TOOLING_CAP)
    fit_score = competitor_score + tooling_capped

    # Displacement is a different motion — pure_dsr ONLY. An enablement suite
    # or proposal tool is not a DSR; flagging those as "switch" would point
    # AEs at rip-and-replace emails for accounts that don't run a DSR at all.
    is_switch = any(c["bucket"] == "pure_dsr" for c in matched_comps.values())

    fit_band = (
        "high" if is_switch or fit_score >= 4
        else "medium" if fit_score >= 2
        else "low"
    )

    def flames(points):
        return "🔥" * points

    plays = []
    for bucket in ("pure_dsr", "enablement_suite", "doc_proposal_adjacent"):
        if any(c["bucket"] == bucket for c in matched_comps.values()):
            plays.append({"bucket": bucket, "guidance": play_text.get(bucket, "")})

    return {
        "competitorScore": competitor_score,
        "toolingScore": tooling_score,
        "toolingScoreCapped": tooling_capped,
        "fitScore": fit_score,
        "fitBand": fit_band,
        "isSwitchPlay": is_switch,
        "needsRecencyCheck": is_switch,
        "matchedCompetitors": [
            {**c, "flames": flames(c["points"]), "play": play_text.get(c["bucket"], "")}
            for c in matched_comps.values()
        ],
        "matchedTools": [
            {**t, "flames": flames(t["points"])} for t in matched_tools.values()
        ],
        "plays": plays,
        "ignored": ignored,
        "unrecognized": unrecognized,
    }


def _run_tests():
    sig = _load_signals()
    failures = []

    def check(label, cond):
        if not cond:
            failures.append(label)

    # 1. Switch play fires on pure_dsr only.
    r = score_account(["GetAccept"], sig)
    check("pure_dsr → isSwitchPlay", r["isSwitchPlay"] and r["competitorScore"] == 2)
    r = score_account(["Seismic", "PandaDoc"], sig)
    check("suite/doc competitors → NOT switch play", not r["isSwitchPlay"])
    check("suite/doc competitors still score", r["competitorScore"] == 2)

    # 2. Domain and name matching, case-insensitive, URL forms.
    r = score_account(["https://www.gong.io/", "SALESLOFT", "hubspot.com"], sig)
    check("domain/URL/name matching", len(r["matchedTools"]) == 3)

    # 3. Density bonus: 3 matched techs → +1.
    check("density bonus", r["toolingScore"] == 2 + 2 + 1 + 1)

    # 4. Tooling cap: huge stack can't exceed cap toward fit.
    big = ["Gong", "Chorus", "Salesloft", "Outreach", "Consensus", "Navattic"]
    r = score_account(big, sig)
    check("tooling cap", r["toolingScoreCapped"] == TOOLING_CAP)
    check("raw tooling preserved", r["toolingScore"] > TOOLING_CAP)

    # 5. Ignore list contributes nothing.
    r = score_account(["Slack", "Calendly", "Figma"], sig)
    check("ignore list", r["fitScore"] == 0 and len(r["ignored"]) == 3)

    # 6. No substring false positives ("Clayton Homes" ≠ Clay).
    r = score_account(["Clayton Homes", "Arcade Fire"], sig)
    check("no substring matches", not r["matchedTools"] and len(r["unrecognized"]) == 2)

    # 7. Dedupe: same tool by name and domain counts once.
    r = score_account(["Gong", "gong.io"], sig)
    check("dedupe name+domain", r["toolingScore"] == 2)

    # 8. Fit bands.
    check("band high (switch)", score_account(["Dock"], sig)["fitBand"] == "high")
    check("band medium", score_account(["HubSpot", "Loom"], sig)["fitBand"] == "medium")
    check("band low", score_account(["HubSpot"], sig)["fitBand"] == "low")

    # 9. Recency check flag rides with switch play.
    check("recency check flag", score_account(["Aligned"], sig)["needsRecencyCheck"])

    if failures:
        print("FAIL:\n  " + "\n  ".join(failures))
        return 1
    print(f"OK — all 9 test groups passed ({len(sig['competitors'])} competitors, "
          f"{len(sig['technologies'])} technologies, {len(sig['ignore'])} ignored)")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    if args[0] == "--test":
        sys.exit(_run_tests())
    if args[0] == "--json":
        tools = json.loads(args[1])
    else:
        tools = [t for t in re.split(r"[,\n]", " ".join(args)) if t.strip()]
    print(json.dumps(score_account(tools), indent=2, ensure_ascii=False))
