#!/usr/bin/env python3
"""Signal Scout: local web app + run orchestrator for Signal Stacking.

    python3 scripts/scout_server.py [--port 8765]

Serves the Scout app on 127.0.0.1 (local only, nothing leaves your machine),
reads the same briefs as the static dashboard (via brieflib), and launches
headless `claude -p "/signal-stacking …"` runs on request. One run at a time.

Env:
    SCOUT_BYPASS=1   spawn runs with --permission-mode bypassPermissions
                     instead of the default acceptEdits + tool allowlist.
                     Only if a run's log shows tool-permission denials:
                     bypass lets the headless agent run arbitrary commands.
"""

import argparse
import csv
import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import apollolib
import brieflib
import costlib
from brieflib import (ACCOUNTS, FRESH_DAYS, RESEARCH, ROOT, is_hidden, load_briefs,
                      load_closed_lost, load_do_not_contact, load_email_map,
                      load_hidden, lookup_suppressed, normalize_company, slugify)

RUNS = ROOT / "runs"
SPEND = ROOT / "runs" / "spend.json"
ACTIVE = RUNS / "ACTIVE"
APP_HTML = Path(__file__).resolve().parent / "scout_app.html"
USAGE_MD = ROOT / ".claude" / "signal-stacking" / "trumpet-usage-data.md"
CLOSED_LOST_CSV = ACCOUNTS / "closed-lost.csv"
ACCOUNTS_CSV = ACCOUNTS / "accounts.csv"

# Tools the headless orchestrator + subagents actually use. Server-level MCP
# rules (mcp__exa) allow every tool on that server; exa2..exa5 cover extra keys.
ALLOWED_TOOLS = (
    "mcp__exa mcp__exa2 mcp__exa3 mcp__exa4 mcp__exa5 "
    "WebSearch WebFetch ToolSearch Read Glob Grep Write Edit Task "
    "TaskCreate TaskUpdate TaskList TaskGet "
    "Bash(python3 scoring/score.py:*) Bash(python3 scripts/build_dashboard.py:*)"
)

_lock = threading.Lock()
# Guards every status.json read-modify-write. The monitor thread and any
# polling request's orphan check both touch it; without this they interleave
# and leave a half-overwritten file that no longer parses as JSON.
_status_lock = threading.RLock()


# ---------------------------------------------------------------- run manager

def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False


def active_run():
    """Return {'id', 'pid'} of the live run, clearing a stale marker."""
    if not ACTIVE.is_file():
        return None
    try:
        info = json.loads(ACTIVE.read_text())
    except Exception:
        ACTIVE.unlink(missing_ok=True)
        return None
    if not _pid_alive(info.get("pid")):
        # PID gone. Usually the monitor thread is mid-cleanup and has already
        # recorded the real outcome. Only claim "orphaned" if it never did.
        _finalize_status(info.get("id"), "failed", note="orphaned, process gone")
        ACTIVE.unlink(missing_ok=True)
        return None
    return info


def _status_path(run_id):
    return RUNS / run_id / "status.json"


def _read_status(run_id):
    try:
        return json.loads(_status_path(run_id).read_text())
    except Exception:
        return None


def _write_status(run_id, st):
    """Atomic replace, so a partial write never leaves unparseable JSON."""
    with _status_lock:
        path = _status_path(run_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(st, indent=1))
        os.replace(tmp, path)


TERMINAL = ("done", "failed", "cancelled")


def _finalize_status(run_id, state, exit_code=None, note=""):
    if not run_id:
        return
    with _status_lock:  # re-read inside the lock: the monitor may have just finished
        st = _read_status(run_id) or {}
        if st.get("state") in TERMINAL:
            return
        st.update(state=state, ended=datetime.now().isoformat(timespec="seconds"))
        if exit_code is not None:
            st["exit_code"] = exit_code
        if note:
            st["note"] = note
        try:
            _write_status(run_id, st)
        except Exception:
            pass


def last_run_id():
    if not RUNS.is_dir():
        return None
    runs = sorted((p for p in RUNS.iterdir() if p.is_dir()), key=lambda p: p.name)
    return runs[-1].name if runs else None


def _research_mtimes():
    if not RESEARCH.is_dir():
        return {}
    return {p.name: p.stat().st_mtime for p in RESEARCH.glob("*.md")}


