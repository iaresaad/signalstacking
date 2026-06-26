# Signal Stacking

An investigative sales-prospecting agent for [Claude Code](https://claude.com/claude-code), powered by the **Exa MCP**. Give it a company (or a list of hundreds), and it researches each one, **stacks their signals** (business events + hiring/growth + exec priorities + named people) into reusable outreach angles, and drafts a problem-first cold email. Built for selling **Trumpet** today; the seller is swappable.

## Quick start

```bash
# one company
/signal-stacking Vanta

# a domain (the shape a future Slack/signup trigger will use)
/signal-stacking vanta.com

# bulk — reads accounts/accounts.csv (or .txt) and writes a brief per company
/signal-stacking batch
```

Each run writes a brief to `research/<company>.md` and, in bulk mode, an index to `research/_index.md`. Results in chat always surface the **signals and angles used** so a seller can reuse them even if they rewrite the email.

## Bulk input

Drop your target accounts into `accounts/accounts.csv` (gitignored). See `accounts/accounts.example.csv`:

```csv
company,domain,target,notes
Ramp,ramp.com,,VP Sales — no public CRO
Vanta,vanta.com,Stevie Case,CRO
```

Only `company` is required. Bulk mode is **resume-safe**: it skips accounts already in `research/`, so a 100+ run can be restarted after any interruption, and it logs ambiguous/failed accounts in the index instead of stopping.

## How it works

Per company, parallel Exa subagents gather: (A) business signals, (B) hiring/growth triggers, (C) the revenue leader + named champion/buyer/influencer, (D) exec voice + strategic milestone statements. A synthesis step clusters them into 2–3-signal **stacked angles**, then drafts a problem-first email. Every external claim carries a dated source URL; nothing is fabricated.

## Configure

| File | Purpose | Edit when |
|------|---------|-----------|
| `.claude/commands/signal-stacking.md` | The skill | rarely |
| `.claude/signal-stacking/seller-context.md` | What you sell — drives every angle (copy from `seller-context.example.md`; gitignored) | positioning changes |
| `.claude/signal-stacking/trumpet-usage-data.md` | **Optional** product-usage data for the proof line | you have sign-up data (gitignored) |
| `accounts/accounts.csv` | Your target-account list for bulk runs | per campaign (gitignored) |

## Example output

See [`examples/sample-brief.md`](examples/sample-brief.md) for a full brief + email on a fictional company, so you can see the output format without any real prospect data.

## What's not committed

`research/` (generated briefs with real contacts), `accounts/accounts.csv` (your target list), `seller-context.md` (your real positioning), and `trumpet-usage-data.md` (private CRM data) are gitignored. Only the tool, config templates, and examples are tracked.

## Roadmap

Connect to Slack so product sign-ups and website visitors auto-trigger a brief + email: a webhook passes `{company or domain, person, trigger}`, which maps onto the single-company pipeline (the person becomes the champion, the trigger becomes the timely context, and any Trumpet usage data lights up the named-champion proof line).
