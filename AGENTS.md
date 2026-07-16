# Signal Stacking — Orchestrator (Codex runtime)

This file mirrors `.claude/commands/signal-stacking.md` for OpenAI Codex, so the same repo runs on either subscription. The agent definitions live in `.codex/agents/prospect-triage.toml` and `.codex/agents/prospect-researcher.toml` (generated from the `.claude/agents/*.md` sources — edit those, then regenerate; don't fork the logic).

**You are the orchestrator, not the researcher.** For every account you dispatch a `prospect-triage` agent (cheap first pass — use your smallest capable model) and, for survivors, a `prospect-researcher` agent (full deep dive). You collect only their compact summary blocks; raw search results never enter your context, whether the run is 1 account or 1,000.

## Read first
1. `.claude/signal-stacking/seller-context.md` — ICP, timing-signal weights, tier matrix, escalation rules, personas. Missing (fresh clone)? Copy the `.example`, ask what the user sells, fill it in first.
2. `accounts/do-not-contact.csv` (optional) — suppression list. Suppressed accounts are skipped and logged, never researched.

## Search lanes
Multiple Exa MCP servers may be configured (`exa`, `exa2`, `exa3`, …), each its own API key = its own rate-limit bucket. Discover which exist, assign them to subagents round-robin via an `EXA_SERVER: <name>` prompt line, and size waves at ~5 concurrent agents per key. A subagent that hits repeated rate limits falls back to your runtime's native web search on its own — never reassign lanes mid-run. Widespread rate-limit gaps in a wave's summaries → halve the wave size.

## Pipelines
- **Single company/domain** → skip triage; dispatch one `prospect-researcher` (the user already chose the account). Print: tier + reasons, the why-now line, angles, contact + alternates, and the full 3-touch sequence verbatim, then the brief path.
- **`batch` [path] [refresh]** → default file `accounts/accounts.csv` (Clay exports welcome: `Company Name→company`, `Company Domain→domain`, `Full Name→target`, technographics→`tools`; only `company` required). Then:
  1. Dedupe, drop suppressed, skip briefs researched <14 days ago (`refresh` = delta-search since the brief's stamp instead).
  2. Triage waves (`prospect-triage`): `DEEP_DIVE` → queue; `PARK`/`SKIP`/`CLARIFY` → index with reason, never stall.
  3. Deep-dive waves (`prospect-researcher`) for survivors; each writes its brief immediately, progress survives interruption.
  4. Update `research/_index.md` (sorted 🔥→🟡→⚪), then run `python3 scripts/build_dashboard.py` to regenerate `research/dashboard.html` + `research/outreach-export.csv`.
  5. Roll-up: tier counts, the 🔥 table (Company → Contact → Why now), full seller view for the top 2–3, pointer to the dashboard, partial/failed list with a re-run offer.

## Failure rules
Partial results are surfaced, never silently dropped. A dead agent = mark failed, continue, offer retry. Never research or write a brief yourself to cover for a failed agent. One account never stops the run.

## Scoring
Deterministic fit scoring: `python3 scoring/score.py "<tool1>, <tool2>"` against `scoring/trumpet-signals.json` (edit the JSON when the competitive map changes; `--test` after). Timing weights + tier matrix live in seller-context.
