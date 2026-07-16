#!/usr/bin/env python3
"""Build the AE-facing dashboard + sequencer export from research/*.md briefs.

Run from the repo root (the orchestrator does this automatically):
    python3 scripts/build_dashboard.py

Outputs:
    research/dashboard.html       — open in any browser; sortable, filterable,
                                    copy buttons on every touch. Fully offline.
    research/outreach-export.csv  — one row per account with contact + sequence,
                                    importable into a sequencer or Clay.

Parses both new-format briefs (with Tier/Why-now) and legacy briefs (flagged
"untiered — re-run for scoring"). Briefs older than STALE_DAYS get a
"re-verify contact" flag. If an accounts CSV has email columns, contacts are
joined in by company name for the export.
"""

import csv
import html
import io
import json
import re
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESEARCH = ROOT / "research"
ACCOUNTS = ROOT / "accounts"
STALE_DAYS = 30

TIER_ORDER = {"🔥": 0, "🟡": 1, "⚪": 2, "—": 3}
TIER_LABEL = {"🔥": "In-market now", "🟡": "Warming", "⚪": "Monitor", "—": "Untiered"}


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

    why_now = first_line(section(md, "Why now"))
    if not why_now:  # legacy brief: fall back to the angle's first sentence
        angle = section(md, "Suggested Outreach Angle", "Stacked Angles")
        why_now = first_line(angle)[:220]

    contact = ""
    people = section(md, "Key People", "Target Contact")
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
    touch1 = subsection(seq, "Touch 1") or (seq if seq and "### " not in seq else "")
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
        "flags": flags,
        "touch1": touch1,
        "touch2": touch2,
        "touch3": touch3,
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


def build():
    today = date.today()
    email_map = load_email_map()
    briefs = []
    for p in sorted(RESEARCH.glob("*.md")):
        if p.name.startswith("_"):
            continue
        try:
            briefs.append(parse_brief(p, today, email_map))
        except Exception as e:  # never let one bad brief kill the dashboard
            print(f"  ! skipped {p.name}: {e}")

    briefs.sort(key=lambda b: (TIER_ORDER[b["tier"]], -(b["timing"] or -99), b["company"].lower()))

    # ---- sequencer export ------------------------------------------------
    RESEARCH.mkdir(exist_ok=True)
    export = RESEARCH / "outreach-export.csv"
    with export.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["company", "tier", "contact", "email", "why_now", "switch_play",
                    "touch1_email", "touch2_bump", "touch3_linkedin", "researched", "brief"])
        for b in briefs:
            w.writerow([b["company"], b["tierLabel"], b["contact"], b["email"],
                        b["whyNow"], b["switchPlay"], b["touch1"], b["touch2"],
                        b["touch3"], b["researched"], f"research/{b['file']}"])

    # ---- dashboard -------------------------------------------------------
    counts = {t: sum(1 for b in briefs if b["tier"] == t) for t in TIER_ORDER}
    payload = json.dumps(briefs, ensure_ascii=False).replace("</", "<\\/")
    generated = today.isoformat()

    page = DASHBOARD_TEMPLATE
    for key, val in [
        ("__DATA__", payload), ("__GENERATED__", generated),
        ("__N_HOT__", str(counts["🔥"])), ("__N_WARM__", str(counts["🟡"])),
        ("__N_MON__", str(counts["⚪"])), ("__N_UNT__", str(counts["—"])),
        ("__N_ALL__", str(len(briefs))),
    ]:
        page = page.replace(key, val)
    (RESEARCH / "dashboard.html").write_text(page, encoding="utf-8")

    print(f"dashboard: research/dashboard.html  ({len(briefs)} accounts: "
          f"🔥{counts['🔥']} 🟡{counts['🟡']} ⚪{counts['⚪']} untiered {counts['—']})")
    print(f"export:    research/outreach-export.csv ({len(briefs)} rows, "
          f"{sum(1 for b in briefs if b['email'])} with emails)")


