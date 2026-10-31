"""mail_providers.py - pluggable inbox backends for the OpenRouter farm (HTTP build).

Backends
    imap          your own mailbox with plus-addressing            <- reliable
    cfworker      own domain + Cloudflare Email Worker            <- unlimited volume
    custom        your own HTTP API                               <- whatever you run
    temp-mail.io  public, keyless                                 <- usually refused
    tempmail.plus public, keyless                                 <- usually refused
    mailgw        public, keyless (mail.gw)
    guerrilla     public, keyless

WHY THE PUBLIC ONES ARE LAST
    OpenRouter rejects disposable domains at signup:
        "Temporary email services are not supported. Please use your regular email address."
    Measured 2026-10 over plain HTTP (1 signup POST per domain, rotating proxies):
      refused: mailto.plus, fexpost.com, fexbox.org, rover.info, tempmail.plus, yopmail.com,
               maildrop.cc, tmpmail.net/org, moakt.cc, mohmal.com, tempmail.email,
               luckymail.org, inboxes.com, harakirimail.com, discard.email, mailcatch.com,
               tempr.email, mailsac.com, 10minutemail.com, mailmetrash.com, getnada.com,
               emailondeck.com, mailinator.com, sharklasers.com, guerrillamail.info,
               emlhub.com, emlpro.com, 10mail.org, dropmail.me, mail.tm, mail.gw,
               1secmail.com, vjuum.com, laafd.com, txcct.com, lucktaro.com (tempmail.lol),
               tanpony.com, yzcalo.com, guerrillamailblock.com
      accepted: gmail.com, outlook.com, and alias domains anonaddy.me / mozmail.com / 33mail.com
    tempmail.lol also answers "429 Rate limited (free)" after two inboxes, so it cannot carry volume.

    So: `imap` (plus addressing on a normal mailbox) or `cfworker` (your own domain) are the only
    two that scale. A domain judged disposable is remembered in blocked_domains.json.
"""
import email
import imaplib
import threading
import json
import os
import random
import re
import string
import time

import requests

BASE = os.path.dirname(os.path.abspath(__file__))
BLOCK_FILE = os.path.join(BASE, "blocked_domains.json")

BUILTIN_BLOCKED = {
    "mailto.plus", "fexpost.com", "fexbox.org", "rover.info", "tempmail.plus",
    "yopmail.com", "maildrop.cc", "tmpmail.net", "tmpmail.org", "moakt.cc", "mohmal.com",
    "tempmail.email", "luckymail.org", "inboxes.com", "harakirimail.com", "discard.email",
    "mailcatch.com", "tempr.email", "mailsac.com", "10minutemail.com", "mailmetrash.com",
    "getnada.com", "emailondeck.com", "mailinator.com", "sharklasers.com", "guerrillamail.info",
    "guerrillamailblock.com", "emlhub.com", "emlpro.com", "10mail.org", "dropmail.me",
    "mail.tm", "maxxspace.com", "mail.gw", "1secmail.com", "vjuum.com", "laafd.com",
    "txcct.com", "lucktaro.com", "tanpony.com", "yzcalo.com",
}


_RL_LOCK = threading.Lock()
_RL_LAST = [0.0]


def pace(min_gap):
    """Global spacing for providers with a per-minute quota (Kirin: 30/min, most: 10/min).
    Set OR_MAIL_MIN_GAP; parallel workers then share one budget instead of 429-ing each other."""
    gap = min_gap - (time.time() - _RL_LAST[0])
    if gap > 0:
        time.sleep(gap)
    _RL_LAST[0] = time.time()


def mail_gap():
    try:
        return float(os.environ.get("OR_MAIL_MIN_GAP", "2.0"))
    except ValueError:
        return 2.0


def rand_tag(n=10):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def load_blocked():
    try:
        with open(BLOCK_FILE, encoding="utf-8") as fh:
            return set(json.load(fh)) | BUILTIN_BLOCKED
    except Exception:
        return set(BUILTIN_BLOCKED)


