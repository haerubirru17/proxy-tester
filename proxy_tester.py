#!/usr/bin/env python3
"""
proxy_tester.py — Uji kualitas proxy berlapis.

Alur per proxy:
  1. Liveness   : connect + deteksi IP keluaran (via api.ipify.org)
  2. Geo        : negara/kota/ISP/tipe IP (ip-api.com)
  3. Latensi    : 5 request -> min/median/p95
  4. Stability  : 20 request -> error rate + perubahan IP (sticky check)
  5. Cloudflare : apakah IP dicurigai Cloudflare (proksi prediksi Turnstile)

Skor 0-100 = Liveness 20 + Geo 15 + Latensi 20 + Stability 30 + Cloudflare 15

Input  : satu atau lebih file .txt, satu proxy per baris.
         Format didukung: host:port | host:port:user:pass |
                          scheme://user:pass@host:port | scheme://host:port
Output : results.csv (metrik lengkap) + ringkasan terminal.

Pemakaian:
  proxy_tester.py file1.txt [file2.txt ...] [--threads 10] [--country US]
                  [--timeout 10] [--stability 20]
"""
import argparse
import concurrent.futures
import csv
import ipaddress
import re
import socket
import statistics
import sys
import time
from urllib.parse import urlparse

import requests

# ----------------------------- Konstanta ---------------------------------

IP_ECHO = "https://api.ipify.org?format=json"
GEO_API = "http://ip-api.com/json/?fields=status,country,countryCode,city,isp,org,hosting,proxy"
LAT_URL = "https://api.ipify.org?format=json"          # ringan, tanpa state
CF_TEST = "https://www.cloudflare.com/cdn-cgi/trace"   # endpoint ringan CF

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

GRADES = [
    (85, 100, "LAYAK SIGNUP"),
    (60, 84,  "CUKUP BROWSING"),
    (0,  59,  "BUANG"),
]

# ----------------------------- Parsing input -----------------------------

LINE_RE = re.compile(
    r"^(?:(?P<scheme>https?|socks5|socks4)://)?"
    r"(?:([^:@\s/]+):([^@\s/]+)@)?"
    r"([^:@\s/]+):(\d{2,5})$"
)