def _monitor(run_id, proc, before):
    """Track a running claude process: new/updated briefs + exit state."""
    while True:
        done = proc.poll() is not None
        with _status_lock:  # read-modify-write must be one step (see _write_status)
            st = _read_status(run_id) or {}
            now = _research_mtimes()
            st["briefs_new"] = sorted(
                n[:-3] for n, m in now.items() if n not in before or m > before[n] + 1
            )
            if done:
                cancelled = st.get("state") == "cancelling"
                st.update(
                    state="cancelled" if cancelled
                    else ("done" if proc.returncode == 0 else "failed"),
                    exit_code=proc.returncode,
                    ended=datetime.now().isoformat(timespec="seconds"),
                )
                st.pop("note", None)  # drop any speculative "orphaned" note
                _write_status(run_id, st)
                ACTIVE.unlink(missing_ok=True)
                return
            _write_status(run_id, st)
        threading.Event().wait(2)


def _monitor_pid(run_id, pid, before):
    """Track a run this process did not spawn (server restarted mid-run).

    Runs are started with start_new_session, so they survive a server restart,
    but the monitor thread does not, and without one the next status poll finds
    a dead PID and reports a successful run as "orphaned". This re-attaches by
    PID. The exit code is unknowable from outside the parent, so the outcome is
    inferred from whether briefs landed, and the status says so rather than
    implying we watched it exit.
    """
    while True:
        alive = _pid_alive(pid)
        with _status_lock:
            st = _read_status(run_id) or {}
            now = _research_mtimes()
            st["briefs_new"] = sorted(
                n[:-3] for n, m in now.items() if n not in before or m > before[n] + 1
            )
            st["reattached"] = True
            if not alive:
                cancelled = st.get("state") == "cancelling"
                landed = bool(st["briefs_new"])
                st.update(
                    state="cancelled" if cancelled else ("done" if landed else "failed"),
                    ended=datetime.now().isoformat(timespec="seconds"),
                    note=("outcome inferred after a server restart, exit code unknown"
                          if not cancelled else ""),
                )
                _write_status(run_id, st)
                ACTIVE.unlink(missing_ok=True)
                return
            _write_status(run_id, st)
        threading.Event().wait(2)


def reattach_active_run():
    """On startup, adopt a run still executing from a previous server."""
    info = active_run()
    if not info:
        return None
    st = _read_status(info["id"]) or {}
    if st.get("state") in TERMINAL:
        return None
    before = {n: m for n, m in _research_mtimes().items()
              if n[:-3] not in (st.get("briefs_new") or [])}
    threading.Thread(target=_monitor_pid, args=(info["id"], info["pid"], before),
                     daemon=True).start()
    return info


