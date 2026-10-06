#!/usr/bin/env python3
"""
server.py — backend proxy-tester.

FastAPI wrapper untuk proxy_tester.py:
  POST /api/test?token=...   upload 1+ file .txt proxy + opsi, mulai job uji
  GET  /api/status?token=...&job=...   progres job (polling)
  GET  /api/results?token=...&job=...  hasil lengkap (JSON) atau CSV (?format=csv)
  GET  /health                    tanpa auth (untuk monitoring)

Auth: query param token, dibandingkan dengan env PROXY_TOKEN.
TLS:  env SSL_CERT / SSL_KEY (uvicorn).
"""
import argparse
import csv
import io
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import FastAPI, HTTPException, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from proxy_tester import (parse_line, test_proxy, flatten, score, grade)

app = FastAPI(title="proxy-tester backend", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://[a-z0-9-]+\.github\.io",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

TOKEN = os.environ.get("PROXY_TOKEN", "")
if not TOKEN:
    raise RuntimeError("env PROXY_TOKEN wajib diisi")

JOBS: dict[str, dict] = {}          # job_id -> state
JOBS_LOCK = threading.Lock()
MAX_JOB_AGE = 3600 * 6             # buang job > 6 jam


def _check(token: str):
    if token != TOKEN:
        raise HTTPException(401, "token salah")


def _gc_jobs():
    now = time.monotonic()
    with JOBS_LOCK:
        for jid in [j for j, s in JOBS.items() if now - s["ts"] > MAX_JOB_AGE]:
            del JOBS[jid]


def _run_job(job_id: str, proxies: list, opts: dict):
    state = JOBS[job_id]
    state["total"] = len(proxies)
    # test_proxy mengharapkan objek ber-atribut (argparse.Namespace), bukan dict
    ns = argparse.Namespace(
        threads=opts["threads"], country=opts["country"],
        stability=opts["stability"], timeout=opts["timeout"],
        tcp_timeout=opts.get("tcp_timeout", 3),
    )
    try:
        with ThreadPoolExecutor(max_workers=opts["threads"]) as ex:
            futs = {ex.submit(test_proxy, pr, ns): pr for pr in proxies}
            for i, fut in enumerate(as_completed(futs), 1):
                if state.get("stop"):
                    for f in futs:
                        f.cancel()
                    state["stopped"] = True
                    return
                r = fut.result()
                state["results"].append(r)
                state["done"] = i
                state["log"].append({
                    "host": f'{r["host"]}:{r["port"]}',
                    "alive": r["alive"],
                    "total": r["total"],
                    "grade": r["grade"],
                    "note": r.get("country") if r["alive"] else r.get("alive_note", ""),
                })
    except Exception as e:  # jangan biarkan job menggantung tanpa jejak
        state["error"] = f"{type(e).__name__}: {e}"
        state["done"] = state["total"]


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/test")
async def start_test(
    token: str = Form(...),
    files: list[UploadFile] = Form(...),
    threads: int = Form(10),
    country: str = Form(""),
    stability: int = Form(20),
    timeout_s: int = Form(10),
):
    _check(token)
    _gc_jobs()

    # tulis upload ke file sementara agar bisa pakai parse_files biasa
    import tempfile
    paths = []
    for f in files:
        data = await f.read()
        if not data:
            continue
        tmp = tempfile.NamedTemporaryFile(
            suffix=".txt", prefix="upload_", delete=False)
        tmp.write(data)
        tmp.close()
        paths.append(tmp.name)
    if not paths:
        raise HTTPException(400, "tidak ada file terkirim")

    from proxy_tester import parse_files
    proxies, stats = parse_files(paths)
    for p in paths:
        try:
            os.unlink(p)
        except OSError:
            pass
    if not proxies:
        raise HTTPException(400, "tidak ada proxy valid di file terkirim")

    job_id = uuid.uuid4().hex[:12]
    state = {
        "ts": time.monotonic(), "total": 0, "done": 0,
        "results": [], "log": [], "file_stats": stats,
        "opts": {"threads": min(threads, 50), "country": country,
                 "stability": max(1, stability), "timeout": max(2, timeout_s)},
    }
    with JOBS_LOCK:
        JOBS[job_id] = state
    threading.Thread(
        target=_run_job, args=(job_id, proxies, state["opts"]), daemon=True
    ).start()
    return {"ok": True, "job": job_id, "proxies": len(proxies),
            "file_stats": [{"file": s[0], "ok": s[1], "dup": s[2], "note": s[3]}
                           for s in stats]}


@app.get("/api/status")
def status(token: str, job: str):
    _check(token)
    state = JOBS.get(job)
    if not state:
        raise HTTPException(404, "job tidak ditemukan")
    return {
        "done": state["done"], "total": state["total"],
        "log": state["log"][-50:],
        "finished": state["total"] > 0 and (state["done"] >= state["total"]
                                            or state.get("stopped")),
        "stopped": state.get("stopped", False),
        "error": state.get("error"),
    }


@app.post("/api/stop")
def stop(token: str = Form(...), job: str = Form(...)):
    _check(token)
    state = JOBS.get(job)
    if not state:
        raise HTTPException(404, "job tidak ditemukan")
    state["stop"] = True
    return {"ok": True}


@app.get("/api/results")
def results(token: str, job: str, format: str = "json"):
    _check(token)
    state = JOBS.get(job)
    if not state:
        raise HTTPException(404, "job tidak ditemukan")
    rows = [flatten(r) for r in
            sorted(state["results"], key=lambda r: -r["total"])]
    if format == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else
                           ["raw", "grade"])
        w.writeheader()
        w.writerows(rows)
        buf.seek(0)
        return StreamingResponse(
            buf, media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="proxy_results_{job}.csv"'})
    return JSONResponse({
        "finished": state["total"] > 0 and state["done"] >= state["total"],
        "count": len(rows), "results": rows,
    })


if __name__ == "__main__":
    import uvicorn
    ssl_cert = os.environ.get("SSL_CERT")
    ssl_key = os.environ.get("SSL_KEY")
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8443")),
                ssl_certfile=ssl_cert, ssl_keyfile=ssl_key)
