#!/usr/bin/env python3
"""Token-cost estimation for Signal Stacking runs.

Estimates the token cost of a /signal-stacking run BEFORE launch so users on a
limited token budget can decide whether to fire it. All tunable numbers live in
scripts/cost-model.json (see the _comment fields there for how each estimate
was derived from .claude/agents/prospect-triage.md, prospect-researcher.md and
.claude/commands/signal-stacking.md); this module is pure arithmetic.

Cost model of a batch run:
  orchestrator base + per-account overhead        (main model)
+ one prospect-triage per NEW or STALE account    (Haiku, max 3 searches, tiny)
+ one prospect-researcher per SURVIVING account   (main model, ~10-15 searches
                                                   whose results dominate cost)
Accounts with a brief fresher than 14 days ("fresh") are skipped free unless
--refresh, in which case they (and stale accounts) are re-searched at the
refresh multiplier (~60% of a full deep dive, delta-only searching).
Single-account runs skip triage entirely (the user already chose the account)
and always run exactly one deep dive per account.

Uncertainty: the triage survival rate (fraction of triaged accounts verdicted
DEEP_DIVE) is unknown before the run, so estimates are a {low, expected, high}
band: low = every triaged account parks after triage (0 deep dives),
high = every triaged account survives (all deep dives), expected = the
survival-rate-weighted middle. Single mode has no triage, so low == high.

Integration API (for the local server)
--------------------------------------
The server's POST /api/estimate endpoint should call:

    from costlib import estimate_run
    result = estimate_run(n_new, n_stale, n_fresh,
                          refresh=body.get("refresh", False),
                          single=body.get("single", False),
                          survival=body.get("survival"))   # None -> default
    return json_response(result)                            # fully JSON-safe

and the UI shows result["summary"] (a one-line human string) in the
run-confirmation dialog, with result["tokens"] / result["usd"] available for a
richer breakdown. The $ figures are Claude API LIST-PRICE references only -
subscription users should read them as relative cost, not a bill.

CLI
---
    python3 scripts/costlib.py <n_new> [n_stale] [n_fresh] [--refresh]
                               [--single] [--survival 0.5] [--selftest]
"""

import json
import os
import sys

_DEFAULT_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "cost-model.json")


