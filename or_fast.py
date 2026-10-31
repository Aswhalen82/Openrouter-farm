#!/usr/bin/env python3
"""or_fast.py - OpenRouter farm over plain HTTP. No browser, no Chromium, ~7-15 s per account.

Everything the browser did is three HTTP APIs:

  Clerk FAPI  clerk.openrouter.ai/v1/client
              POST sign_ups -> PATCH password -> prepare_verification(email_code)
              -> attempt_verification(code)  => status "complete" + created_session_id
              POST client/sessions/<id>/tokens => session JWT
  Better Auth openrouter.ai/api/frontend/v1/private/...
              GET  user/workspaces                    (Authorization: Bearer <jwt>)
              POST workspace-api-keys?workspace_id=.. => the sk-or-v1 key
  auth/key    openrouter.ai/api/v1/auth/key          => 200 means the key is live

The signup POST is NOT captcha-protected for a plain HTTP client with a Chrome TLS
fingerprint (curl_cffi), which is why headless Chromium used to get 403 but this does not.

    python or_fast.py 20                        # 20 accounts, 4 in parallel
    python or_fast.py 200 --workers 10
    python or_fast.py 5 --no-router --smoke     # keys only + prove one works
    python or_fast.py 50 --pool proxies_good.txt

Mail backends are chosen with --mail (see mail_providers.py). The scalable ones are `imap`
(plus-addressing on your own mailbox) and `cfworker` (your own domain + cf_mail_worker.js);
public temp-mail domains are already on the site's disposable list.
"""
import argparse
import json
import os
import random
import re
import string
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cr

import mail_providers as mailbox
from router_push import Router

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "accounts.txt")
WORK = os.path.join(BASE, "work")
GEO_CACHE = os.path.join(WORK, "proxy_geo.json")
STATS_FILE = os.path.join(WORK, "proxy_stats.json")

FAPI = "https://clerk.openrouter.ai/v1"
QP = "__clerk_api_version=2026-08-20&_clerk_js_version=6.41.0"
APP = "https://openrouter.ai"

PREF_CC = ["US", "GB", "CA", "AU", "DE", "FR", "NL", "SG", "JP", "IN", "BR", "TH"]
FIRST = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Avery", "Cameron",
         "Drew", "Quinn", "Rowan", "Skyler", "Emerson", "Finley", "Hayden"]
LAST = ["Miller", "Bennett", "Carter", "Hayes", "Brooks", "Reed", "Foster", "Sullivan", "Palmer",
        "Grant", "Hayward", "Lambert", "Walsh", "Doyle", "Mercer", "Ashton"]

LOCK = threading.Lock()
STATS = {"ok": 0, "fail": 0}


def log(*a):
    with LOCK:
        print(time.strftime("%H:%M:%S"), *a, flush=True)


def write_row(rec):
    fields = ("email", "password", "key", "status", "note", "proxy")
    row = "|".join(str(rec[k]).replace("|", "/").replace("\n", " ") for k in fields)
    with LOCK:
        with open(OUT, "a", encoding="utf-8") as fh:
            fh.write(row + "\n")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1)


# ------------------------------------------------------------------ proxy pool
def load_pool(path):
    pool = []
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        p = line.split(":")
        if len(p) < 2:
            continue
        pool.append({"host": p[0], "port": p[1],
                     "user": p[2] if len(p) > 2 else "",
                     "pw": ":".join(p[3:]) if len(p) > 3 else "",
                     "line": line, "key": f"{p[0]}:{p[1]}:{p[2] if len(p) > 2 else ''}"})
    return pool


def _geo_one(pp):
    url = (f"http://{pp['user']}:{pp['pw']}@{pp['host']}:{pp['port']}" if pp["user"]
           else f"http://{pp['host']}:{pp['port']}")
    px = {"http": url, "https": url}
    try:
        j = cr.get("https://ipinfo.io/json", proxies=px, impersonate="chrome", timeout=20).json()
        return pp["key"], {"cc": (j.get("country") or "").upper(), "ip": j.get("ip")}
    except Exception:
        try:
            j = cr.get("http://ip-api.com/json/?fields=countryCode,query", proxies=px,
                       impersonate="chrome", timeout=20).json()
            return pp["key"], {"cc": (j.get("countryCode") or "").upper(), "ip": j.get("query")}
        except Exception as exc:
            return pp["key"], {"cc": "", "err": type(exc).__name__}