def block_domain(domain):
    domain = (domain or "").lower().strip()
    if not domain:
        return
    try:
        with open(BLOCK_FILE, encoding="utf-8") as fh:
            cur = set(json.load(fh))
    except Exception:
        cur = set()
    cur.add(domain)
    with open(BLOCK_FILE, "w", encoding="utf-8") as fh:
        json.dump(sorted(cur), fh, indent=1)


def code_from_text(blob):
    """6-digit verification code out of a mail body / subject / preview."""
    blob = re.sub(r"<style.*?</style>", " ", blob or "", flags=re.S | re.I)
    blob = re.sub(r"<[^>]+>", " ", blob)
    blob = re.sub(r"#[0-9a-fA-F]{3,8}\b", " ", blob)
    cand = [c for c in re.findall(r"(?<!\d)(\d{6})(?!\d)", blob) if len(set(c)) > 1]
    return cand[0] if cand else None


class Inbox:
    """address + poll() -> next unseen code."""

    address = ""
    backend = "base"
    used = None

    def poll(self, timeout=180, interval=3):
        if self.used is None:
            self.used = set()
        end = time.time() + timeout
        while time.time() < end:
            try:
                code = self._check()
            except Exception:
                code = None
            if code and code not in self.used:
                self.used.add(code)
                return code
            time.sleep(interval)
        return None

    def _check(self):
        raise NotImplementedError

    def close(self):
        pass


class ImapInbox(Inbox):
    """Own mailbox, plus-addressed (`you+tag@domain`) - one mailbox, unlimited addresses.

        OR_IMAP_HOST   imap.gmail.com | outlook.office365.com | imap.yandex.com | imap.fastmail.com
        OR_IMAP_USER   you@gmail.com
        OR_IMAP_PASS   app password (Gmail: 2FA -> App passwords), NOT the account password
        OR_IMAP_PORT   993
    """

    backend = "imap"

    def __init__(self):
        self.host = os.environ["OR_IMAP_HOST"]
        self.port = int(os.environ.get("OR_IMAP_PORT", "993"))
        self.user = os.environ["OR_IMAP_USER"]
        self.password = os.environ["OR_IMAP_PASS"]
        local, _, domain = self.user.partition("@")
        self.local_tag = rand_tag()
        self.address = f"{local}+{self.local_tag}@{domain}"

    def _mail(self):
        m = imaplib.IMAP4_SSL(self.host, self.port)
        m.login(self.user, self.password)
        m.select("INBOX")
        return m

    def _check(self):
        m = None
        try:
            m = self._mail()
            typ, data = m.search(None, "UNSEEN")
            for mid in (data[0].split()[-8:] if data and data[0] else []):
                typ, raw = m.fetch(mid, "(RFC822)")
                if typ != "OK" or not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])
                hdr = " ".join([str(msg.get("To", "")), str(msg.get("Delivered-To", ""))]).lower()
                if self.local_tag not in hdr and self.user.lower() not in hdr:
                    continue
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            try:
                                body += " " + part.get_payload(decode=True).decode(
                                    part.get_content_charset() or "utf-8", "ignore")
                            except Exception:
                                pass
                else:
                    try:
                        body = msg.get_payload(decode=True).decode(
                            msg.get_content_charset() or "utf-8", "ignore")
                    except Exception:
                        body = str(msg.get_payload())
                code = code_from_text(body)
                if code:
                    try:
                        m.store(mid, "+FLAGS", "\\Seen")
                    except Exception:
                        pass
                    return code
        finally:
            if m:
                try:
                    m.logout()
                except Exception:
                    pass
        return None


class CFWorkerBox(Inbox):
    """Own domain behind the Cloudflare Email Worker in cf_mail_worker.js.

        CF_MAIL_URL     https://mail-catcher.<you>.workers.dev
        CF_MAIL_KEY     the worker SECRET
        CF_MAIL_DOMAIN  yourdomain.tld
    """

    backend = "cfworker"

    def __init__(self):
        self.base = os.environ["CF_MAIL_URL"].rstrip("/")
        self.key = os.environ["CF_MAIL_KEY"]
        self.domain = os.environ["CF_MAIL_DOMAIN"].lstrip("@")
        self.address = f"x{rand_tag(11)}@{self.domain}"

    def _check(self):
        r = requests.get(f"{self.base}/code", params={"to": self.address, "key": self.key}, timeout=25)
        codes = (r.json() or {}).get("codes") or []
        if not codes:
            return None
        for c in codes:
            if c not in (self.used or set()):
                return c
        return None


