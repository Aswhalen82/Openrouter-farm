"""router_push.py - push OpenRouter keys into a stock 9Router instance.

OpenRouter is a built-in provider template in 9Router, so a "connection" is all that is needed -
do not create a custom OpenAI-compatible node (that leaves a duplicate provider in the dashboard).

    POST /api/providers              {"provider":"openrouter","name":"...","apiKey":"sk-or-..."}
    GET  /api/providers              -> {"connections":[{id,name,provider,testStatus,...}]}
    POST /api/providers/<id>/test    -> {"valid":true|false}   (a fresh row is "unknown" until this)

Auth, first that applies:
    NINE_ROUTER_TOKEN      value for the x-9r-cli-token header (dashboard -> CLI)
    NINE_ROUTER_PASSWORD   dashboard password; the script logs in and keeps the session cookie
    neither                works when the dashboard runs with requireLogin disabled on localhost

    NINE_ROUTER_URL        default http://localhost:20128
    NINE_ROUTER_PREFIX     connection name prefix, default "or-"
"""
import os
import re

import requests


class Router:
    def __init__(self, base=None, token=None, password=None, prefix=None):
        self.base = (base or os.environ.get("NINE_ROUTER_URL") or "http://localhost:20128").rstrip("/")
        self.token = token if token is not None else os.environ.get("NINE_ROUTER_TOKEN")
        self.password = password if password is not None else os.environ.get("NINE_ROUTER_PASSWORD")
        self.prefix = prefix or os.environ.get("NINE_ROUTER_PREFIX") or "or-"
        self.session = requests.Session()
        self._logged_in = False

    def _headers(self):
        return {"x-9r-cli-token": self.token} if self.token else {}

    def login(self):
        if self._logged_in or not self.password:
            return self._logged_in
        r = self.session.post(f"{self.base}/api/auth/login", json={"password": self.password}, timeout=20)
        self._logged_in = r.status_code == 200
        if not self._logged_in:
            raise RuntimeError(f"9Router login failed: {r.status_code} {r.text[:120]}")
        return True

    def _request(self, method, path, **kw):
        kw.setdefault("timeout", 30)
        r = self.session.request(method, f"{self.base}{path}", headers=self._headers(), **kw)
        if r.status_code == 401 and self.password and not self._logged_in:
            self.login()
            r = self.session.request(method, f"{self.base}{path}", headers=self._headers(), **kw)
        return r

    def list_connections(self):
        r = self._request("GET", "/api/providers")
        if r.status_code != 200:
            raise RuntimeError(f"9Router list failed: {r.status_code} {r.text[:160]}")
        return (r.json() or {}).get("connections") or []

    def find(self, name):
        for c in self.list_connections():
            if c.get("name") == name:
                return c
        return None

    def add_key(self, api_key, name_hint=""):
        local = re.sub(r"[^a-z0-9._-]+", "", (name_hint or "").split("@")[0].lower()) or "acct"
        name = f"{self.prefix}{local}"
        existing = self.find(name)
        if existing:
            return name, existing.get("id"), "exists"

        r = self._request("POST", "/api/providers",
                          json={"provider": "openrouter", "name": name, "apiKey": api_key})
        if r.status_code in (200, 201):
            return name, ((r.json() or {}).get("connection") or {}).get("id"), "added"
        if r.status_code == 409:
            name = f"{name}-{abs(hash(api_key)) % 1000:03d}"
            r = self._request("POST", "/api/providers",
                              json={"provider": "openrouter", "name": name, "apiKey": api_key})
            if r.status_code in (200, 201):
                return name, ((r.json() or {}).get("connection") or {}).get("id"), "added"
        raise RuntimeError(f"9Router add failed: {r.status_code} {r.text[:160]}")

    def test(self, conn_id):
        if not conn_id:
            return None
        r = self._request("POST", f"/api/providers/{conn_id}/test")
        if r.status_code != 200:
            return None
        return bool((r.json() or {}).get("valid"))

    def push(self, api_key, name_hint=""):
        name, cid, action = self.add_key(api_key, name_hint)
        ok = self.test(cid) if cid else None
        return f"{action}+{'active' if ok else 'untested'}"


if __name__ == "__main__":
    import sys
    rt = Router()
    if len(sys.argv) > 1:
        print(rt.push(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""))
    else:
        for c in rt.list_connections():
            print(c.get("name"), c.get("provider"), c.get("testStatus"))