def geolocate(pool):
    geo = load_json(GEO_CACHE, {})
    todo = [p for p in pool if p["key"] not in geo]
    if todo:
        with ThreadPoolExecutor(max_workers=16) as ex:
            for k, v in ex.map(_geo_one, todo):
                geo[k] = v
        save_json(GEO_CACHE, geo)
    return geo


def pick_proxy(pool, geo):
    stats = load_json(STATS_FILE, {})
    ranked = []
    for p in pool:
        s = stats.get(p["key"], {})
        cc = geo.get(p["key"], {}).get("cc") or "US"
        ranked.append((PREF_CC.index(cc) if cc in PREF_CC else 99,
                       s.get("blocked", 0), s.get("used", 0), random.random(), p, cc))
    ranked.sort(key=lambda x: x[:4])
    _, _, used, _, p, cc = ranked[0]
    with LOCK:
        stats.setdefault(p["key"], {})["used"] = used + 1
        save_json(STATS_FILE, stats)
    return p, cc


def mark_proxy(p, field):
    with LOCK:
        stats = load_json(STATS_FILE, {})
        s = stats.setdefault(p["key"], {})
        s[field] = s.get(field, 0) + 1
        save_json(STATS_FILE, stats)


# ------------------------------------------------------------------ helpers
def gen_pw():
    U, L, D, S = "ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "#!@$%&*?"
    return (random.choice(U) + random.choice(L) + random.choice(D) + random.choice(S)
            + "".join(random.choices(U + L + D, k=10)))


def deep_key(obj):
    if isinstance(obj, str):
        return obj if re.fullmatch(r"sk-or-v1-[A-Za-z0-9]{40,}", obj) else None
    if isinstance(obj, dict):
        for v in obj.values():
            k = deep_key(v)
            if k:
                return k
    if isinstance(obj, list):
        for v in obj:
            k = deep_key(v)
            if k:
                return k
    return None


def session_for(pp):
    if pp["user"]:
        proxy = f"http://{pp['user']}:{pp['pw']}@{pp['host']}:{pp['port']}"
    else:
        proxy = f"http://{pp['host']}:{pp['port']}"
    s = cr.Session(impersonate="chrome", proxies={"http": proxy, "https": proxy}, timeout=60)
    s.headers.update({"Origin": APP, "Referer": APP + "/", "Accept": "application/json"})
    return s


def retry(fn, tries=4, delay=2.0):
    """Repeat on 5xx/429/exception. Returns the last response either way."""
    r = None
    for i in range(tries):
        try:
            r = fn()
            if getattr(r, "status_code", 200) not in (429,) and getattr(r, "status_code", 200) < 500:
                return r
        except Exception:
            r = None
        time.sleep(delay * (i + 1))
    return r


def verify_key(key):
    try:
        return requests_get(f"{APP}/api/v1/auth/key", key)
    except Exception:
        return 0


def requests_get(url, key):
    r = cr.get(url, headers={"Authorization": f"Bearer {key}"}, impersonate="chrome", timeout=40)
    return r.status_code


def smoke(key):
    """Walk the free models until one answers - a 200 proves the key end to end."""
    try:
        models = cr.get(f"{APP}/api/v1/models", headers={"Authorization": f"Bearer {key}"},
                        impersonate="chrome", timeout=60).json().get("data", [])
    except Exception:
        models = []
    free = [m["id"] for m in models if str(m.get("pricing", {}).get("prompt")) in ("0", "0.0", "0.000000")]
    for model in free[:6]:
        try:
            r = cr.post(f"{APP}/api/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                        json={"model": model, "messages": [{"role": "user", "content": "ping"}],
                              "max_tokens": 5}, impersonate="chrome", timeout=60)
            if r.status_code == 200:
                return 200, model
        except Exception:
            continue
    return None, None