def parse_line(line: str):
    """Return dict proxy atau None. Baris kosong/# = skip."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    m = LINE_RE.match(line)
    if not m:
        return None
    scheme, user, pwd, host, port = m.groups()
    return {
        "raw": line,
        "scheme": (scheme or "http").lower(),
        "user": user or "",
        "pass": pwd or "",
        "host": host,
        "port": int(port),
    }

def parse_files(paths):
    """Baca semua file, gabung, dedup by host:port:user. Return (list, stats)."""
    seen, proxies, stats = set(), [], []
    for p in paths:
        try:
            lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
        except OSError as e:
            stats.append((p, 0, 0, f"tidak bisa dibaca: {e}"))
            continue
        ok = dup = bad = 0
        for ln in lines:
            pr = parse_line(ln)
            if pr is None:
                if ln.strip() and not ln.strip().startswith("#"):
                    bad += 1
                continue
            key = (pr["host"], pr["port"], pr["user"])
            if key in seen:
                dup += 1
                continue
            seen.add(key)
            pr["source"] = p
            proxies.append(pr)
            ok += 1
        stats.append((p, ok, dup, f"{bad} baris tak valid" if bad else "ok"))
    return proxies, stats

# ----------------------------- HTTP helpers ------------------------------

def proxy_dict(pr):
    url = f'{pr["scheme"]}://'
    if pr["user"]:
        url += f'{pr["user"]}:{pr["pass"]}@'
    url += f'{pr["host"]}:{pr["port"]}'
    return {"http": url, "https": url}

def session_for(pr, timeout):
    s = requests.Session()
    s.proxies = proxy_dict(pr)
    s.headers.update({"User-Agent": UA})
    # matikan retry otomatis — kita kelola sendiri
    a = requests.adapters.HTTPAdapter(max_retries=0)
    s.mount("http://", a)
    s.mount("https://", a)
    s.timeout = timeout
    return s

def req_json(s, url, timeout):
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

# ----------------------------- Tahap uji ---------------------------------

def t_liveness(s, timeout):
    """Return (ok, exit_ip, note)."""
    try:
        d = req_json(s, IP_ECHO, timeout)
        return True, d.get("ip", ""), ""
    except Exception as e:
        return False, "", _err(e)

def t_geo(s, timeout):
    try:
        d = req_json(s, GEO_API, timeout)
        if d.get("status") != "success":
            return {"country": "?", "geo_note": "geo api gagal"}
        return {
            "country": d.get("countryCode", "?"),
            "city": d.get("city", ""),
            "isp": d.get("isp", ""),
            "hosting": bool(d.get("hosting")),
            "flagged_proxy": bool(d.get("proxy")),
        }
    except Exception as e:
        return {"country": "?", "geo_note": _err(e)}

def t_latency(s, n=5, timeout=10):
    times = []
    for _ in range(n):
        t0 = time.monotonic()
        try:
            s.get(LAT_URL, timeout=timeout)
        except Exception:
            times.append(None)
            continue
        times.append(round((time.monotonic() - t0) * 1000))
    ok = [t for t in times if t is not None]
    if not ok:
        return {"lat_min": None, "lat_med": None, "lat_p95": None, "lat_err": n}
    p95 = sorted(ok)[min(len(ok) - 1, int(len(ok) * 0.95))]
    return {
        "lat_min": min(ok), "lat_med": int(statistics.median(ok)),
        "lat_p95": p95, "lat_err": n - len(ok),
    }

def t_stability(s, n=20, timeout=10):
    okc = 0
    ips = set()
    first_ip = None
    for _ in range(n):
        try:
            d = req_json(s, IP_ECHO, timeout)
            ip = d.get("ip", "")
            ips.add(ip)
            if first_ip is None:
                first_ip = ip
            okc += 1
        except Exception:
            pass
    rate = okc / n
    changed = len(ips - {""}) > 1
    return {
        "stab_ok": okc, "stab_total": n, "stab_rate": round(rate, 3),
        "ip_changed": changed, "exit_ips": ";".join(sorted(ips - {""})[:3]),
    }

def t_cloudflare(s, timeout):
    """Return (verdict, note). verdict: clean|suspicious|dead"""
    try:
        r = s.get(CF_TEST, timeout=timeout)
        if r.status_code == 200 and "fl=" in r.text:
            return "clean", ""
        return "suspicious", f"HTTP {r.status_code}"
    except Exception as e:
        return "suspicious", _err(e)

def _err(e):
    return type(e).__name__ + (f": {e}" if str(e) else "")

# ----------------------------- Skoring -----------------------------------

def score(r, want_country):
    """Return (total, breakdown dict)."""
    b = {}
    b["liveness"] = 20 if r["alive"] else 0
    if r["alive"]:
        g = 15
        if r.get("country") == "?":
            g = 5
        elif r.get("hosting"):
            g -= 5
        if want_country and r.get("country") != want_country:
            g -= 8
        if r.get("flagged_proxy"):
            g -= 3
        b["geo"] = max(0, g)

        lat = r.get("lat_med")
        if lat is None:
            b["latency"] = 0
        elif lat < 1000:
            b["latency"] = 20
        elif lat < 2000:
            b["latency"] = 15
        elif lat < 4000:
            b["latency"] = 8
        else:
            b["latency"] = 3

        b["stability"] = round(30 * r.get("stab_rate", 0))
        if r.get("ip_changed"):
            b["stability"] = max(0, b["stability"] - 5)

        b["cloudflare"] = {"clean": 15, "suspicious": 4}.get(r.get("cf"), 0)
    else:
        b.update({"geo": 0, "latency": 0, "stability": 0, "cloudflare": 0})
    return sum(b.values()), b

def grade(total):
    for lo, hi, name in GRADES:
        if lo <= total <= hi:
            return name
    return "BUANG"

# ----------------------------- Runner ------------------------------------

def tcp_prefilter(host, port, timeout=3):
    """Cek cepat: apakah port TCP bisa dihubungi. None = sehat, str = alasan mati."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as e:
        return type(e).__name__

def test_proxy(pr, args, prefilter=True):
    r = {
        "raw": pr["raw"], "scheme": pr["scheme"], "host": pr["host"],
        "port": pr["port"], "source": pr["source"],
    }
    # prefilter TCP murah (3s) — proxy mati tidak perlu menunggu timeout HTTP 10s
    if prefilter:
        dead = tcp_prefilter(pr["host"], pr["port"],
                             timeout=getattr(args, "tcp_timeout", 3))
        if dead:
            r.update({"alive": False, "exit_ip": "", "alive_note": f"tcp:{dead}",
                      "cf": "dead"})
            r["total"], r["breakdown"] = score(r, args.country)
            r["grade"] = grade(r["total"])
            return r
    s = session_for(pr, args.timeout)
    try:
        ok, ip, note = t_liveness(s, args.timeout)
        r.update({"alive": ok, "exit_ip": ip, "alive_note": note})
        if not ok:
            r["cf"] = "dead"
            r["total"], r["breakdown"] = score(r, args.country)
            r["grade"] = grade(r["total"])
            return r

        r.update(t_geo(s, args.timeout))
        r.update(t_latency(s, timeout=args.timeout))
        r.update(t_stability(s, n=args.stability, timeout=args.timeout))
        verdict, cf_note = t_cloudflare(s, args.timeout)
        r["cf"], r["cf_note"] = verdict, cf_note

        r["total"], r["breakdown"] = score(r, args.country)
        r["grade"] = grade(r["total"])
        return r
    finally:
        s.close()