def start_run(payload):
    """Stage runs/<id>/, spawn headless claude, start the monitor. -> (code, dict)"""
    with _lock:
        if active_run():
            return 409, {"error": "a run is already in progress", "active": active_run()}

        claude = shutil.which("claude")
        if not claude:
            return 500, {"error": "`claude` CLI not found on PATH"}

        companies = payload.get("companies") or []
        refresh = bool(payload.get("refresh"))
        if not companies:
            return 400, {"error": "no companies given"}
        for c in companies:  # accept a pasted URL as a domain
            name, dom = normalize_target(c.get("domain") or c.get("company") or "")
            if dom:
                c["company"], c["domain"] = c.get("company") or name, dom
                if normalize_target(c["company"])[1]:
                    c["company"] = name

        # Suppression was only an instruction in the orchestrator prompt, which
        # cannot stop a run from being launched or the tokens from being spent.
        # Enforce it here: a current customer or open opp must not be researched
        # and must never reach a sequence.
        dnc = load_do_not_contact()
        blocked = []
        for c in companies:
            hit = lookup_suppressed(dnc, c.get("company") or "", c.get("domain") or "")
            if hit:
                blocked.append({"company": c.get("company") or c.get("domain"),
                                "matched": hit.get("company", ""),
                                "reason": hit.get("reason", "on the do-not-contact list")})
        if blocked and not payload.get("override_suppression"):
            return 409, {"error": "suppressed accounts in this run", "blocked": blocked,
                         "detail": "On accounts/do-not-contact.csv: current customers, "
                                   "open opportunities or cooloffs. Remove them from the "
                                   "run, or re-send with override_suppression to proceed."}

        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        rdir = RUNS / run_id
        rdir.mkdir(parents=True, exist_ok=True)

        single = len(companies) == 1 and payload.get("mode") != "batch"
        if single:
            c = companies[0]
            prompt = f"/signal-stacking {c.get('domain') or c.get('company')}"
        else:
            staged = rdir / "accounts.csv"
            with staged.open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["company", "domain", "target", "tools", "notes"])
                for c in companies:
                    w.writerow([c.get("company", ""), c.get("domain", ""),
                                c.get("target", ""), c.get("tools", ""),
                                c.get("notes", "")])
            prompt = f"/signal-stacking batch runs/{run_id}/accounts.csv"
            if refresh:
                prompt += " refresh"

        (rdir / "request.json").write_text(json.dumps(
            {"id": run_id, "prompt": prompt, "refresh": refresh,
             "companies": companies,
             "created": datetime.now().isoformat(timespec="seconds")}, indent=1))

        if os.environ.get("SCOUT_BYPASS") == "1":
            perm = ["--permission-mode", "bypassPermissions"]
        else:
            perm = ["--permission-mode", "acceptEdits", "--allowedTools", ALLOWED_TOOLS]

        logf = (rdir / "run.log").open("w")
        proc = subprocess.Popen(
            [claude, "-p", prompt, *perm],
            cwd=ROOT, stdout=logf, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            remember_accounts(companies)
            record_spend(run_id, {**payload, "companies": companies}, estimate(payload))
        except Exception:
            pass  # never let bookkeeping kill a launched run
        ACTIVE.write_text(json.dumps({"id": run_id, "pid": proc.pid}))
        before = _research_mtimes()
        _write_status(run_id, {
            "id": run_id, "state": "running", "pid": proc.pid, "prompt": prompt,
            "started": datetime.now().isoformat(timespec="seconds"),
            "n_accounts": len(companies), "briefs_new": [],
        })
        threading.Thread(target=_monitor, args=(run_id, proc, before),
                         daemon=True).start()
        return 201, {"id": run_id, "prompt": prompt}


def cancel_run():
    info = active_run()
    if not info:
        return 404, {"error": "no active run"}
    st = _read_status(info["id"]) or {}
    st["state"] = "cancelling"
    _write_status(info["id"], st)
    try:
        os.killpg(os.getpgid(info["pid"]), signal.SIGTERM)
    except OSError as e:
        return 500, {"error": f"kill failed: {e}"}
    return 200, {"cancelled": info["id"]}


def run_status():
    info = active_run()
    run_id = info["id"] if info else last_run_id()
    if not run_id:
        return {"state": "idle"}
    st = _read_status(run_id) or {"id": run_id, "state": "unknown"}
    log = RUNS / run_id / "run.log"
    if log.is_file():
        with log.open("rb") as f:
            f.seek(max(0, log.stat().st_size - 4096))
            st["log_tail"] = f.read().decode("utf-8", "replace")
    return st


# ------------------------------------------------------------------- app data

def _usage_slugs():
    if not USAGE_MD.is_file():
        return set()
    slugs = re.findall(r"^###\s+(\S+)", USAGE_MD.read_text(encoding="utf-8"), re.M)
    return {s for s in slugs if "<" not in s}  # skip template placeholders


def _read_accounts():
    """accounts/accounts.csv with flexible headers -> canonical row dicts."""
    if not ACCOUNTS_CSV.is_file():
        return []
    try:
        rows = list(csv.DictReader(ACCOUNTS_CSV.open(encoding="utf-8-sig")))
    except Exception:
        return []
    if not rows:
        return []
    cols = {c.lower().strip(): c for c in rows[0].keys() if c}

    def col(*cands):
        for cand in cands:
            for c in cols:
                if cand in c:
                    return cols[c]
        return None

    comp = col("company name", "company")
    dom = col("domain", "website")
    name = col("full name", "target")
    title = col("job title", "title")
    tools = col("tools", "technolog")
    mail = next((cols[c] for c in cols
                 if "email" in c and "?" not in c and "all" not in c), None)
    hidden = load_hidden()
    out, seen = [], set()
    for r in rows:
        company = (r.get(comp) or "").strip() if comp else ""
        if not company:
            continue
        if is_hidden(hidden, company, (r.get(dom) or "").strip().lower() if dom else ""):
            continue
        key = normalize_company(company)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "company": company,
            "domain": (r.get(dom) or "").strip().lower() if dom else "",
            "target": (r.get(name) or "").strip() if name else "",
            "title": (r.get(title) or "").strip() if title else "",
            "tools": (r.get(tools) or "").strip() if tools else "",
            "email": (r.get(mail) or "").strip() if mail else "",
        })
    return out


def _hidden_count():
    """How many accounts are withheld from the UI entirely."""
    p = ACCOUNTS / "hidden.csv"
    if not p.is_file():
        return 0
    try:
        return sum(1 for r in csv.DictReader(p.open(encoding="utf-8-sig"))
                   if (r.get("company") or "").strip())
    except Exception:
        return 0