# ------------------------------------------------------------------ one account
def run_one(idx, args, pool, geo, router):
    """One account. If Clerk demands its bot-protection (`missing_fields: protect_check`,
    which a plain HTTP client cannot satisfy) the signup is retried on a different exit IP -
    the check is risk based, so another IP usually skips it. The inbox is reused."""
    rec = {"email": "", "password": "", "key": "", "status": "fail", "note": "", "proxy": ""}
    try:
        box = mailbox.open_inbox(args.mail)
    except Exception as exc:
        rec["note"] = f"mail: {type(exc).__name__}: {str(exc)[:90]}"
        write_row(rec)
        return rec

    for attempt in range(1, max(1, args.attempts) + 1):
        pw = gen_pw()
        rec.update(password=pw, key="", status="fail", note="")
        try:
            s, pp, cc = None, None, "-"
            if pool:
                for _ in range(5):                      # skip dead pool lines (402 / refused)
                    pp, cc = pick_proxy(pool, geo)
                    rec["proxy"] = f"{pp['host']}:{pp['port']}"
                    s = session_for(pp)
                    try:
                        s.get(APP + "/", timeout=40)     # sets __cf_bm / __cfuvid
                        s.get(f"{FAPI}/client?{QP}", timeout=40)
                        break
                    except Exception:
                        mark_proxy(pp, "failed")
                        s = None
                if s is None:
                    rec["note"] = "no working proxy in pool"
                    break
            else:
                s = cr.Session(impersonate="chrome", timeout=60)
                s.headers.update({"Origin": APP, "Referer": APP + "/", "Accept": "application/json"})
                s.get(APP + "/", timeout=45)
                s.get(f"{FAPI}/client?{QP}", timeout=45)
            rec["email"] = box.address
            log(f"[{idx}.{attempt}] {box.address} via {rec['proxy'] or 'direct'} [{cc}]")

            r = retry(lambda: s.post(f"{FAPI}/client/sign_ups?{QP}", data={
                "email_address": box.address, "password": pw, "first_name": random.choice(FIRST),
                "last_name": random.choice(LAST), "legal_accepted": "true"}))
            if r is not None and r.status_code == 403:
                if "temporary email" in r.text.lower():
                    mailbox.block_domain(box.address.split("@")[-1])
                    rec["note"] = "disposable domain, blacklisted"
                else:
                    rec["note"] = "sign_up 403 " + r.text[:80]
                    if pp:
                        mark_proxy(pp, "blocked")
                break
            sid = ((r.json() if r is not None else {}) or {}).get("response", {}).get("id")
            if not sid:
                rec["note"] = f"sign_up {getattr(r, 'status_code', '?')} {getattr(r, 'text', '')[:80]}"
                break

            pr = retry(lambda: s.patch(f"{FAPI}/client/sign_ups/{sid}?{QP}",
                                       data={"password": pw, "legal_accepted": "true"}))
            rr = retry(lambda: s.post(f"{FAPI}/client/sign_ups/{sid}/prepare_verification?{QP}",
                                      data={"strategy": "email_code"}))
            log(f"[{idx}.{attempt}] patch={getattr(pr, 'status_code', '?')} "
                f"prepare={getattr(rr, 'status_code', '?')}")

            def client_state():
                try:
                    c = (s.get(f"{FAPI}/client?{QP}").json().get("response") or {})
                    su = c.get("sign_up") or (c.get("sign_ups") or [{}])[0] or {}
                    v = (su.get("verifications") or {}).get("email_address") or {}
                    return {"status": su.get("status"), "email": v.get("status"),
                            "missing": su.get("missing_fields"),
                            "sess": c.get("last_active_session_id")
                            or ((c.get("sessions") or [{}])[0].get("id"))}
                except Exception:
                    return {}

            sess, resp = None, {}
            for round_ in range(3):
                code = box.poll(timeout=args.mail_timeout if round_ == 0 else 60)
                log(f"[{idx}.{attempt}] code={code}")
                if not code:
                    break
                j = retry(lambda: s.post(f"{FAPI}/client/sign_ups/{sid}/attempt_verification?{QP}",
                                         data={"strategy": "email_code", "code": code}))
                resp = ((j.json() if j is not None else {}) or {}).get("response") or {}
                sess = resp.get("created_session_id")
                if sess:
                    break
                st = client_state()
                log(f"[{idx}.{attempt}] state={st.get('status')} email={st.get('email')} "
                    f"missing={st.get('missing')}")
                sess = st.get("sess")
                if sess:
                    break
                if st.get("missing") and "protect_check" in st.get("missing"):
                    break                             # retry on another IP
                if j is None or "form_code_incorrect" not in (j.text or ""):
                    break
            if not sess:
                protect = "protect_check" in str(resp.get("missing_fields"))
                rec["note"] = "protect_check required" if protect else \
                    "verify not complete: " + str(resp.get("status"))
                if pp and protect:
                    mark_proxy(pp, "protection")
                continue

            tok = retry(lambda: s.post(f"{FAPI}/client/sessions/{sess}/tokens?{QP}", data={}), tries=5)
            jwt = ((tok.json() if tok is not None else {}) or {}).get("jwt")
            if not jwt:
                rec["note"] = f"no jwt ({getattr(tok, 'status_code', '?')})"
                continue
            H = {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}

            wsr = retry(lambda: s.get(f"{APP}/api/frontend/v1/private/user/workspaces", headers=H))
            ws = (((wsr.json() if wsr is not None else {}) or {}).get("data") or [{}])[0].get("id")
            if not ws:
                rec["note"] = f"no workspace ({getattr(wsr, 'status_code', '?')})"
                continue

            kr = retry(lambda: s.post(f"{APP}/api/frontend/v1/private/workspace-api-keys"
                                      f"?workspace_id={ws}", headers=H,
                                      data=json.dumps({"name": "k" + "".join(
                                          random.choices(string.ascii_lowercase, k=5))})))
            key = None
            if kr is not None:
                try:
                    key = deep_key(kr.json())
                except Exception:
                    key = None
                if not key:
                    m = re.search(r"sk-or-v1-[A-Za-z0-9]{40,}", kr.text)
                    key = m.group(0) if m else None
            if not key:
                rec["note"] = "no key in response: " + (getattr(kr, "text", "")[:120])
                continue

            status_code = verify_key(key)
            rec.update(key=key, status="ok" if status_code == 200 else "unverified",
                       note=f"auth/key {status_code} | attempt {attempt}")
            log(f"[{idx}.{attempt}] KEY {key[:20]}... auth/key={status_code}")
            if router:
                try:
                    rec["note"] += " | 9router " + router.push(key, box.address)
                except Exception as exc:
                    rec["note"] += f" | 9router {type(exc).__name__}"
            if args.smoke and status_code == 200:
                sc, model = smoke(key)
                rec["note"] += f" | smoke {sc or 'fail'}" + (f" {model}" if model else "")
            break
        except Exception as exc:
            rec["note"] = f"{type(exc).__name__}: {str(exc)[:110]}"
            log(f"[{idx}.{attempt}] error {rec['note']}")

    try:
        box.close()
    except Exception:
        pass

    write_row(rec)
    with LOCK:
        STATS["ok" if rec["status"] == "ok" else "fail"] += 1
    log(f"[{idx}] {rec['status']} {rec['note'][:80]}")
    return rec


