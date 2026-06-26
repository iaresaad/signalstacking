# Signal Stacking — sales prospecting project

Investigative prospecting agent. Researches a company via the Exa MCP, **stacks its signals**
(business + hiring/growth + exec priorities + named people) into tailored outreach angles and
a drafted email, and saves a brief to `research/<company>.md`.

## Run it
```
/signal-stacking <company name>     # single company
/signal-stacking <domain>           # e.g. vanta.com (future Slack/signup shape)
/signal-stacking batch              # reads accounts/accounts.csv, one brief per row
```
Default email target is the economic buyer (CRO/VP Sales); the brief also lists champion +
influencer contacts so you can pick another entry point. Bulk mode is resume-safe: it skips
accounts already in `research/` and logs failures in `research/_index.md`.

## Files
| File | Purpose | Edit when |
|------|---------|-----------|
| `.claude/commands/signal-stacking.md` | The orchestrator skill | rarely |
| `.claude/signal-stacking/seller-context.md` | What you sell — drives every angle (copy from `seller-context.example.md`; gitignored) | positioning/product changes |
| `.claude/signal-stacking/trumpet-usage-data.md` | **Optional** product-usage data for the proof line (copy from `.example`; gitignored) | you have sign-up data for a prospect |
| `accounts/accounts.csv` | Target-account list for bulk runs (copy from `.example`; gitignored) | per campaign |

## Signals-first by design
The skill runs fully on public signals + **peer social proof** and needs no Trumpet data.
When you DO have usage data (who at the prospect already uses Trumpet, from product/CRM),
add a block to `trumpet-usage-data.md` and the proof line upgrades from peer proof to a
named-champion-with-stats line automatically. The skill never fabricates usage stats.

## How it works
4 parallel Exa subagents → (A) business signals, (B) hiring/growth triggers, (C) revenue leader
+ named champion/buyer/influencer, (D) exec voice + strategic milestone statements → synthesis
that clusters findings into 2–3-signal **stacked angles** → 6-line email. Every external claim
carries a dated source URL.