def app_state():
    today = date.today()
    briefs = attach_enrichment(load_briefs(today, load_email_map()))
    by_slug = {b["slug"]: b for b in briefs}
    # Slug alone is too strict a join: an accounts CSV says "Docusign, Inc."
    # while the brief file is named differently, so the row would read "no brief"
    # inviting a paid re-run of research that already exists.
    by_norm, by_dom = {}, {}
    for b in briefs:
        by_norm.setdefault(normalize_company(b["company"]), b)
        by_norm.setdefault(normalize_company(b["slug"].replace("-", " ")), b)
        if b.get("domain"):
            by_dom.setdefault(b["domain"].lower(), b)
    cl = load_closed_lost()
    dnc = load_do_not_contact()
    usage = _usage_slugs()

    accounts = _read_accounts()
    for a in accounts:
        slug = slugify(a["company"])
        # domain is the strongest join: a row typed in as "stripe.com" must find
        # the brief titled "Stripe", or the picker offers a paid re-run of it.
        b = (by_slug.get(slug)
             or by_norm.get(normalize_company(a["company"]))
             or (by_dom.get(a["domain"].lower()) if a.get("domain") else None))
        a["slug"] = b["slug"] if b else slug
        a["has_brief"] = bool(b)
        a["tier"] = b["tier"] if b else ""
        a["age_days"] = b["ageDays"] if b else None
        a["fresh"] = bool(b and b["ageDays"] is not None and b["ageDays"] < FRESH_DAYS)
        a["closed_lost"] = a["domain"] in cl or normalize_company(a["company"]) in cl
        sup = lookup_suppressed(dnc, a["company"], a["domain"])
        a["suppressed"] = bool(sup)
        a["suppress_reason"] = sup.get("reason", "") if sup else ""
        a["usage"] = slug in usage

    for b in briefs:
        sup = lookup_suppressed(dnc, b["company"], b.get("domain", ""))
        b["suppressed"] = bool(sup)
        b["suppress_reason"] = sup.get("reason", "") if sup else ""

    counts = {t: sum(1 for b in briefs if b["tier"] == t) for t in ("🔥", "🟡", "⚪", "—")}
    return {
        "briefs": briefs,
        "counts": counts,
        "accounts": accounts,
        "closed_lost_rows": len(set(id(v) for v in cl.values())),
        "suppressed_rows": len({id(v) for v in dnc.values()}),
        "hidden_rows": _hidden_count(),
        "usage_companies": sorted(usage),
        "run": run_status(),
        "apollo": apollo_status(),
        "spend": spend_summary(),
        "lanes": lane_status(),
        "generated": today.isoformat(),
    }


def _brief_by_slug(slug):
    today = date.today()
    for b in brieflib.load_briefs(today, load_email_map()):
        if b["slug"] == slug:
            return b
    return None


def attach_enrichment(briefs):
    """Merge cached Apollo data onto each brief's contacts (no API calls)."""
    cache = apollolib.load_cache()
    for b in briefs:
        n_mail = n_phone = 0
        for c in b.get("contacts", []):
            rec = cache.get(apollolib.person_key(c, b.get("domain", "")))
            c["apollo"] = rec or None
            if rec and rec.get("emails"):
                n_mail += 1
            if rec and rec.get("phones"):
                n_phone += 1
        b["enriched"] = {"contacts": len(b.get("contacts", [])),
                         "with_email": n_mail, "with_phone": n_phone}
    return briefs


def enrich_brief(payload):
    """Enrich one brief's buying committee. -> (code, dict)"""
    slug = (payload.get("slug") or "").strip()
    b = _brief_by_slug(slug)
    if not b:
        return 404, {"error": f"no brief for slug {slug!r}"}
    people = b.get("contacts") or []
    if not people:
        return 400, {"error": "this brief names no contacts to enrich"}
    # Same rule as /api/run: never build a contact list for someone we must not
    # contact. Enrichment also costs credits, so this is money as well as safety.
    dnc = load_do_not_contact()
    hit = lookup_suppressed(dnc, b["company"], b.get("domain", ""))
    if hit and not payload.get("override_suppression"):
        return 409, {"error": "suppressed account, not enriching",
                     "matched": hit.get("company", ""),
                     "reason": hit.get("reason", "on the do-not-contact list")}
    if not payload.get("all"):
        people = [c for c in people if c.get("entry")] or people[:1]
    ok, res = apollolib.enrich(
        people, domain=b.get("domain", ""),
        reveal_phone=bool(payload.get("reveal_phone", True)),
        force=bool(payload.get("force")))
    if not ok:
        return 502, res
    res["slug"] = slug
    res["company"] = b["company"]
    return 200, res


def enrich_estimate(payload):
    b = _brief_by_slug((payload.get("slug") or "").strip())
    if not b:
        return 404, {"error": "no such brief"}
    people = b.get("contacts") or []
    if not payload.get("all"):
        people = [c for c in people if c.get("entry")] or people[:1]
    est = apollolib.estimate(people, b.get("domain", ""),
                             reveal_phone=bool(payload.get("reveal_phone", True)),
                             force=bool(payload.get("force")))
    est.update(slug=b["slug"], company=b["company"], domain=b.get("domain", ""))
    return 200, est


