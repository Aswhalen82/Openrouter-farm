"""proxy_check.py - filter a proxy list to the ones that actually work.

Lines are `host:port:user:pass` or `host:port`. Two checks, both through the proxy:
    1. CONNECT openrouter.ai:443 must answer 200   (402 = out of quota, 429 = busy, timeouts = dead)
    2. https://ipinfo.io/json for exit IP / country

    python proxy_check.py proxies.txt proxies_good.txt [--workers 24]
"""
import argparse
import base64
import socket
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from curl_cffi import requests as cr


def parse(line):
    p = line.strip().split(":")
    if len(p) < 2:
        return None
    return p[0], p[1], (p[2] if len(p) > 2 else ""), (":".join(p[3:]) if len(p) > 3 else "")


def connect_status(line):
    p = parse(line)
    if not p:
        return line, "bad_line"
    host, port, user, pw = p
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode() if user else ""
    try:
        s = socket.create_connection((host, int(port)), timeout=12)
        req = "CONNECT openrouter.ai:443 HTTP/1.1\r\nHost: openrouter.ai:443\r\n"
        if auth:
            req += f"Proxy-Authorization: Basic {auth}\r\n"
        s.sendall((req + "\r\n").encode())
        first = s.recv(200).split(b"\r\n")[0].decode(errors="replace")
        s.close()
        bits = first.split()
        return line, (bits[1] if len(bits) > 1 else "???")
    except Exception as exc:
        return line, type(exc).__name__


def geo(line):
    host, port, user, pw = parse(line)
    url = f"http://{user}:{pw}@{host}:{port}" if user else f"http://{host}:{port}"
    try:
        j = cr.get("https://ipinfo.io/json", proxies={"http": url, "https": url},
                   impersonate="chrome", timeout=20).json()
        return {"ip": j.get("ip"), "cc": (j.get("country") or "").upper(),
                "org": (j.get("org") or "")[:38]}
    except Exception as exc:
        return {"error": type(exc).__name__}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--workers", type=int, default=24)
    a = ap.parse_args()

    lines = [l.strip() for l in open(a.src, encoding="utf-8") if l.strip() and not l.startswith("#")]
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        status = list(ex.map(connect_status, lines))
    print("connect:", dict(Counter(code for _, code in status)))

    alive = [line for line, code in status if code == "200"]
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        gs = list(ex.map(geo, alive))

    for line, g in sorted(zip(alive, gs), key=lambda x: (x[1].get("cc") or "zz")):
        host = ":".join(line.split(":")[:2])
        print(f"{host:24} {g.get('cc',''):3} {g.get('ip',''):16} {g.get('org','')}")

    print("countries:", dict(Counter(g.get("cc", "") for g in gs)))
    with open(a.dst, "w", encoding="utf-8") as fh:
        fh.write("\n".join(alive) + "\n")
    print(f"GOOD {len(alive)}/{len(lines)} -> {a.dst}")


if __name__ == "__main__":
    sys.exit(main())
