# Signal Stacking

An in-market scoring + investigative prospecting agent for [Claude Code](https://claude.com/claude-code) (and OpenAI Codex), powered by the **Exa MCP**. Give it a company — or a list of hundreds — and it tells you **who is in-market to buy right now**, why, who to contact, and hands your AEs a ready-to-send 3-touch sequence. Built for selling **[Trumpet](https://sendtrumpet.com)** today; the seller is swappable in one file.

Core belief: one signal is a guess; a **stack** is a thesis. Where a company hires, what it deploys, and what its execs publicly commit to reveals what it's buying next.

## Quick start

**Signal Scout (the app) — one command:**

```bash
./scout
```

Starts the server (reusing one already running), verifies every search lane with
a real search, confirms the do-not-contact list actually loaded, checks the
Apollo key, and opens the app. It prints one line per check and ends in either
`READY` or `ATTENTION` — so "is this safe to send from?" is answered before you
look at the UI, not after.

```
[  ok  ] search lanes          2/2 verified by real search · waves of 10
[  ok  ] do-not-contact list   934 companies · 2 of your accounts blocked
[  ok  ] apollo enrichment     key valid · 9 cached · emails only (no tunnel)
READY → http://127.0.0.1:8765
```

`./scout status` re-runs the checks without starting anything; `./scout stop`
shuts the server down. A failed check exits non-zero, so it composes with other
tooling.

Type a company for a one-off scout, drop in an accounts CSV (Clay exports work
unchanged), attach signal sources (closed-lost CRM export, product usage data),
select accounts, and hit Run. **Every run shows a token-cost estimate before it
launches** (tunables in `scripts/cost-model.json`) — so a limited token budget
gets spent on the accounts that matter. The page live-updates as briefs land.
Runs execute headlessly via `claude -p`; everything stays on your machine.

**Or straight from Claude Code:**

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
| `accounts/closed-lost.csv` | Closed-lost CRM export → re-engagement signals + angles (upload in Scout or copy from `.example`; gitignored) | after each pipeline review |
| `scripts/cost-model.json` | Per-run token-cost estimates shown before every launch | actuals drift from estimates |
| `.claude/signal-stacking/trumpet-usage-data.md` | Optional product-usage data for proof lines | you have signup data (gitignored) |
| `.env` | `APOLLO_API_KEY` (+ optional `APOLLO_WEBHOOK_BASE`) for contact enrichment — copy from `.env.example` (gitignored) | key rotates, or you start a tunnel |
| `.claude/commands/signal-stacking.md` | Orchestration | rarely |
| `.claude/agents/*.md` | Triage + deep-dive methodology, email rules | rarely |

## Contact enrichment (Apollo)

Briefs name the buying committee; Apollo turns those names into reachable
contacts. Open an account in Scout and hit **Enrich with Apollo** — every
person the brief names (entry point, economic buyer, influencers, champions,
exec sponsor) is matched and their work email filled in. Results land in
`research/contacts-export.csv`, one row per person.

Setup is one line — put your **master** API key in `.env`:

```bash
cp .env.example .env      # then paste your key into APOLLO_API_KEY
```

Standard Apollo keys are rejected by the enrichment endpoints; you need a
master key from Settings → Integrations → API.

**Mobile numbers need a public webhook.** This is an Apollo constraint, not a
choice: `reveal_phone_number` is refused unless you also pass a `webhook_url`,
and the numbers are delivered there asynchronously minutes later. Scout serves
that endpoint at `/api/apollo/webhook`, but it binds to 127.0.0.1, so Apollo
cannot reach it until you expose it:

```bash
cloudflared tunnel --url http://127.0.0.1:8765        # prints an https URL
echo 'APOLLO_WEBHOOK_BASE=https://<that-url>' >> .env  # then restart Scout
```

Without that, enrichment still runs and returns emails — the UI says
"emails only · no tunnel for mobiles" rather than implying nobody has a phone.
The webhook URL carries a generated `APOLLO_WEBHOOK_TOKEN`; requests without it
are rejected, since a public tunnel would otherwise let anyone inject
fabricated numbers into your cache.

**Credits are real money.** Roughly 1 credit per email, plus ~8 more when a
mobile is actually found, so a nine-person committee with phones can cost ~80.
Scout shows the credit range and asks before spending, and every result is
cached in `accounts/apollo-cache.json` — re-enriching an account you already
pulled costs nothing.

## Evidence before draft

Each account's detail panel leads with **What it found** — every sourced claim
the research produced, newest first, each with a kind chip (signal / hiring /
stack / filing / closed-lost) and clickable source links — and only then shows
**The draft**. A claim with no citation is labelled `no source cited` rather
than sitting silently next to cited ones.

The ordering is the point: the copy is only as trustworthy as the evidence
behind it, so the evidence is what you read first and what you check when a
line in the email looks too good.

Fit and timing render as separate chips rather than one blended number, since
they answer different questions ("are they the kind of company that buys?" vs
"are they buying now?"). Timing shows a percentile against your own book only
once there are at least 8 scored accounts to rank against — below that a
percentile is noise dressed as precision.

## Hiding an account from the app entirely

`accounts/hidden.csv` (`company,domain,reason`) removes an account from the UI,
the dashboard and every export — for live deals too sensitive to appear in a
screenshot or a shared link. This is distinct from suppression: **do-not-contact
blocks outreach but still shows the account so you can see why; hidden means
gone.** The research file stays on disk untouched; delete the row to bring it
back. `./scout` reports the count so you always know something is being withheld.

## Suppression (do-not-contact)

`accounts/do-not-contact.csv` (`company,domain,reason,until`) lists current
customers, open opportunities and cooloffs. Both halves matter: closed-won
accounts you would recognise by name, and **open opportunities you would not** —
an AE mid-deal is invisible from outside the CRM. **The app enforces it**, not just the
orchestrator prompt: a run naming a suppressed account is refused with 409 before
a token is spent, suppressed rows are excluded from bulk selection, and an
`until` date in the past stops suppressing (an expired cooloff).

Both sides of the match generate the same candidate keys (`brieflib.suppression_keys`)
— bare domain, normalized name, and the domain's second-level label — and the
check runs *after* URL normalization. Each of those is load-bearing: a row
holding a bare name (`Acme Co`) has to match an input arriving as a URL
(`https://www.acme-co.com/fleet`), and `normalize_company` strips legal suffixes
but not TLDs, so `acme-co.com` and `Acme Co` do not otherwise converge.

This matters more than it looks. The FIT score is computed from technographics,
so your own customers — high-velocity revtech with big AE teams — are exactly
the accounts the scorer ranks 🔥. Worse, the Touch-1 copy cites public customers
as social proof, so an unsuppressed customer would be sent an email citing
*itself* as the reason to buy.

## Running any company

Three ways in, all equivalent:

- **Type a name** — `Stripe`
- **Paste a website** — `https://www.stripe.com/pricing`, `stripe.com`, `www.Ramp.com/careers` all resolve to the bare domain
- **Drop a CSV** — Clay exports work unchanged; any file with a company column does

Comma-separate for several (`Stripe, notion.so, https://ramp.com`) and it runs as
a batch. Anything you run is appended to `accounts/accounts.csv`, so a one-off
company joins the picker instead of disappearing after the run.

**A dropped CSV adds to your list — it does not replace it.** Replacing is
possible but has to be chosen explicitly in the prompt (or `?replace=1` on the
API), because a curated account list is expensive to rebuild. Accounts join to
briefs by slug, then normalized name, then domain, so a row typed as
`stripe.com` still finds the brief titled *Stripe* rather than offering you a
paid re-run of research you already have.

## Search lanes

Scout probes every configured `exa*` MCP server with a **real search** and shows
`N/M search lanes · waves of N×5` in the header. If a lane is configured but not
searching, the app says so loudly instead of letting runs quietly degrade.

This exists because the failure is otherwise invisible: `claude mcp list` reports
a server with a dead key as **"✔ Connected"**, since the key rides in the URL
query string — the transport handshake succeeds and only the searches 401. The
orchestrator builds its lane list from servers that *respond*, so a dead key
still counts as a lane: it widens the waves and sends half the accounts down a
lane that silently falls back to plain WebSearch. **Never trust `claude mcp list`
to tell you a key works.**

Exa's default limit is 10 QPS per key. A deep-dive researcher averages ~0.06
searches/sec, so the binding moment is wave start, when every agent fires its
first search at once — hence ~5 concurrent subagents per key.

## Brief formats and scoring

Briefs written before tier scoring existed carry no `Tier:` line, and no
technographic data survives to score them offline. Those are labelled
**"Legacy — never scored"** rather than "untiered", which would wrongly imply
the scorer looked at them and found nothing. Each one has a **Re-run this
account** button that puts it back through the pipeline for a real tier.
Enrichment works on legacy briefs regardless — they still name contacts.

## What's not committed

`research/` (briefs, dashboard, exports — real contacts and targeting), `accounts/*.csv` except the examples, `seller-context.md`, and usage data are all gitignored. Only the tool, the scoring data, templates, and sanitized examples are tracked.

## Roadmap

- **Slack trigger:** product signups / website visitors post `{company or domain, person, trigger}` to a webhook → single-account pipeline (person = champion, trigger = the timely context) → brief + sequence back in the channel. The input seam already accepts domains for exactly this.
- **CRM writeback:** push tier + why-now onto the account record so the dashboard and the CRM never disagree.