class CustomInbox(Inbox):
    """Your own mail API (2-step APIs supported).

        OR_MAIL_CREATE_URL  POST -> {"email": "...", ...}; extra keys are replayed as query params
        OR_MAIL_POLL_URL    GET ?email=&<extra> -> {"messages":[{"id","text"}], "texts":[...],
                                                    "emails":[{"id"}], "mail_list":[{"mail_id"}]}
        OR_MAIL_FETCH_URL   optional GET ?id=&email= -> full body as JSON with a text field
        OR_MAIL_HEADERS     optional JSON headers for every call
    """

    backend = "custom"

    def __init__(self):
        self.headers = json.loads(os.environ.get("OR_MAIL_HEADERS") or "{}")
        j = requests.post(os.environ["OR_MAIL_CREATE_URL"], headers=self.headers, json={}, timeout=30).json()
        self.address = j.pop("email")
        self.extra = {k: str(v) for k, v in j.items()}
        self.poll_url = os.environ["OR_MAIL_POLL_URL"]
        self.fetch_url = os.environ.get("OR_MAIL_FETCH_URL")

    def _body(self, mid):
        if not self.fetch_url:
            return ""
        with _RL_LOCK:
            pace(mail_gap())
        j = requests.get(self.fetch_url, params={"id": mid, "email": self.address, **self.extra},
                         headers=self.headers, timeout=25).json()
        return " ".join(str(v) for v in j.values() if isinstance(v, str)) if isinstance(j, dict) else str(j)

    def _check(self):
        with _RL_LOCK:
            pace(mail_gap())
        j = requests.get(self.poll_url, params={"email": self.address, **self.extra},
                         headers=self.headers, timeout=25).json()
        seen = self.used if self.used is not None else set()
        blobs = [m.get("text") or m.get("body") or "" for m in (j.get("messages") or [])]
        blobs += j.get("texts") or []
        for msg in (j.get("messages") or []):
            if msg.get("id") and msg["id"] not in seen:
                seen.add(msg["id"])
                blobs.append(self._body(msg["id"]))
        for msg in (j.get("emails") or j.get("mail_list") or []):
            mid = msg.get("id") or msg.get("mail_id")
            if mid and mid not in seen:
                seen.add(mid)
                blobs.append(self._body(mid))
        for b in blobs:
            code = code_from_text(b)
            if code:
                return code
        return None


class TempMailIO(Inbox):
    backend = "temp-mail.io"
    API = "https://api.internal.temp-mail.io/api/v3"

    def __init__(self):
        j = requests.post(f"{self.API}/email/new",
                          json={"min_name_length": 10, "max_name_length": 10}, timeout=25).json()
        self.address, self.token = j["email"], j["token"]

    def _check(self):
        j = requests.get(f"{self.API}/email/{self.address}/messages", timeout=25).json()
        for m in j:
            code = code_from_text((m.get("subject") or "") + " " + (m.get("body_text") or "")
                                  + " " + (m.get("body_html") or ""))
            if code:
                return code
        return None


class TempMailPlus(Inbox):
    backend = "tempmail.plus"
    API = "https://tempmail.plus/api"
    DOMAINS = ["mailto.plus", "fexpost.com", "fexbox.org", "rover.info"]

    def __init__(self):
        self.address = f"{rand_tag(10)}@{random.choice(self.DOMAINS)}"
        self.epin = ""

    def _check(self):
        j = requests.get(f"{self.API}/mails", params={"email": self.address, "epin": self.epin,
                                                      "first_id": 0, "limit": 20}, timeout=25).json()
        for m in j.get("mail_list") or []:
            d = requests.get(f"{self.API}/mails/{m['mail_id']}",
                             params={"email": self.address, "epin": self.epin}, timeout=25).json()
            code = code_from_text((d.get("subject") or "") + " " + (d.get("text") or "")
                                  + " " + (d.get("html") or ""))
            if code:
                return code
        return None