def main():
    ap = argparse.ArgumentParser(description="OpenRouter farm over plain HTTP")
    ap.add_argument("count", nargs="?", type=int, default=1)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--pool", help="proxy list: host:port:user:pass per line")
    ap.add_argument("--mail", help="mail backend or order, e.g. imap | 'imap,temp-mail.io'")
    ap.add_argument("--no-router", action="store_true")
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--attempts", type=int, default=3,
                    help="signup retries per account on a new exit IP")
    ap.add_argument("--mail-timeout", type=int, default=180)
    args = ap.parse_args()

    os.makedirs(WORK, exist_ok=True)
    pool = load_pool(args.pool) if args.pool else []
    geo = geolocate(pool) if pool else {}
    if pool:
        from collections import Counter
        log(f"pool {len(pool)} | {dict(Counter((geo.get(p['key'], {}).get('cc') or '??') for p in pool))}")
    log(f"workers {args.workers} | mail {args.mail or 'auto'} | router {'on' if not args.no_router else 'off'}")

    router = None
    if not args.no_router:
        cand = Router()
        try:
            cand.list_connections()
            router = cand
            log(f"9router {cand.base} reachable")
        except Exception as exc:
            log(f"9router unavailable ({str(exc)[:70]}) - keys only in {OUT}")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(lambda i: run_one(i, args, pool, geo, router), range(args.count)))
    dt = time.time() - t0
    log(f"DONE ok={STATS['ok']} fail={STATS['fail']} in {dt:.0f}s "
        f"({dt / max(1, args.count):.1f}s/account) -> {OUT}")


if __name__ == "__main__":
    main()
