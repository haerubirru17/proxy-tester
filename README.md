# Proxy Tester

Uji kualitas proxy berlapis dengan UI web: **liveness → geo → latensi →
stabilitas → Cloudflare**. Skor 0–100 per proxy, grade otomatis
(`LAYAK SIGNUP` / `CUKUP BROWSING` / `BUANG`), hasil bisa diunduh CSV.

**UI:** https://haerubirru17.github.io/proxy-tester/
**Backend:** VPS UpCloud (`https://213.163.196.178.sslip.io:8443`, token required)

## Arsitektur

```
GitHub Pages (docs/)  ──HTTPS──▶  FastAPI server.py di VPS :8443
  upload N file .txt                ▶ ThreadPoolExecutor (10 thread)
  progress polling 1.5s             ▶ proxy_tester.py: 5 tahap uji
  tabel hasil + CSV                 ▶ skor 0-100 + grade
```

Uji proxy butuh puluhan request dari satu IP — hanya bisa dari server;
browser cuma panel kontrol.

## Alur uji per proxy

| Tahap | Apa diukur | Bobot |
|---|---|---|
| Liveness | connect + IP keluaran benar (bukan leak) | 20 |
| Geo | negara/kota/ISP; hosting & flagged-proxy dipenalti | 15 |
| Latensi | median 5 request (<1s=20 … >4s=3) | 20 |
| Stabilitas | error rate N request; IP berubah = penalti (sticky rusak) | 30 |
| Cloudflare | apakah IP dicurigai CF (proksi prediksi Turnstile 600010) | 15 |

Proxy mati di tahap 1 langsung diskor 0 dan dilewati sisanya.

## Format file input

Satu proxy per baris; baris kosong dan `#` komentar diabaikan. Semua format ini sah:

```
1.2.3.4:8080
1.2.3.4:8080:user:pass
http://1.2.3.4:8080
http://user:pass@1.2.3.4:8080
socks5://user:pass@1.2.3.4:1080
```

Tanpa scheme dianggap `http`. Duplikat (host:port:user sama) hanya diuji sekali,
walau muncul di beberapa file.

## UI web

Halaman `docs/` (GitHub Pages): drag-drop banyak file .txt, token akses,
opsi thread/negara/stabilitas, progress live dengan log per proxy, tabel hasil,
download CSV. CORS backend dibatasi ke `https://*.github.io`.

## Instalasi backend (VPS)

```bash
git clone https://github.com/haerubirru17/proxy-tester.git
cd proxy-tester
python3 -m venv .venv && .venv/bin/pip install requests pysocks fastapi "uvicorn[standard]" python-multipart

# systemd
sudo tee /etc/systemd/system/proxy-tester.service <<'UNIT'
[Unit]
Description=Proxy Tester backend
After=network.target

[Service]
WorkingDirectory=/opt/proxy-tester
Environment=PROXY_TOKEN=GANTI_TOKEN_ANDA
Environment=PORT=8443
Environment=SSL_CERT=/etc/letsencrypt/live/<DOMAIN>/fullchain.pem
Environment=SSL_KEY=/etc/letsencrypt/live/<DOMAIN>/privkey.pem
ExecStart=/opt/proxy-tester/.venv/bin/python server.py
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl enable --now proxy-tester
curl -sk https://127.0.0.1:8443/health   # -> {"ok":true}
```

Token diset via env `PROXY_TOKEN` (wajib; tanpa token server menolak start).
Domain sslip.io sama dengan yowes-web — sertifikat Let's Encrypt yang sudah
ada dipakai bersama (port berbeda: 8443).

## CLI tanpa web

```bash
# satu file
python3 proxy_tester.py proxies.txt

# MULTI-FILE — duplikat antar-file otomatis dilewati
python3 proxy_tester.py provider_a.txt provider_b.txt --threads 10 -o results.csv
```

## Output

- **Terminal**: progres live per proxy + ringkasan akhir (jumlah per grade, top 5).
- **results.csv**: satu baris per proxy — IP keluaran, negara, ISP, latensi
  min/median/p95, error rate stabilitas, perubahan IP, verdict Cloudflare,
  skor total, grade, catatan error.

## Catatan

- Tahap Cloudflare = proksi kasar untuk memprediksi kelolosan Turnstile
  (error 600010) — sama-sama dinilai dari reputasi IP, tapi tidak identik.
- Hasil job disimpan di memori, dibuang setelah 6 jam.