CSV_COLS = [
    "raw", "source", "alive", "exit_ip", "country", "city", "isp", "hosting",
    "flagged_proxy", "lat_min", "lat_med", "lat_p95", "lat_err",
    "stab_ok", "stab_total", "stab_rate", "ip_changed", "cf", "total",
    "grade", "note",
]

def flatten(r):
    note = "; ".join(
        f"{k}={v}" for k, v in
        (("alive", r.get("alive_note")), ("geo", r.get("geo_note")),
         ("cf", r.get("cf_note"))) if v
    )
    return {
        "raw": r["raw"], "source": r["source"],
        "alive": r["alive"], "exit_ip": r.get("exit_ip", ""),
        "country": r.get("country", ""), "city": r.get("city", ""),
        "isp": r.get("isp", ""), "hosting": r.get("hosting", ""),
        "flagged_proxy": r.get("flagged_proxy", ""),
        "lat_min": r.get("lat_min", ""), "lat_med": r.get("lat_med", ""),
        "lat_p95": r.get("lat_p95", ""), "lat_err": r.get("lat_err", ""),
        "stab_ok": r.get("stab_ok", ""), "stab_total": r.get("stab_total", ""),
        "stab_rate": r.get("stab_rate", ""), "ip_changed": r.get("ip_changed", ""),
        "cf": r.get("cf", ""), "total": r["total"], "grade": r["grade"],
        "note": note,
    }

def is_ip(s):
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False

def main():
    ap = argparse.ArgumentParser(description="Uji kualitas proxy berlapis")
    ap.add_argument("files", nargs="+", help="file .txt berisi proxy (bisa >1)")
    ap.add_argument("--threads", type=int, default=10)
    ap.add_argument("--country", default="", help="kode negara yg diharapkan (mis. US)")
    ap.add_argument("--timeout", type=int, default=10)
    ap.add_argument("--stability", type=int, default=20, help="jumlah req stability")
    ap.add_argument("-o", "--output", default="results.csv")
    args = ap.parse_args()

    proxies, stats = parse_files(args.files)
    print("=" * 62)
    print("PROXY TESTER — uji berlapis (liveness→geo→latensi→stabilitas→CF)")
    print("=" * 62)
    for p, ok, dup, note in stats:
        print(f"  {p}: {ok} proxy baru, {dup} duplikat dilewati ({note})")
    if not proxies:
        print("Tidak ada proxy valid. Keluar.")
        sys.exit(1)
    print(f"Total diuji: {len(proxies)} | thread={args.threads} "
          f"| country={args.country or '-'} | stability={args.stability} req")
    print("-" * 62)

    t0 = time.monotonic()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = {ex.submit(test_proxy, pr, args): pr for pr in proxies}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            r = fut.result()
            results.append(r)
            tag = "✓" if r["alive"] else "✗"
            extra = (f'{r.get("country","?")} {r.get("lat_med","-")}ms '
                     f'stab={r.get("stab_rate","-")}') if r["alive"] else r.get("alive_note","")
            print(f'  [{i:>3}/{len(proxies)}] {tag} {r["host"]}:{r["port"]:<5} '
                  f'skor={r["total"]:>3} {r["grade"]:<15} {extra}')
    elapsed = time.monotonic() - t0

    results.sort(key=lambda r: -r["total"])
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for r in results:
            w.writerow(flatten(r))

    alive = [r for r in results if r["alive"]]
    print("-" * 62)
    print(f"SELESAI dalam {elapsed:.0f}s — {len(alive)}/{len(results)} hidup "
          f"| hasil: {args.output}")
    for lo, hi, name in GRADES:
        n = sum(1 for r in results if r["grade"] == name)
        print(f"  {name:<15}: {n}")
    top = [r for r in results if r["grade"] == "LAYAK SIGNUP"][:5]
    if top:
        print("  Top proxy:")
        for r in top:
            print(f'    {r["host"]}:{r["port"]} skor={r["total"]} '
                  f'{r.get("country","?")} {r.get("lat_med","?")}ms '
                  f'stab={r.get("stab_rate","?")}')
    if any(is_ip(r["host"]) for r in results):
        pass  # host bisa domain atau IP — sama-sama didukung

if __name__ == "__main__":
    main()