# ---------------------------------------------------------------- search lanes
# A dead Exa key is invisible where you'd look for it: `claude mcp list` reports
# the server "Connected" because the key rides in the URL query string, so the
# transport handshake succeeds and only the searches 401. The orchestrator builds
# its lane list from servers that respond, so a dead key still counts as a lane:
# it widens the waves and sends half the accounts down a lane that silently falls
# back to plain WebSearch. The only honest check is a real search.
CLAUDE_CONFIG = Path.home() / ".claude.json"
LANE_TTL = 900  # seconds; a search per key is cheap but not free
_lanes = {"at": 0.0, "lanes": []}


def _exa_lanes():
    """[(name, key)] for every configured exa* MCP server."""
    try:
        cfg = json.loads(CLAUDE_CONFIG.read_text())
    except Exception:
        return []
    out = []
    for name, s in sorted((cfg.get("mcpServers") or {}).items()):
        if not re.fullmatch(r"exa\d*", name):
            continue
        m = re.search(r"exaApiKey=([^&]+)", s.get("url", "") or "")
        if m:
            out.append((name, m.group(1)))
    return out


def _probe_lane(name, key):
    req = urllib.request.Request(
        "https://api.exa.ai/search",
        data=json.dumps({"query": "revenue leader appointed", "numResults": 1}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": key}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return {"name": name, "key_tail": key[-4:], "ok": r.status == 200,
                    "status": r.status, "detail": ""}
    except urllib.error.HTTPError as e:
        return {"name": name, "key_tail": key[-4:], "ok": False, "status": e.code,
                "detail": (e.read() or b"").decode("utf-8", "replace")[:120]}
    except Exception as e:
        return {"name": name, "key_tail": key[-4:], "ok": False, "status": 0,
                "detail": f"{type(e).__name__}: {e}"}


def lane_status(force=False):
    """Which Exa lanes actually search. Cached, because /api/state polls every 3s."""
    now = time.time()
    if force or now - _lanes["at"] > LANE_TTL or not _lanes["lanes"]:
        lanes = _exa_lanes()
        if lanes:
            with ThreadPoolExecutor(max_workers=len(lanes)) as ex:
                probed = list(ex.map(lambda a: _probe_lane(*a), lanes))
        else:
            probed = []
        _lanes.update(at=now, lanes=probed)
    lanes = _lanes["lanes"]
    live = [l for l in lanes if l["ok"]]
    dead = [l for l in lanes if not l["ok"]]
    return {
        "lanes": lanes,
        "configured": len(lanes),
        "live": len(live),
        # the orchestrator sizes waves at ~5 concurrent subagents per working key
        "wave_size": len(live) * 5,
        "degraded": bool(dead),
        "checked": datetime.fromtimestamp(_lanes["at"]).isoformat(timespec="seconds"),
        "warning": ("" if not dead else
                    f"{', '.join(l['name'] for l in dead)} configured but NOT searching. "
                    f"runs will size waves for {len(lanes)} lanes and silently fall back to "
                    f"WebSearch on the dead one. Fix the key or remove the server."),
    }


_apollo_health = {"at": 0.0, "ok": False, "detail": "not checked"}
APOLLO_HEALTH_TTL = 300  # seconds


def apollo_status(force=False):
    """Key/webhook status. The health probe is cached because /api/state polls every
    3s and must not make a network call to Apollo on every poll."""
    now = time.time()
    if force or now - _apollo_health["at"] > APOLLO_HEALTH_TTL:
        ok, msg = apollolib.health()
        _apollo_health.update(at=now, ok=ok, detail=msg)
    ok, msg = _apollo_health["ok"], _apollo_health["detail"]
    return {"key_configured": bool(apollolib.api_key()), "key_valid": ok, "detail": msg,
            "phone_available": apollolib.phone_available(),
            "webhook": apollolib.webhook_url(),
            "cached_people": len(apollolib.load_cache())}


def record_spend(run_id, payload, est):
    """Append a launched run to the spend ledger.

    Estimates only. The API does not report per-run token usage back here, so
    this is a budget tracker, not a bill. Labelled as such everywhere it shows.
    """
    try:
        try:
            ledger = json.loads(SPEND.read_text())
        except Exception:
            ledger = []
        ledger.append({
            "id": run_id,
            "at": datetime.now().isoformat(timespec="seconds"),
            "companies": [c.get("company") or c.get("domain") or "" for c in
                          (payload.get("companies") or [])][:50],
            "n_accounts": len(payload.get("companies") or []),
            "mode": est.get("mode", ""),
            "refresh": bool(payload.get("refresh")),
            "tokens": (est.get("tokens") or {}).get("expected", 0),
            "usd": (est.get("usd") or {}).get("expected", 0),
        })
        tmp = SPEND.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ledger[-500:], indent=1))
        os.replace(tmp, SPEND)
    except Exception:
        pass  # never let accounting break a launch


