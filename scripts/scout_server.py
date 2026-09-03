#!/usr/bin/env python3
"""Signal Scout — local web app + run orchestrator for Signal Stacking.

    python3 scripts/scout_server.py [--port 8765]

Serves the Scout app on 127.0.0.1 (local only, nothing leaves your machine),
reads the same briefs as the static dashboard (via brieflib), and launches
headless `claude -p "/signal-stacking …"` runs on request. One run at a time.

Env:
    SCOUT_BYPASS=1   spawn runs with --permission-mode bypassPermissions
                     instead of the default acceptEdits + tool allowlist.
                     Only if a run's log shows tool-permission denials —
                     bypass lets the headless agent run arbitrary commands.
"""

import argparse
import csv
import io
import json
import os
import re
import shutil
import signal
import subprocess
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import brieflib
import costlib
from brieflib import (ACCOUNTS, FRESH_DAYS, RESEARCH, ROOT, load_briefs,
                      load_closed_lost, load_email_map, normalize_company,
                      slugify)

RUNS = ROOT / "runs"
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
        # recorded the real outcome — only claim "orphaned" if it never did.
        _finalize_status(info.get("id"), "failed", note="orphaned — process gone")
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
    """Atomic replace — a partial write must never leave unparseable JSON."""
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
    out, seen = [], set()
    for r in rows:
        company = (r.get(comp) or "").strip() if comp else ""
        if not company:
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


def app_state():
    today = date.today()
    briefs = load_briefs(today, load_email_map())
    by_slug = {b["slug"]: b for b in briefs}
    cl = load_closed_lost()
    usage = _usage_slugs()

    accounts = _read_accounts()
    for a in accounts:
        slug = slugify(a["company"])
        b = by_slug.get(slug)
        a["slug"] = slug
        a["has_brief"] = bool(b)
        a["tier"] = b["tier"] if b else ""
        a["age_days"] = b["ageDays"] if b else None
        a["fresh"] = bool(b and b["ageDays"] is not None and b["ageDays"] < FRESH_DAYS)
        a["closed_lost"] = a["domain"] in cl or normalize_company(a["company"]) in cl
        a["usage"] = slug in usage

    counts = {t: sum(1 for b in briefs if b["tier"] == t) for t in ("🔥", "🟡", "⚪", "—")}
    return {
        "briefs": briefs,
        "counts": counts,
        "accounts": accounts,
        "closed_lost_rows": len(set(id(v) for v in cl.values())),
        "usage_companies": sorted(usage),
        "run": run_status(),
        "generated": today.isoformat(),
    }


def estimate(payload):
    """Cost preview for a proposed run — the 'be smart about tokens' gate."""
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
    return 200, {"accounts": len(_read_accounts())}


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
        elif path == "/api/accounts":
            replace = "replace=0" not in self.path
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
    active_run()  # clears any stale ACTIVE marker on startup
    for port in range(args.port, args.port + 11):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    else:
        raise SystemExit(f"no free port in {args.port}-{args.port + 10}")
    print(f"Signal Scout → http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
