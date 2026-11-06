# OCR microservice for `promaxgb10-6116`

> **Status:** a service of this shape is already deployed at
> `http://promaxgb10-6116:8769` (it reports `provider: CUDAExecutionProvider`,
> `model_type: medium`, ~1570 MB RSS) and the MCP server is configured to use
> it. That deployment is a *different implementation* with a slightly richer
> health payload (`status`, `version`, `gpu`, `model_type`, `device_id`,
> `rss_mb`, `uptime_s`, `auth_required`). This file is the reference
> implementation kept in-repo so the contract is documented and reproducible —
> you do not need to deploy it twice.
>
> The two agree on the two things the client depends on:
> `POST /ocr` with a multipart `file` field, returning
> `{"result": [[box, text, confidence], …], "elapse": [det, cls, rec]}`.
> Extra response fields are ignored by the client.

Moves OCR off `latitude`. RapidOCR loads ~170 MB of ONNX models and burns CPU on
every page; this service does that work on the GPU host instead and exposes it
over HTTP on **port 8769**.

**Yes — it is a service on a port.** Everything below is copy-paste, and the
service has been tested end-to-end (see [Verified](#verified)).

| | |
|---|---|
| Host | `promaxgb10-6116` (Dell Pro Max GB10) |
| Port | `8769` (next to whisper.cpp on `8768`) |
| Engine | `rapidocr-onnxruntime` — the *same* engine `latitude` uses today |
| Models | bundled inside the 14.2 MB wheel, so **no download at runtime** |
| RSS | **174 MB** measured with models loaded |
| Output | RapidOCR's native shape, so the client change is mechanical |

## Step 0 — check three things first

```bash
ssh promaxgb10-6116
uname -m            # expect aarch64
python3 -V          # MUST be 3.10–3.12 (not 3.13)
ss -ltnp | grep 8769 || echo "port 8769 free"
```

Why these matter:

- **`aarch64`** is fine — every dependency has an ARM wheel (`onnxruntime`,
  `opencv-python-headless`, `shapely`, `pyclipper`, `numpy`, `Pillow`), and
  RapidOCR itself is pure Python.
- **Python `<3.13`** is a hard constraint: `rapidocr-onnxruntime` declares
  `Requires-Python <3.13`. On Ubuntu 24.04 `python3` is 3.12, so you are fine. If
  you have 3.13 only, install 3.12 first:
  ```bash
  sudo apt-get install -y python3.12 python3.12-venv
  ```
  and use `python3.12 -m venv` in step 2.
- **Port 8769 free** — otherwise pick another and change the unit + client URL.

## Step 1 — system packages

```bash
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip rsync
```

No `libgl1` needed — this deployment deliberately uses
`opencv-python-headless`. (RapidOCR *declares* the GUI `opencv-python`, which
fails on a headless box with `ImportError: libGL.so.1`; step 3 pins the headless
build instead.)

## Step 2 — service account and directories

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin ocr 2>/dev/null || true
sudo mkdir -p /opt/ocr-server
sudo chown -R ocr:ocr /opt/ocr-server
```

> If you prefer running as your own user, use `/home/sameer/ocr-server` and
> `User=sameer` — just keep the two paths consistent with the unit file.

## Step 3 — Python environment

```bash
sudo -u ocr python3 -m venv /opt/ocr-server/.venv

sudo -u ocr /opt/ocr-server/.venv/bin/pip install --upgrade pip
sudo -u ocr /opt/ocr-server/.venv/bin/pip install -r /opt/ocr-server/requirements.txt

# RapidOCR pulls the GUI opencv build; force the headless one to win.
sudo -u ocr /opt/ocr-server/.venv/bin/pip uninstall -y opencv-python
sudo -u ocr /opt/ocr-server/.venv/bin/pip install --force-reinstall opencv-python-headless

# must print a version, not an ImportError
sudo -u ocr /opt/ocr-server/.venv/bin/python -c "import cv2, onnxruntime, rapidocr_onnxruntime as r; print('cv2', cv2.__version__); print('ort', onnxruntime.__version__); print('rapidocr ok')"
```

## Step 4 — copy the service files

From `latitude`:

```bash
cd /home/sameer/Shared/Sync/Private/Apps/services/mcp/noapi-google-search-mcp
rsync -av deploy/ocr-server/{app.py,requirements.txt} promaxgb10-6116:/tmp/ocr-server/
ssh promaxgb10-6116 'sudo mv /tmp/ocr-server/* /opt/ocr-server/ && sudo chown -R ocr:ocr /opt/ocr-server'
```

## Step 5 — test by hand before installing the unit

```bash
cd /opt/ocr-server
sudo -u ocr OCR_WARMUP=1 /opt/ocr-server/.venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8769
```

In a second terminal on the GB10:

```bash
curl -s localhost:8769/           # {"service":"ocr-server",...,"ready":true}
curl -s localhost:8769/health     # {"ok":true,"model_loaded":true,...}
curl -s -F "file=@/path/to/some.png" localhost:8769/ocr
```

Expected response (this is real output from the tested build):

```json
{"result":[[[[18.0,45.0],[456.0,44.0],[457.0,98.0],[18.0,99.0]],"SELFTEST 2026",0.998446]],
 "elapse":[1.5058,0.0158,0.2817]}
```

`Ctrl-C` when it looks right.

## Step 6 — install as a systemd service

```bash
sudo cp /opt/ocr-server/ocr-server.service /etc/systemd/system/
```

Edit it if your paths/user differ from the defaults at the top:

```bash
sudo nano /etc/systemd/system/ocr-server.service
# User=, Group=, WorkingDirectory=, ExecStart=
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ocr-server
sudo systemctl status ocr-server --no-pager
journalctl -u ocr-server -f          # watch the model load once
```

Optional token (recommended if the LAN is not fully trusted):

```bash
sudo tee /etc/ocr-server.env >/dev/null <<'EOF'
OCR_SERVER_TOKEN=<a-long-random-string>
OMP_NUM_THREADS=8
EOF
sudo chmod 0600 /etc/ocr-server.env
sudo systemctl restart ocr-server
```

## Step 7 — verify from `latitude`

```bash
curl -s http://promaxgb10-6116:8769/health
curl -s -F "file=@/tmp/mcp_selftest/text.png" http://promaxgb10-6116:8769/ocr
```

If the LAN blocks it: `sudo ufw allow from 192.168.178.0/24 to any port 8769`.

## Step 8 — point the MCP server at it (still to do)

The service is now usable, but `mcp-google.service` still runs OCR locally. The
client change is small and deliberately kept mechanical, because the response
shape is identical:

- `config.py`: add `OCR_REMOTE_URL` (e.g. `http://promaxgb10-6116:8769`) and
  optional `OCR_REMOTE_TOKEN`, mirroring the `WHISPER_REMOTE_*` block.
- `tools/vision.py` (`ocr_image`) and `tools/web_utils.py` (scanned-PDF
  fallback): when `OCR_REMOTE_URL` is set, POST the bytes and use
  `payload["result"]` / `payload["elapse"]` exactly where `engine(path)` goes
  today — the downstream sorting and formatting code is untouched.
- Add `ocr` to `/health` dependency reporting, and a test that the local engine
  is never constructed when `OCR_REMOTE_URL` is set.

Then the `[ocr]` extra (and `rapidocr-onnxruntime` + `onnxruntime`) can come off
`latitude` entirely — another ~174 MB RSS and the OCR CPU spikes.

## Tuning

| Setting | Default | Meaning |
|---|---|---|
| `OCR_CONCURRENCY` | `2` | Simultaneous OCR calls; higher is not faster (ONNX already multi-threads). |
| `OMP_NUM_THREADS` | `4` | ONNX intra-op threads. GB10 has 20 Arm cores; `8` is a reasonable ceiling. |
| `OCR_MAX_UPLOAD_BYTES` | `26214400` (25 MB) | Reject larger uploads instead of loading them. |
| `OCR_WARMUP` | `1` | Load models at boot so the first request is not slow. |
| `OCR_SERVER_TOKEN` | _empty_ | Bearer token; empty disables auth. |

**GPU:** `onnxruntime-gpu` does publish aarch64 wheels, but Blackwell/CUDA
support on GB10 is not something to assume. Start on CPU — OCR is a
one-shot-per-page workload and the Grace cores handle it — and only investigate
the GPU if a measured need appears.

## Verified

Tested on `latitude` against the shipped `app.py` before writing this:

| Check | Result |
|---|---|
| `GET /` and `GET /health` | 200 |
| `POST /ocr` on a real PNG | 200, 3 regions at 99.7–99.9% confidence |
| Response parses with the *existing* `ocr_image()` sort logic | reading order preserved |
| Model loaded once, not per request | `model_loaded: true` after first call |
| Empty upload / missing field | `400` / `422` |
| Token auth: none, wrong, correct | `401` / `401` / allowed |
| RSS with models loaded | **174 MB** |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ImportError: libGL.so.1` | The GUI OpenCV build won. Re-run the two `opencv-python` lines in step 3, or `sudo apt install -y libgl1`. |
| `No matching distribution found for rapidocr-onnxruntime` | Python 3.13. Use 3.12 (step 0). |
| `error while attempting to bind on address ('0.0.0.0', 8769)` | Port taken — `ss -ltnp \| grep 8769`. |
| First request takes seconds | `OCR_WARMUP=0`; the models load lazily. Leave it at `1`. |
| `/ocr` returns `{"result": []}` | Model ran, found no text. Compare with the same file on `latitude` — a QR code or photo legitimately yields nothing. |
| OOM on the GB10 | Lower `OCR_CONCURRENCY` to 1 and `OMP_NUM_THREADS` to 4. |