class MailGW(Inbox):
    backend = "mailgw"
    API = "https://api.mail.gw"

    def __init__(self):
        doms = requests.get(f"{self.API}/domains", timeout=25).json()["hydra:member"]
        self.address = f"{rand_tag()}@{random.choice(doms)['domain']}"
        self.password = rand_tag(14)
        requests.post(f"{self.API}/accounts", json={"address": self.address,
                                                    "password": self.password}, timeout=25)
        self.token = requests.post(f"{self.API}/token", json={"address": self.address,
                                                              "password": self.password},
                                   timeout=25).json()["token"]

    def _check(self):
        h = {"Authorization": f"Bearer {self.token}"}
        j = requests.get(f"{self.API}/messages", headers=h, timeout=25).json()
        for m in j.get("hydra:member") or []:
            d = requests.get(f"{self.API}/messages/{m['id']}", headers=h, timeout=25).json()
            code = code_from_text((d.get("subject") or "") + " " + (d.get("text") or "")
                                  + " " + (d.get("html") or ""))
            if code:
                return code
        return None


class Guerrilla(Inbox):
    backend = "guerrilla"
    API = "https://api.guerrillamail.com/ajax.php"

    def __init__(self):
        j = requests.get(self.API, params={"f": "get_email_address", "ip": "127.0.0.1",
                                           "agent": "Mozilla/5.0"}, timeout=25).json()
        self.address, self.sid = j["email_addr"], j["sid_token"]

    def _check(self):
        j = requests.get(self.API, params={"f": "get_email_list", "offset": 0,
                                           "sid_token": self.sid}, timeout=25).json()
        for m in j.get("list") or []:
            d = requests.get(self.API, params={"f": "fetch_email", "email_id": m["mail_id"],
                                               "sid_token": self.sid}, timeout=25).json()
            code = code_from_text((d.get("mail_subject") or "") + " " + str(d.get("mail_body") or ""))
            if code:
                return code
        return None


BACKENDS = {
    "imap": ImapInbox,
    "cfworker": CFWorkerBox,
    "custom": CustomInbox,
    "temp-mail.io": TempMailIO,
    "tempmail.plus": TempMailPlus,
    "mailgw": MailGW,
    "guerrilla": Guerrilla,
}

DEFAULT_ORDER = ["cfworker", "imap", "custom", "temp-mail.io", "tempmail.plus", "mailgw", "guerrilla"]

NEEDS = {
    "imap": ("OR_IMAP_HOST", "OR_IMAP_USER", "OR_IMAP_PASS"),
    "cfworker": ("CF_MAIL_URL", "CF_MAIL_KEY", "CF_MAIL_DOMAIN"),
    "custom": ("OR_MAIL_CREATE_URL", "OR_MAIL_POLL_URL"),
}


def backend_ready(name):
    return all(os.environ.get(k) for k in NEEDS.get(name, ()))


def open_inbox(prefer=None, blocked=None, tries=10):
    """First backend that answers with a non-blocklisted address."""
    blocked = blocked if blocked is not None else load_blocked()
    order = [b.strip() for b in (prefer or "").split(",") if b.strip()] or DEFAULT_ORDER
    errors = []
    for name in order:
        if name not in BACKENDS:
            errors.append(f"{name}: unknown")
            continue
        if not backend_ready(name):
            errors.append(f"{name}: env missing")
            continue
        for _ in range(tries):
            try:
                box = BACKENDS[name]()
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}")
                break
            dom = box.address.split("@")[-1].lower()
            if dom in blocked:
                errors.append(f"{name}: {dom} blocked")
                continue
            box.backend = name
            return box
    raise RuntimeError("no usable mail backend: " + "; ".join(errors[-4:]))