def load_model(path=None):
    """Load the tunable cost model. Defaults to cost-model.json next to this file."""
    with open(path or _DEFAULT_MODEL_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _fmt_tokens(n):
    """1234567 -> '1.2M', 45300 -> '45k', 900 -> '900'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(int(n))


def _usd(haiku_in, haiku_out, main_in, main_out, prices):
    h, m = prices["haiku"], prices["main"]
    return (haiku_in * h["input"] + haiku_out * h["output"]
            + main_in * m["input"] + main_out * m["output"]) / 1_000_000.0


def estimate_run(n_new, n_stale, n_fresh, refresh, single=False,
                 survival=None, model=None):
    """Estimate token + reference-$ cost of a Signal Stacking run.

    Args:
        n_new:    accounts with no existing brief.
        n_stale:  accounts whose brief is older than the freshness window (14d).
        n_fresh:  accounts with a fresh brief (skipped free unless refresh).
        refresh:  True for a `refresh` run - fresh accounts are included and
                  all re-researched accounts cost only the refresh multiplier.
        single:   single-account pipeline - triage is skipped (user chose the
                  account) and every account gets exactly one deep dive.
        survival: fraction of triaged accounts expected to survive triage
                  (verdict DEEP_DIVE). None -> model default (~0.5). Ignored
                  in single mode.
        model:    a dict from load_model(); None -> load the default.

    Returns a JSON-safe dict: counts breakdown, expected deep dives, token
    estimate {low, expected, high}, reference-$ range, and a one-line summary.
    """
    if model is None:
        model = load_model()
    for name, v in (("n_new", n_new), ("n_stale", n_stale), ("n_fresh", n_fresh)):
        if v < 0:
            raise ValueError(f"{name} must be >= 0, got {v}")

    tri = model["triage"]
    dive = model["deep_dive"]
    mult = model["refresh_multiplier"]["value"]
    orch = model["orchestrator"]
    prices = model["pricing_usd_per_mtok"]
    if survival is None:
        survival = model["survival_rate_default"]["value"]
    survival = max(0.0, min(1.0, float(survival)))

    n_total = n_new + n_stale + n_fresh

    # Per-account deep-dive cost multipliers ("full-dive equivalents").
    # stale: full re-research unless refresh (then delta-only);
    # fresh: free unless refresh (then delta-only).
    stale_factor = mult if refresh else 1.0
    fresh_factor = mult if refresh else 0.0

    if single:
        # No triage; every account gets exactly one deep dive (fresh/stale
        # accounts still benefit from delta searching on a refresh run).
        n_triaged = 0
        dive_units = (n_new * 1.0 + n_stale * stale_factor
                      + (n_fresh * (mult if refresh else 1.0)))
        dive_counts = {"low": float(n_total), "expected": float(n_total),
                       "high": float(n_total)}
        unit_scale = {"low": 1.0, "expected": 1.0, "high": 1.0}
    else:
        n_triaged = n_new + n_stale + (n_fresh if refresh else 0)
        dive_units = (n_new * 1.0 + n_stale * stale_factor
                      + n_fresh * fresh_factor)
        dive_counts = {"low": 0.0,
                       "expected": round(survival * n_triaged, 1),
                       "high": float(n_triaged)}
        unit_scale = {"low": 0.0, "expected": survival, "high": 1.0}

    # Fixed (survival-independent) tokens.
    haiku_in = n_triaged * tri["input_tokens"]
    haiku_out = n_triaged * tri["output_tokens"]
    orch_in = orch["base_input_tokens"] + n_total * orch["per_account_input_tokens"]
    orch_out = orch["base_output_tokens"] + n_total * orch["per_account_output_tokens"]

    tokens, usd = {}, {}
    for k, s in unit_scale.items():
        d_in = dive_units * s * dive["input_tokens"]
        d_out = dive_units * s * dive["output_tokens"]
        main_in = orch_in + d_in
        main_out = orch_out + d_out
        tokens[k] = int(round(haiku_in + haiku_out + main_in + main_out))
        usd[k] = round(_usd(haiku_in, haiku_out, main_in, main_out, prices), 2)

    exp_dives = dive_counts["expected"]
    if single:
        dive_word = "deep dive" if n_total == 1 else "deep dives"
        tail = f"{n_total} {dive_word} (triage skipped)"
        band = f"≈ {_fmt_tokens(tokens['expected'])} tokens expected"
    else:
        exp_dives_str = (f"{exp_dives:g}" if exp_dives == int(exp_dives)
                         else f"{exp_dives:.1f}")
        tail = f"{n_triaged} triage + ~{exp_dives_str} deep dives"
        if n_fresh and not refresh:
            tail += f" · {n_fresh} fresh skipped free"
        band = (f"≈ {_fmt_tokens(tokens['expected'])} tokens expected "
                f"({_fmt_tokens(tokens['low'])}–{_fmt_tokens(tokens['high'])})")
    summary = (f"{band} · ~${usd['expected']:.2f} at API list prices "
               f"· {tail}")

    return {
        "mode": "single" if single else "batch",
        "refresh": bool(refresh),
        "counts": {
            "new": n_new,
            "stale": n_stale,
            "fresh": n_fresh,
            "total": n_total,
            "triaged": n_triaged,
            "skipped_free": 0 if (refresh or single) else n_fresh,
        },
        "survival": None if single else survival,
        "deep_dives": dive_counts,
        "expected_deep_dives": exp_dives,
        "tokens": tokens,
        "tokens_by_model_expected": {
            "haiku": int(haiku_in + haiku_out),
            "main": int(tokens["expected"] - haiku_in - haiku_out),
        },
        "usd": usd,
        "pricing_note": ("$ figures use Claude API list prices "
                         "(reference only - subscription users see this as "
                         "relative cost, not a bill)"),
        "summary": summary,
    }


def _selftest():
    m = load_model()

    def tok(**kw):
        kw.setdefault("refresh", False)
        return estimate_run(model=m, **kw)

    # 1. Monotonic in account count.
    a = tok(n_new=10, n_stale=0, n_fresh=0)
    b = tok(n_new=20, n_stale=0, n_fresh=0)
    c = tok(n_new=40, n_stale=0, n_fresh=0)
    assert a["tokens"]["expected"] < b["tokens"]["expected"] < c["tokens"]["expected"], \
        "expected tokens must increase with account count"
    assert a["tokens"]["high"] < c["tokens"]["high"]

    # 2. Fresh accounts are ~free on non-refresh runs (only tiny orchestrator
    #    per-account overhead; no triage, no deep dives).
    base = tok(n_new=0, n_stale=0, n_fresh=0)
    fresh = tok(n_new=0, n_stale=0, n_fresh=10)
    assert fresh["expected_deep_dives"] == 0
    assert fresh["counts"]["triaged"] == 0
    per_fresh = (fresh["tokens"]["expected"] - base["tokens"]["expected"]) / 10
    assert per_fresh < 5000, f"fresh account should cost ~0, got {per_fresh} tokens"

    # 3. Band ordering: low <= expected <= high, strict when triage uncertainty exists.
    assert a["tokens"]["low"] < a["tokens"]["expected"] < a["tokens"]["high"]
    assert a["usd"]["low"] < a["usd"]["expected"] < a["usd"]["high"]

    # 4. Survival endpoints match the band edges.
    s0 = tok(n_new=10, n_stale=0, n_fresh=0, survival=0.0)
    s1 = tok(n_new=10, n_stale=0, n_fresh=0, survival=1.0)
    assert s0["tokens"]["expected"] == a["tokens"]["low"]
    assert s1["tokens"]["expected"] == a["tokens"]["high"]

    # 5. Single mode: no triage, guaranteed deep dive, degenerate band.
    single = tok(n_new=1, n_stale=0, n_fresh=0, single=True)
    batch1 = tok(n_new=1, n_stale=0, n_fresh=0)
    assert single["tokens_by_model_expected"]["haiku"] == 0, "single must skip triage"
    assert single["tokens"]["low"] == single["tokens"]["expected"] == single["tokens"]["high"]
    # Single is a guaranteed full dive: more than batch-of-1's expected
    # (which is triage + survival-weighted dive) but less than batch-of-1's
    # high (triage + full dive).
    assert single["tokens"]["expected"] > batch1["tokens"]["low"]
    assert single["tokens"]["expected"] < batch1["tokens"]["high"]

    # 6. Refresh is cheaper than full re-research for stale accounts.
    stale_full = tok(n_new=0, n_stale=10, n_fresh=0, refresh=False)
    stale_refresh = tok(n_new=0, n_stale=10, n_fresh=0, refresh=True)
    assert stale_refresh["tokens"]["expected"] < stale_full["tokens"]["expected"]

    # 7. Refresh pulls fresh accounts back into the run (they stop being free).
    fresh_refresh = tok(n_new=0, n_stale=0, n_fresh=10, refresh=True)
    assert fresh_refresh["tokens"]["expected"] > fresh["tokens"]["expected"]
    assert fresh_refresh["counts"]["triaged"] == 10

    # 8. JSON safety.
    json.dumps(c)

    print("selftest: all 8 checks passed")
    print("example 40-account batch:", c["summary"])
    return 0


def _cli(argv):
    if "--selftest" in argv:
        return _selftest()

    flags = {"refresh": "--refresh" in argv, "single": "--single" in argv}
    survival = None
    if "--survival" in argv:
        survival = float(argv[argv.index("--survival") + 1])
    pos = []
    skip = False
    for i, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == "--survival":
            skip = True
        elif not arg.startswith("-"):
            pos.append(int(arg))

    if not pos:
        print(__doc__.split("CLI\n---\n")[1].strip())
        return 1
    n_new = pos[0]
    n_stale = pos[1] if len(pos) > 1 else 0
    n_fresh = pos[2] if len(pos) > 2 else 0

    result = estimate_run(n_new, n_stale, n_fresh,
                          refresh=flags["refresh"], single=flags["single"],
                          survival=survival)
    print(result["summary"])
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