def spend_summary():
    """Estimated spend over rolling windows, for the budget panel."""
    try:
        ledger = json.loads(SPEND.read_text())
    except Exception:
        ledger = []
    now = datetime.now()
    hidden = load_hidden()
    def _clean(e):
        e = dict(e)
        e["companies"] = ["(hidden)" if is_hidden(hidden, c) else c
                          for c in e.get("companies", [])]
        return e
    out = {"runs": len(ledger), "windows": {},
           "recent": [_clean(e) for e in ledger[-8:][::-1]],
           "note": "estimates at API list prices, a budget guide and not a bill"}
    for label, days in (("today", 1), ("last7", 7), ("last30", 30), ("all", None)):
        sel = []
        for e in ledger:
            if days is None:
                sel.append(e); continue
            try:
                at = datetime.fromisoformat(e.get("at", ""))
            except Exception:
                continue
            if label == "today":
                if at.date() == now.date():
                    sel.append(e)
            elif (now - at).days < days:
                sel.append(e)
        out["windows"][label] = {
            "runs": len(sel),
            "accounts": sum(e.get("n_accounts", 0) for e in sel),
            "tokens": sum(e.get("tokens", 0) for e in sel),
            "usd": round(sum(e.get("usd", 0) for e in sel), 2),
        }
    return out


def estimate(payload):
    """Cost preview for a proposed run: the 'be smart about tokens' gate."""
    companies = payload.get("companies") or []
    refresh = bool(payload.get("refresh"))
    single = len(companies) == 1 and payload.get("mode") != "batch"
    today = date.today()
    ages = {}
    for p in RESEARCH.glob("*.md"):
        m = re.search(r"_(?:Researched|Generated):\s*(\d{4}-\d{2}-\d{2})",
                      p.read_text(encoding="utf-8"))
        if m:
            try:
                ages[p.stem] = (today - datetime.strptime(m.group(1), "%Y-%m-%d").date()).days
            except ValueError:
                pass
    n_new = n_stale = n_fresh = 0
    for c in companies:
        age = ages.get(slugify(c.get("company") or c.get("domain") or ""))
        if age is None:
            n_new += 1
        elif age < FRESH_DAYS:
            n_fresh += 1
        else:
            n_stale += 1
    return costlib.estimate_run(n_new, n_stale, n_fresh, refresh, single=single)


# ------------------------------------------------------------------- uploads

CL_HEADER_MAP = {  # flexible CRM export headers -> canonical closed-lost columns
    "company": ["company", "account name", "account"],
    "domain": ["domain", "website"],
    "close_date": ["close_date", "closed date", "close date", "closedate"],
    "loss_reason": ["loss_reason", "loss reason", "closed lost reason", "reason"],
    "competitor": ["competitor", "lost to", "competitor lost to"],
    "champion_name": ["champion_name", "champion", "primary contact", "contact name"],
    "champion_title": ["champion_title", "champion title", "contact title"],
    "notes": ["notes", "description", "next steps"],
}


def save_closed_lost(raw):
    try:
        rows = list(csv.DictReader(io.StringIO(raw)))
    except Exception as e:
        return 400, {"error": f"unreadable CSV: {e}"}
    if not rows:
        return 400, {"error": "CSV has no data rows"}
    cols = {c.lower().strip(): c for c in rows[0].keys() if c}
    mapping = {}
    for canon, cands in CL_HEADER_MAP.items():
        for cand in cands:
            if cand in cols:
                mapping[canon] = cols[cand]
                break
    if "company" not in mapping:
        return 400, {"error": "no company/account column found",
                     "headers_seen": sorted(cols)}
    out = []
    for r in rows:
        row = {canon: (r.get(src) or "").strip() for canon, src in mapping.items()}
        if row.get("company"):
            out.append(row)
    with CLOSED_LOST_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(CL_HEADER_MAP))
        w.writeheader()
        for row in out:
            w.writerow({k: row.get(k, "") for k in CL_HEADER_MAP})
    return 200, closed_lost_state()


