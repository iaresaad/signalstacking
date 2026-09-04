#!/usr/bin/env python3
"""Apollo enrichment for Signal Stacking: emails + mobile numbers.

Turns the people `brieflib.brief_contacts()` pulls out of a brief into
reachable contacts. Two things about Apollo's API drive the whole design:

1. Emails come back synchronously from `/people/bulk_match`. Mobile numbers do
   NOT. Apollo refuses `reveal_phone_number` unless you also pass a
   `webhook_url`, then delivers phones to it minutes later, out of band. The
   Scout server exposes `/api/apollo/webhook` for exactly that, but it only
   works when a public tunnel points at this machine (see `.env.example`).
2. Credits are real money: ~1 for an email, ~8 more when a mobile is found.
   So every result is cached to disk and `estimate()` can price a run before
   it happens, the same way the token-cost modal gates a research run.

Stdlib only, like the rest of scripts/.
"""

import json
import os
import re
import secrets
import threading
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from brieflib import ACCOUNTS, ROOT

API_BASE = "https://api.apollo.io/api/v1"
CACHE_PATH = ACCOUNTS / "apollo-cache.json"
ENV_PATH = ROOT / ".env"
BATCH = 10          # Apollo's hard cap for bulk_match
EMAIL_CREDITS = 1   # per person when an email/demographic is found
PHONE_CREDITS = 8   # additional, only when a mobile is actually returned

_cache_lock = threading.RLock()


# ------------------------------------------------------------------ config

def load_env(path=ENV_PATH):
    """Read KEY=VALUE lines from .env without clobbering the real environment."""
    if not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_key():
    load_env()
    return os.environ.get("APOLLO_API_KEY", "").strip()


def webhook_base():
    load_env()
    return os.environ.get("APOLLO_WEBHOOK_BASE", "").strip().rstrip("/")


def phone_available():
    """Mobile reveal needs a public URL Apollo can POST back to."""
    return bool(webhook_base())


def webhook_token():
    """Shared secret embedded in the webhook URL.

    The tunnel that receives Apollo's callback is public, so an unauthenticated
    endpoint would let anyone inject fabricated phone numbers into the cache.
    Generated once and persisted to .env.
    """
    load_env()
    tok = os.environ.get("APOLLO_WEBHOOK_TOKEN", "").strip()
    if tok:
        return tok
    tok = secrets.token_urlsafe(24)
    os.environ["APOLLO_WEBHOOK_TOKEN"] = tok
    try:
        with open(ENV_PATH, "a", encoding="utf-8") as f:
            f.write(f"\nAPOLLO_WEBHOOK_TOKEN={tok}\n")
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
    return tok


def webhook_url():
    base = webhook_base()
    return f"{base}/api/apollo/webhook?t={webhook_token()}" if base else ""


# ------------------------------------------------------------------- cache

def _norm_linkedin(url):
    if not url:
        return ""
    u = re.sub(r"^https?://", "", url.strip().lower())
    u = re.sub(r"^([a-z]{2,3}\.)?linkedin\.com", "linkedin.com", u)
    return u.split("?")[0].rstrip("/")


def person_key(person, domain=""):
    """Stable cache key: LinkedIn URL when present, else name+company domain."""
    li = _norm_linkedin(person.get("linkedin"))
    if li:
        return li
    return f"{(person.get('name') or '').strip().lower()}|{(domain or '').lower()}"


def load_cache():
    with _cache_lock:
        try:
            return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}


def save_cache(cache):
    """Atomic write, since the webhook thread and a request thread both write here."""
    with _cache_lock:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)


# ------------------------------------------------------------------ pricing

def estimate(people, domain="", reveal_phone=False, force=False):
    """Credit cost of enriching `people`, counting only uncached lookups."""
    cache = load_cache()
    todo = [p for p in people if force or not _fresh(cache.get(person_key(p, domain)))]
    n = len(todo)
    phone = reveal_phone and phone_available()
    return {
        "people": len(people),
        "to_fetch": n,
        "cached": len(people) - n,
        "reveal_phone": phone,
        "credits_min": n * EMAIL_CREDITS,
        # worst case: every person yields both an email and a mobile
        "credits_max": n * (EMAIL_CREDITS + (PHONE_CREDITS if phone else 0)),
    }


def _fresh(entry):
    return bool(entry and entry.get("fetched"))


# ---------------------------------------------------------------- transport

def _post(path, body):
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "accept": "application/json",
                 "Cache-Control": "no-cache", "x-api-key": api_key()},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = (e.read() or b"").decode("utf-8", "replace")[:400]
        return e.code, {"error": f"Apollo HTTP {e.code}", "detail": detail}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def health():
    """Validate the key without spending credits."""
    if not api_key():
        return False, "APOLLO_API_KEY is not set (add it to .env)"
    req = urllib.request.Request(
        f"{API_BASE}/auth/health",
        headers={"accept": "application/json", "x-api-key": api_key()})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read() or b"{}")
            return bool(d.get("is_logged_in")), json.dumps(d)
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: key rejected (enrichment needs a MASTER key)"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# --------------------------------------------------------------- extraction

def _split_name(full):
    parts = (full or "").split()
    if len(parts) < 2:
        return (full or ""), ""
    return parts[0], " ".join(parts[1:])


