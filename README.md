# Signal Stacking

An in-market scoring + investigative prospecting agent for [Claude Code](https://claude.com/claude-code) (and OpenAI Codex), powered by the **Exa MCP**. Give it a company — or a list of hundreds — and it tells you **who is in-market to buy right now**, why, who to contact, and hands your AEs a ready-to-send 3-touch sequence. Built for selling **[Trumpet](https://sendtrumpet.com)** today; the seller is swappable in one file.

Core belief: one signal is a guess; a **stack** is a thesis. Where a company hires, what it deploys, and what its execs publicly commit to reveals what it's buying next.

## Quick start

```bash
# one company
/signal-stacking Vanta

# a domain (the shape a future Slack/signup trigger will use)
/signal-stacking vanta.com

# bulk — reads accounts/accounts.csv, tiers every account, builds the dashboard
/signal-stacking batch

# re-score a stale list without redoing fresh research
/signal-stacking batch refresh
```

Every batch ends with two AE-facing deliverables (both local, nothing leaves your machine):

- **`research/dashboard.html`** — open in any browser. Tier tiles (🔥 In-market now / 🟡 Warming / ⚪ Monitor), sortable + searchable account table, click a row for the full brief with **one-click copy on every touch**. Built for non-technical teammates: you run the batch, they work the list.
- **`research/outreach-export.csv`** — company, tier, contact (+email when your account list has one), why-now line, all three touches. Imports into Outreach/Salesloft/Instantly or back into Clay.

## How it decides who's in-market

Two axes, computed per account:

- **FIT** (are they the kind of company that buys?) — deterministic technographic scoring, zero tokens: `scoring/score.py` matches the account's tool stack against `scoring/trumpet-signals.json` — DSR competitors (bucketed: pure-DSR = switch play, enablement suites = coexistence play, proposal tools = scope-expansion play), complementary revtech, an ignore list, a stack-density bonus. Domain-first matching, no substring false-positives, capped so tooling alone can't mint a hot tier. `python3 scoring/score.py --test` runs its self-tests.
- **TIMING** (are they buying *now*?) — behavioral signals weighted in `seller-context.md`: new revenue leader in seat, AE/SDR hiring waves, funding, upmarket pushes, exec quotes naming the pain. Escalation rules capture interactions (competitor-vulnerable + new CRO → 🔥; competitor adopted <6 months ago → suppressed with a re-check date).

Every tier is shown **with its arithmetic** in the brief, so a seller can trust it or challenge it.

## Architecture (token- and rate-limit-efficient by design)

```
orchestrator (your context stays clean — it only ever sees compact summaries)
  │
  ├─ wave 1..n: prospect-triage agents      ← cheap model, ≤3 searches each,
  │             (round-robin across every    kills the dead half of the list
  │              configured Exa key)         for ~5% of the tokens
  │
  ├─ wave 1..n: prospect-researcher agents  ← survivors only: 4-lens deep dive,
  │             (same key rotation,          scoring, brief + 3-touch sequence,
  │              WebSearch failover)         written to disk immediately
  │
  └─ scripts/build_dashboard.py             ← dashboard + sequencer export
```

- **N-key Exa rotation:** configure any number of Exa MCP servers (`exa`, `exa2`, `exa3`, …) — each key is its own rate-limit bucket; waves scale ~5 concurrent agents per key. Add a key: `claude mcp add --transport http exa3 "https://mcp.exa.ai/mcp?exaApiKey=YOUR_KEY"`.
- **Provider failover:** an agent that keeps hitting Exa rate limits falls back to the runtime's native web search (Claude's WebSearch on Claude Code, OpenAI's on Codex). Batches degrade to a slower lane instead of stalling.
- **Resume-safe + fresh-aware:** briefs stamp their research date; re-runs skip anything <14 days old, `refresh` searches only the delta. A 500-account run can die and restart without losing a token.
- **Two runtimes:** `.claude/` for Claude Code, `AGENTS.md` + `.codex/` for Codex — same logic, whichever subscription has headroom.

## Example output

**[`examples/briefs/`](examples/briefs/)** — twelve real briefs from a live batch run (Backblaze, OpenAI, Anthropic, Apollo, Demandbase, …), sanitized for publication: real dated signals, real public execs, verbatim sourced quotes, and the outreach angle each stack produced. Also [`examples/sample-brief.md`](examples/sample-brief.md) for the current full format (tier arithmetic + 3-touch sequence) on a fictional company.

## Configure

| File | Purpose | Edit when |
|------|---------|-----------|
| `.claude/signal-stacking/seller-context.md` | What you sell, ICP, timing weights, tier matrix, personas (copy from `.example`; gitignored) | positioning changes |
| `scoring/trumpet-signals.json` | Competitor + technographic fit data | the competitive map changes (`--test` after) |
| `accounts/accounts.csv` | Target list — raw Clay exports work unchanged | per campaign (gitignored) |
| `accounts/do-not-contact.csv` | Suppression: customers, open opps, cooloffs (copy from `.example`; gitignored) | always current |
| `.claude/signal-stacking/trumpet-usage-data.md` | Optional product-usage data for proof lines | you have signup data (gitignored) |
| `.claude/commands/signal-stacking.md` | Orchestration | rarely |
| `.claude/agents/*.md` | Triage + deep-dive methodology, email rules | rarely |

## What's not committed

`research/` (briefs, dashboard, exports — real contacts and targeting), `accounts/*.csv` except the examples, `seller-context.md`, and usage data are all gitignored. Only the tool, the scoring data, templates, and sanitized examples are tracked.

## Roadmap

- **Slack trigger:** product signups / website visitors post `{company or domain, person, trigger}` to a webhook → single-account pipeline (person = champion, trigger = the timely context) → brief + sequence back in the channel. The input seam already accepts domains for exactly this.
- **CRM writeback:** push tier + why-now onto the account record so the dashboard and the CRM never disagree.