def closed_lost_state():
    """Rows + join preview vs accounts + briefs; unmatched rows are surfaced."""
    if not CLOSED_LOST_CSV.is_file():
        return {"rows": [], "unmatched": 0}
    rows = list(csv.DictReader(CLOSED_LOST_CSV.open(encoding="utf-8-sig")))
    accounts = _read_accounts()
    by_dom = {a["domain"]: a for a in accounts if a["domain"]}
    by_name = {normalize_company(a["company"]): a for a in accounts}
    brief_slugs = {p.stem for p in RESEARCH.glob("*.md")} if RESEARCH.is_dir() else set()
    out = []
    for r in rows:
        dom = (r.get("domain") or "").strip().lower()
        acc = by_dom.get(dom) or by_name.get(normalize_company(r.get("company") or ""))
        r = dict(r)
        r["matched_account"] = acc["company"] if acc else ""
        r["matched_brief"] = slugify(r.get("company") or "") in brief_slugs
        out.append(r)
    # unmatched = joins to neither an account row nor an existing brief
    return {"rows": out,
            "unmatched": sum(1 for r in out
                             if not r["matched_account"] and not r["matched_brief"])}


def normalize_target(s):
    """'https://www.stripe.com/pricing' -> ('stripe.com', 'stripe.com'); 'Stripe' -> ('Stripe', '')."""
    s = (s or "").strip()
    if not s:
        return "", ""
    m = re.match(r"^(?:https?://)?(?:www\.)?([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?:[/?#].*)?$", s, re.I)
    if m:
        d = m.group(1).lower()
        return d, d
    return s, ""


def remember_accounts(companies):
    """Append newly-researched companies to accounts.csv so they persist.

    An ad-hoc run typed into the box would otherwise produce a brief that never
    appears in the account picker.
    """
    if not companies:
        return
    try:
        existing = set()
        for a in _read_accounts():
            existing.update(brieflib.suppression_keys(a["company"], a.get("domain", "")))
    except Exception:
        existing = set()
    # keys, not the raw name: a run typed as "acme-co.com" must not create a
    # second row when "Acme Co" is already in the list
    new = [c for c in companies
           if c.get("company")
           and not (set(brieflib.suppression_keys(c["company"], c.get("domain", ""))) & existing)]
    if not new:
        return
    header = ["company", "domain", "full name", "job title", "tools", "email", "notes"]
    fresh = not ACCOUNTS_CSV.is_file() or not ACCOUNTS_CSV.read_text(encoding="utf-8-sig").strip()
    fieldnames = header
    if not fresh:
        try:
            rows = list(csv.DictReader(ACCOUNTS_CSV.open(encoding="utf-8-sig")))
            if rows:
                fieldnames = list(rows[0].keys())
        except Exception:
            pass
    comp_f = next((f for f in fieldnames if "company" in f.lower() and "domain" not in f.lower()), None)
    dom_f = next((f for f in fieldnames if "domain" in f.lower() or "website" in f.lower()), None)
    with ACCOUNTS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if fresh:
            w.writeheader()
        for c in new:
            row = {k: "" for k in fieldnames}
            if comp_f:
                row[comp_f] = c["company"]
            if dom_f:
                row[dom_f] = c.get("domain", "")
            w.writerow(row)


def save_accounts(raw, replace):
    try:
        rows = list(csv.DictReader(io.StringIO(raw)))
    except Exception as e:
        return 400, {"error": f"unreadable CSV: {e}"}
    if not rows:
        return 400, {"error": "CSV has no data rows"}
    if not any("company" in (c or "").lower() for c in rows[0]):
        return 400, {"error": "no company column found",
                     "headers_seen": [c for c in rows[0] if c]}
    before = len(_read_accounts()) if ACCOUNTS_CSV.is_file() else 0
    if replace or not ACCOUNTS_CSV.is_file():
        ACCOUNTS_CSV.write_text(raw if raw.endswith("\n") else raw + "\n",
                                encoding="utf-8")
    else:  # merge: append rows whose normalized company is new
        existing = {normalize_company(a["company"]) for a in _read_accounts()}
        old = ACCOUNTS_CSV.read_text(encoding="utf-8-sig")
        old_rows = list(csv.DictReader(io.StringIO(old)))
        fieldnames = list(old_rows[0].keys()) if old_rows else list(rows[0].keys())
        comp_col = next((c for c in rows[0] if "company" in (c or "").lower()), None)
        with ACCOUNTS_CSV.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            for r in rows:
                if normalize_company((r.get(comp_col) or "")) not in existing:
                    w.writerow(r)
    after = len(_read_accounts())
    return 200, {"accounts": after, "before": before,
                 "added": max(0, after - before) if not replace else after,
                 "mode": "replace" if replace else "merge"}