DASHBOARD_TEMPLATE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Signal Stacking — Account Dashboard</title>
<style>
  :root{
    --bg:#faf9f5; --surface:#ffffff; --ink:#1a1915; --ink-2:#5d5a51; --ink-3:#8a8678;
    --line:#e8e5dc; --accent:#c96442; --hot-bg:#fbeee8; --hot-ink:#a4441f;
    --warm-bg:#faf3e0; --warm-ink:#8a6116; --mon-bg:#f0efea; --mon-ink:#6b6858;
    --chip:#f0efea; --hover:#f5f4ee;
  }
  @media (prefers-color-scheme: dark){ :root{
    --bg:#262624; --surface:#30302e; --ink:#f5f4ee; --ink-2:#b8b5a9; --ink-3:#8a8678;
    --line:#3e3d3a; --accent:#e07b53; --hot-bg:#4a2c1e; --hot-ink:#f0a480;
    --warm-bg:#453a1c; --warm-ink:#e5c66e; --mon-bg:#3a3936; --mon-ink:#b8b5a9;
    --chip:#3a3936; --hover:#383734;
  }}
  *{box-sizing:border-box} html{-webkit-text-size-adjust:100%}
  body{margin:0;background:var(--bg);color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
  .wrap{max-width:1200px;margin:0 auto;padding:28px 20px 80px}
  header h1{font-size:22px;margin:0 0 2px;letter-spacing:-.01em}
  header p{margin:0;color:var(--ink-3);font-size:13px}
  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:20px 0}
  .tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;cursor:pointer}
  .tile.active{outline:2px solid var(--accent);outline-offset:-1px}
  .tile .n{font-size:26px;font-weight:650;letter-spacing:-.02em}
  .tile .l{font-size:12px;color:var(--ink-2);margin-top:2px}
  .controls{display:flex;gap:10px;margin:0 0 14px;flex-wrap:wrap}
  .controls input{flex:1;min-width:220px;background:var(--surface);border:1px solid var(--line);
    border-radius:8px;padding:8px 12px;font:inherit;color:var(--ink)}
  .controls input:focus{outline:2px solid var(--accent);outline-offset:-1px}
  table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--line);
    border-radius:10px;overflow:hidden;font-size:14px}
  .tablebox{overflow-x:auto;border-radius:10px}
  th{position:sticky;top:0;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
    color:var(--ink-3);background:var(--surface);padding:10px 12px;border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap}
  th .dir{color:var(--accent)}
  td{padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}
  tr.row{cursor:pointer}
  tr.row:hover{background:var(--hover)}
  tr.row.open{background:var(--hover)}
  td.co{font-weight:600;white-space:nowrap}
  .chip{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:2px 10px;
    font-size:12px;font-weight:600;white-space:nowrap}
  .chip.hot{background:var(--hot-bg);color:var(--hot-ink)}
  .chip.warm{background:var(--warm-bg);color:var(--warm-ink)}
  .chip.mon,.chip.unt{background:var(--mon-bg);color:var(--mon-ink)}
  .why{color:var(--ink-2);max-width:480px}
  .flag{display:inline-block;background:var(--chip);color:var(--ink-2);border-radius:6px;
    padding:1px 7px;font-size:11px;margin:2px 4px 0 0;white-space:nowrap}
  .switch{font-size:12px;color:var(--hot-ink);font-weight:600;white-space:nowrap}
  tr.detail td{background:var(--bg);padding:0;border-bottom:1px solid var(--line)}
  .detailbox{padding:18px 22px;display:grid;gap:16px}
  .touch{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .touch h4{margin:0 0 8px;font-size:13px;display:flex;justify-content:space-between;align-items:center}
  .touch pre{margin:0;white-space:pre-wrap;font:13px/1.55 inherit;color:var(--ink-2)}
  button.copy{background:var(--chip);border:1px solid var(--line);color:var(--ink-2);border-radius:7px;
    padding:4px 12px;font:600 12px/1 inherit;cursor:pointer}
  button.copy:hover{color:var(--ink)} button.copy.ok{color:var(--accent);border-color:var(--accent)}
  .briefmd{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:18px 22px;
    font-size:14px;overflow-x:auto}
  .briefmd h1{font-size:18px;margin:.2em 0} .briefmd h2{font-size:14px;margin:1.2em 0 .4em;
    text-transform:uppercase;letter-spacing:.05em;color:var(--ink-3)} .briefmd h3{font-size:13px;margin:1em 0 .3em}
  .briefmd blockquote{margin:.6em 0;padding:.2em 1em;border-left:3px solid var(--accent);color:var(--ink-2)}
  .briefmd a{color:var(--accent)} .briefmd li{margin:.25em 0} .briefmd em{color:var(--ink-3)}
  .empty{padding:40px;text-align:center;color:var(--ink-3)}
  .meta{font-size:12px;color:var(--ink-3)}
</style></head><body>
<div class="wrap">
  <header>
    <h1>Signal Stacking — Account Dashboard</h1>
    <p>Generated __GENERATED__ · __N_ALL__ accounts · click a tile to filter, a row for the brief + sequence</p>
  </header>
  <div class="tiles" id="tiles">
    <div class="tile" data-tier="🔥"><div class="n">__N_HOT__</div><div class="l">🔥 In-market now</div></div>
    <div class="tile" data-tier="🟡"><div class="n">__N_WARM__</div><div class="l">🟡 Warming</div></div>
    <div class="tile" data-tier="⚪"><div class="n">__N_MON__</div><div class="l">⚪ Monitor</div></div>
    <div class="tile" data-tier="—"><div class="n">__N_UNT__</div><div class="l">Untiered (legacy)</div></div>
  </div>
  <div class="controls"><input id="q" type="search" placeholder="Search company, contact, signal…"></div>
  <div class="tablebox"><table>
    <thead><tr>
      <th data-k="company">Company <span class="dir"></span></th>
      <th data-k="tier">Tier <span class="dir"></span></th>
      <th>Why now</th>
      <th data-k="contact">Contact <span class="dir"></span></th>
      <th data-k="researched">Researched <span class="dir"></span></th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table></div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const TIER_CLASS = {"🔥":"hot","🟡":"warm","⚪":"mon","—":"unt"};
const TIER_ORD = {"🔥":0,"🟡":1,"⚪":2,"—":3};
let tierFilter = null, query = '', sortK = null, sortDir = 1, openSlug = null;

const esc = s => (s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function mdToHtml(md){
  let h = esc(md);
  h = h.replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>');
  h = h.replace(/^&gt; ?(.*)$/gm,'<blockquote>$1</blockquote>').replace(/<\/blockquote>\n<blockquote>/g,'<br>');
  h = h.replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>').replace(/(^|[^_\w])_([^_\n]+)_(?=[^_\w]|$)/g,'$1<em>$2</em>');
  h = h.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
  h = h.replace(/^\s*[-*] (.*)$/gm,'<li>$1</li>').replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g,'<ul>$1</ul>');
  h = h.replace(/^(\d+)\. (.*)$/gm,'<li>$2</li>');
  return h.split(/\n{2,}/).map(b => /^<(h\d|ul|blockquote|li)/.test(b.trim()) ? b : '<p>'+b.trim().replace(/\n/g,'<br>')+'</p>').join('\n');
}

function visible(){
  let rows = DATA.filter(b => !tierFilter || b.tier === tierFilter);
  if (query){
    const q = query.toLowerCase();
    rows = rows.filter(b => (b.company+' '+b.contact+' '+b.whyNow+' '+b.tierLabel).toLowerCase().includes(q));
  }
  if (sortK){
    rows = rows.slice().sort((a,b) => {
      let x = a[sortK] ?? '', y = b[sortK] ?? '';
      if (sortK === 'tier'){ x = TIER_ORD[a.tier]; y = TIER_ORD[b.tier]; }
      return (x < y ? -1 : x > y ? 1 : 0) * sortDir;
    });
  }
  return rows;
}

function touchBlock(label, text, id){
  if (!text) return '';
  return `<div class="touch"><h4>${label}
    <button class="copy" data-copy="${id}">Copy</button></h4><pre id="${id}">${esc(text)}</pre></div>`;
}

function render(){
  const rows = visible();
  const tb = document.getElementById('rows');
  if (!rows.length){ tb.innerHTML = '<tr><td colspan="5" class="empty">No accounts match.</td></tr>'; return; }
  tb.innerHTML = rows.map(b => {
    const flags = (b.flags||[]).map(f => `<span class="flag">${esc(f)}</span>`).join('');
    const sw = b.switchPlay ? '<div class="switch">switch play</div>' : '';
    const open = b.slug === openSlug;
    let detail = '';
    if (open){
      detail = `<tr class="detail"><td colspan="5"><div class="detailbox">
        ${touchBlock('Touch 1 — Email (day 0)', b.touch1, 'c1-'+b.slug)}
        ${touchBlock('Touch 2 — Bump (day 3)', b.touch2, 'c2-'+b.slug)}
        ${touchBlock('Touch 3 — LinkedIn note (day 5)', b.touch3, 'c3-'+b.slug)}
        <div class="briefmd">${mdToHtml(b.markdown)}</div>
        <div class="meta">source file: research/${esc(b.file)}${b.email ? ' · contact email on file: '+esc(b.email) : ''}</div>
      </div></td></tr>`;
    }
    return `<tr class="row ${open?'open':''}" data-slug="${b.slug}">
      <td class="co">${esc(b.company)}${sw}</td>
      <td><span class="chip ${TIER_CLASS[b.tier]}">${b.tier==='—'?'':b.tier+' '}${esc(b.tierLabel)}</span>${flags?'<div>'+flags+'</div>':''}</td>
      <td class="why">${esc(b.whyNow)}</td>
      <td>${esc(b.contact)||'<span class="meta">—</span>'}</td>
      <td class="meta">${esc(b.researched)||'—'}</td>
    </tr>${detail}`;
  }).join('');
}

document.getElementById('rows').addEventListener('click', e => {
  const btn = e.target.closest('button.copy');
  if (btn){
    const pre = document.getElementById(btn.dataset.copy);
    navigator.clipboard.writeText(pre.textContent).then(() => {
      btn.textContent = 'Copied ✓'; btn.classList.add('ok');
      setTimeout(() => { btn.textContent = 'Copy'; btn.classList.remove('ok'); }, 1400);
    });
    e.stopPropagation(); return;
  }
  const tr = e.target.closest('tr.row');
  if (tr){ openSlug = openSlug === tr.dataset.slug ? null : tr.dataset.slug; render(); }
});
document.getElementById('tiles').addEventListener('click', e => {
  const t = e.target.closest('.tile'); if (!t) return;
  tierFilter = tierFilter === t.dataset.tier ? null : t.dataset.tier;
  document.querySelectorAll('.tile').forEach(x => x.classList.toggle('active', x.dataset.tier === tierFilter));
  render();
});
document.getElementById('q').addEventListener('input', e => { query = e.target.value; render(); });
document.querySelectorAll('th[data-k]').forEach(th => th.addEventListener('click', () => {
  const k = th.dataset.k;
  sortDir = sortK === k ? -sortDir : 1; sortK = k;
  document.querySelectorAll('th .dir').forEach(d => d.textContent = '');
  th.querySelector('.dir').textContent = sortDir === 1 ? '▲' : '▼';
  render();
}));
render();
</script>
</body></html>
"""

if __name__ == "__main__":
    build()
