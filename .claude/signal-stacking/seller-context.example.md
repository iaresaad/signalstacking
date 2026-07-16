# Seller Context — TEMPLATE (copy to seller-context.md and fill in for YOUR product)

> The Signal Stacking skill reads `seller-context.md` to tailor every angle and email.
> That live file is gitignored (it holds your real positioning). Copy this template to
> `seller-context.md`, fill it in, and edit it whenever your product or positioning changes.
> To repurpose the skill for a different product, you only edit that one file.

## Product
**<Your product>** (<url>) — <one-line category, e.g. "a B2B digital sales room platform">.
<2–3 sentences: what it is and the core mechanic.>

## Who we sell to (ICP)
- **Economic buyer:** <e.g. CRO / VP Sales / Head of Revenue>
- **Influencer:** <e.g. RevOps / Revenue Enablement>
- **Champion / daily user:** <e.g. AEs, SDRs, AMs, CSMs, SEs>
- **Company type:** <e.g. B2B SaaS with complex, multi-stakeholder sales cycles>

## Core value props (what to anchor angles on)
1. <outcome 1, ideally with a metric>
2. <outcome 2>
3. <outcome 3>

## When we're a strong fit (relevance hooks → map these to a prospect's signals)
- <signal/pain 1> → <why we help>
- <signal/pain 2> → <why we help>
- <signal/pain 3> → <why we help>

## Peer social proof (public customers — used as the live fallback when no usage data)
<Customer A>, <Customer B>, <Customer C>. (Pick the closest peer to each prospect.)

## Product mirroring (relate us to the prospect's OWN product)
Always try one analogy: "the way <our product> does X is similar to how <prospect> does <their core thing>."

## In-market scoring — tiering every account (EDIT: replace with your product's signals)

Two axes. **FIT** comes from technographics — edit `scoring/trumpet-signals.json` with YOUR competitors and complementary tools, then agents run `python3 scoring/score.py "<tools>"`. **TIMING** comes from behavioral signals — replace this table with the triggers that mean someone buys YOUR product soon:

| Timing signal | Weight |
|---|---|
| <new exec who owns your budget line, in seat < 6 months> | 3 |
| <exec publicly naming the pain you solve> | 3 |
| <hiring wave in the function you sell into> | 2 |
| <funding round < 6 months> | 2 |
| <negative signal, e.g. layoffs> | −2 |

**Tier matrix** (timing × fit): timing ≥ 5 + fit high/medium → 🔥 In-market now; timing 2–4 → 🟡 Warming; else ⚪ Monitor. Fit-high with no timing stays 🟡 (watch list).

**Escalation rules:** define 2–3 signal interactions that force a tier up or down (e.g. competitor-vulnerable + new exec → force 🔥; competitor adopted <6 months → force ⚪ with a re-check date). Suppression-list hits (`accounts/do-not-contact.csv`) always skip.

## Personas — branch the 3-touch sequence

- **Economic buyer:** <title> — business-outcome language.
- **Influencer:** <title> — workflow-pain language.
- **Champion:** <title> — peer-to-peer language.