# --------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(
            body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n).decode("utf-8", "replace")

    def _json_body(self):
        """Parsed JSON object, or None if the body isn't one (incl. literal null)."""
        try:
            payload = json.loads(self._body() or "{}")
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, APP_HTML.read_bytes(), "text/html")
        elif path == "/api/state":
            self._send(200, app_state())
        elif path == "/api/run/status":
            self._send(200, run_status())
        elif path == "/api/closed-lost":
            self._send(200, closed_lost_state())
        elif path == "/api/lanes":
            self._send(200, lane_status(force="refresh" in self.path))
        elif path == "/api/spend":
            self._send(200, spend_summary())
        elif path == "/api/apollo/status":
            self._send(200, apollo_status(force="refresh" in self.path))
        elif path == "/api/usage-data":
            md = USAGE_MD.read_text(encoding="utf-8") if USAGE_MD.is_file() else ""
            self._send(200, {"markdown": md, "companies": sorted(_usage_slugs())})
        elif path.startswith("/research/"):
            target = (RESEARCH / path[len("/research/"):]).resolve()
            if target.is_file() and RESEARCH.resolve() in target.parents:
                ctype = "text/html" if target.suffix == ".html" else "text/plain"
                self._send(200, target.read_bytes(), ctype)
            else:
                self._send(404, {"error": "not found"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/run":
            payload = self._json_body()
            if payload is None:
                return self._send(400, {"error": "expected a JSON object body"})
            self._send(*start_run(payload))
        elif path == "/api/estimate":
            payload = self._json_body()
            if payload is None:
                return self._send(400, {"error": "expected a JSON object body"})
            self._send(200, estimate(payload))
        elif path == "/api/run/cancel":
            self._send(*cancel_run())
        elif path == "/api/enrich":
            payload = self._json_body()
            if payload is None:
                return self._send(400, {"error": "expected a JSON object body"})
            self._send(*enrich_brief(payload))
        elif path == "/api/enrich/estimate":
            payload = self._json_body()
            if payload is None:
                return self._send(400, {"error": "expected a JSON object body"})
            self._send(*enrich_estimate(payload))
        elif path == "/api/apollo/webhook":
            # Apollo delivers mobile numbers here, minutes after the sync match.
            # The tunnel is public, so an unauthenticated endpoint would let
            # anyone inject fabricated numbers, so require the shared secret.
            token = ""
            if "?" in self.path:
                token = dict(urllib.parse.parse_qsl(self.path.split("?", 1)[1])).get("t", "")
            if not secrets.compare_digest(token, apollolib.webhook_token()):
                return self._send(403, {"error": "bad or missing webhook token"})
            payload = self._json_body()
            if payload is None:
                return self._send(400, {"error": "expected a JSON object body"})
            n, keys = apollolib.apply_webhook(payload)
            self._send(200, {"updated": n, "people": keys})
        elif path == "/api/accounts":
            replace = "replace=1" in self.path  # merge unless explicitly replacing
            self._send(*save_accounts(self._body(), replace))
        elif path == "/api/closed-lost":
            self._send(*save_closed_lost(self._body()))
        else:
            self._send(404, {"error": "not found"})

    def do_PUT(self):
        if self.path.split("?")[0] == "/api/usage-data":
            payload = self._json_body()
            if payload is None:
                return self._send(400, {"error": "expected a JSON object body"})
            md = payload.get("markdown", "")
            USAGE_MD.parent.mkdir(parents=True, exist_ok=True)
            USAGE_MD.write_text(md, encoding="utf-8")
            self._send(200, {"companies": sorted(_usage_slugs())})
        else:
            self._send(404, {"error": "not found"})


def main():
    ap = argparse.ArgumentParser(description="Signal Scout local server")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    RUNS.mkdir(exist_ok=True)
    # Run state (runs/ACTIVE + status.json) has exactly one writer by design.
    # A second server over the same repo will adopt, finalize and clear runs it
    # did not start, which is how a stale instance silently corrupts the state
    # of a live one. Detect it and say so rather than failing mysteriously later.
    for _p in range(args.port, args.port + 11):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{_p}/api/run/status", timeout=1):
                print(f"WARNING: another Signal Scout is already serving on {_p}. "
                      f"Two servers share runs/ACTIVE and will corrupt each other's "
                      f"run state. Stop the other one (./scout stop) before continuing.",
                      flush=True)
                break
        except Exception:
            pass

    adopted = reattach_active_run()  # clears a stale marker, or adopts a live run
    for port in range(args.port, args.port + 11):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"no free port in {args.port}-{args.port + 10}")
    # flush: when stdout is redirected (nohup, a task runner) block buffering
    # hides this line, and with the 8765..8775 fallback you cannot otherwise
    # tell which port the server actually took.
    if port != args.port:
        print(f"port {args.port} was busy, using {port}", flush=True)
    if adopted:
        print(f"adopted run {adopted['id']} still executing (pid {adopted['pid']})", flush=True)
    print(f"Signal Scout → http://127.0.0.1:{port}  (Ctrl-C to stop)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