def _detail(person, domain):
    first, last = _split_name(person.get("name"))
    d = {"first_name": first, "last_name": last, "name": person.get("name") or ""}
    if domain:
        d["domain"] = domain
    if person.get("linkedin"):
        d["linkedin_url"] = person["linkedin"]
    if person.get("title"):
        d["title"] = person["title"]
    return d


def _phones(match):
    out = []
    for ph in (match.get("phone_numbers") or []):
        num = ph.get("sanitized_number") or ph.get("raw_number")
        if not num:
            continue
        out.append({
            "number": num,
            "type": (ph.get("type") or "").lower(),
            "mobile": (ph.get("type") or "").lower() in ("mobile", "cell", "personal"),
            # Apollo flags numbers on do-not-call registries. Surface it,
            # dialling one of these is a compliance problem, not a nuance.
            "dnc": bool(ph.get("dnc_status") and ph["dnc_status"] != "no_status"),
            "status": ph.get("status") or "",
        })
    return out


def _shape(match, person):
    """Apollo match -> the compact record the app and dashboard consume."""
    if not match:
        return {"matched": False, "fetched": datetime.now().isoformat(timespec="seconds"),
                "name": person.get("name", ""), "emails": [], "phones": []}
    emails = []
    if match.get("email"):
        emails.append({"email": match["email"], "kind": "work",
                       "status": match.get("email_status") or ""})
    for e in (match.get("personal_emails") or []):
        if e and e not in [x["email"] for x in emails]:
            emails.append({"email": e, "kind": "personal", "status": ""})
    return {
        "matched": True,
        "fetched": datetime.now().isoformat(timespec="seconds"),
        "apollo_id": match.get("id") or "",
        "name": match.get("name") or person.get("name", ""),
        "title": match.get("title") or person.get("title", ""),
        "company": ((match.get("organization") or {}).get("name")
                    or match.get("organization_name") or ""),
        "linkedin": match.get("linkedin_url") or person.get("linkedin", ""),
        "location": ", ".join(x for x in (match.get("city"), match.get("state"),
                                          match.get("country")) if x),
        "emails": emails,
        "phones": _phones(match),
        "phone_pending": False,
    }


# ------------------------------------------------------------------ enrich

def enrich(people, domain="", reveal_phone=False, force=False, webhook_override=""):
    """Enrich `people` (brieflib contact dicts). -> (ok, result-dict).

    Cached people are never re-fetched unless `force`. Phones are requested only
    when a webhook URL is available; otherwise emails are returned and each
    record is marked so the UI can say why a number is missing rather than
    implying the person has none.
    """
    if not api_key():
        return False, {"error": "APOLLO_API_KEY is not set. Add it to .env"}

    cache = load_cache()
    want_phone = bool(reveal_phone and (webhook_override or phone_available()))
    hook = webhook_override or (webhook_url() if want_phone else "")

    todo, results = [], {}
    for p in people:
        k = person_key(p, domain)
        if not force and _fresh(cache.get(k)):
            results[k] = cache[k]
        else:
            todo.append((k, p))

    fetched = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        body = {"details": [_detail(p, domain) for _, p in chunk],
                "reveal_personal_emails": True}
        if want_phone:
            body["reveal_phone_number"] = True
            body["webhook_url"] = hook
        code, data = _post("/people/bulk_match", body)
        if code != 200:
            return False, {"error": data.get("error", f"HTTP {code}"),
                           "detail": data.get("detail", ""), "partial": results}
        matches = data.get("matches") or []
        for j, (k, p) in enumerate(chunk):
            rec = _shape(matches[j] if j < len(matches) else None, p)
            rec["role"] = p.get("role", "")
            rec["entry"] = bool(p.get("entry"))
            if want_phone and rec["matched"] and not rec["phones"]:
                rec["phone_pending"] = True      # arriving later via webhook
            cache[k] = rec
            results[k] = rec
            fetched += 1

    save_cache(cache)
    return True, {
        "results": results,
        "fetched": fetched,
        "cached": len(people) - fetched,
        "phone_requested": want_phone,
        "phone_note": ("" if want_phone else
                       "mobile numbers skipped. Set APOLLO_WEBHOOK_BASE to a public "
                       "tunnel URL; Apollo will not reveal phones without a webhook"),
        "webhook": hook,
    }


def apply_webhook(payload):
    """Merge an async Apollo phone payload into the cache. -> (n_updated, ids)."""
    people = payload.get("people") or payload.get("matches") or []
    if isinstance(payload.get("person"), dict):
        people = [payload["person"]]
    with _cache_lock:
        cache = load_cache()
        by_id = {v.get("apollo_id"): k for k, v in cache.items() if v.get("apollo_id")}
        by_li = {_norm_linkedin(v.get("linkedin")): k for k, v in cache.items()
                 if v.get("linkedin")}
        updated = []
        for m in people:
            if not isinstance(m, dict):
                continue
            k = by_id.get(m.get("id")) or by_li.get(_norm_linkedin(m.get("linkedin_url")))
            if not k:
                continue
            phones = _phones(m)
            if not phones:
                continue
            cache[k]["phones"] = phones
            cache[k]["phone_pending"] = False
            cache[k]["phone_fetched"] = datetime.now().isoformat(timespec="seconds")
            updated.append(k)
        if updated:
            save_cache(cache)
    return len(updated), updated
