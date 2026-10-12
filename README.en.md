# RetroChimera API Backend

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9](https://img.shields.io/badge/python-3.9-blue.svg)](https://www.python.org/downloads/)
[![Flask](https://img.shields.io/badge/Flask-3.x-black.svg)](https://flask.palletsprojects.com/)

A **Flask API** layer on top of [RetroChimera](https://github.com/microsoft/retrochimera)
(Microsoft's retrosynthesis prediction model): single-step retrosynthesis prediction,
structure parsing/depiction, and LLM-assisted process analysis.

> 🔴 **This service provides an API only — it does not host any web page.**
> Opening the root path in a browser returns a JSON list of endpoints.
> The UI lives in a separate repository: [retroweb](https://github.com/yijiu2025/retroweb).

> 🌐 中文版本: [README.md](README.md) ·
> Linux/Docker deployment: [deploy/部署Linux.md](deploy/部署Linux.md) ·
> Full multi-step search parameters: [使用说明.md](使用说明.md)

---

## 1. What it's made of

```
retrochimera/
├── 启动后端API.bat         ← Windows one-click launcher
├── start_api.sh            ← Linux/macOS launcher (`--check` env audit / `--model` model service only)
├── run_api.py             ← CLI launcher (strips interfering sys.path injections)
├── Dockerfile              ← Linux container image (CPU / GPU)
├── docker-compose.yml      ← Two-service orchestration: model (no exposed port) + api
├── .dockerignore           ← Build-context exclusions (**the 9GB models/ matters**)
├── web/                    ← API service proper (Flask, endpoints only, no pages)
│   ├── app.py                Flask app + all API routes
│   ├── jobhub.py             Job queue (long tasks don't block requests; stages/logs/cancel)
│   ├── model_runtime.py      Model loading + search logic (**single source of truth**: model & algorithm tables)
│   ├── model_server.py       Resident model service (separate process; actually loads models)
│   ├── model_client.py       Model-service client on the API side (HTTP with state-file fallback)
│   ├── llm.py                LLM entry point (re-exports only, no implementation)
│   ├── llm_config.py           Endpoint profiles + API key pool rotation
│   ├── llm_prompts.py          Prompt building + analysis module registry
│   ├── llm_transport.py        HTTP transport + response parsing (incl. JSON extraction)
│   ├── llm_stream.py           SSE orchestration for analysis / condition enumeration / follow-ups
│   ├── .env.example          Config template (copy to .env and fill in)
│   └── requirements.txt      Dependency manifest (except the upstream package itself)
├── bootstrap/              ← Runtime shims (**must be imported before any third-party import**)
│   ├── envfix.py             Patches conda's DLL search path (fixes `_ssl` load failure)
│   └── limit_workers.py      Caps rule subprocesses (fixes WinError 1455)
├── scripts/                ← CLI tools (diagnose / predict / search / repair / download models)
│   ├── check_install.py      Install verification: loads a model and runs a real prediction
│   ├── predict.py            Single-step prediction CLI (confidence tiers + reaction depictions)
│   ├── search.py             Multi-step route search CLI
│   ├── download_model.py     Model checkpoint downloader
│   └── repair_deps.py        Dependency self-check and repair
├── deploy/                 ← Deployment material
│   ├── environment-win.yml   Conda environment export (Windows)
│   ├── 部署Linux.md           **Linux deployment guide** (bare metal / systemd / Docker)
│   ├── systemd/               Production unit files (api + model, with resource limits & hardening)
│   └── wheels/               Offline Flask wheels (used by the .bat when offline)
├── core/                   ← Upstream source (**vendored into this repo**; see its VENDORED.md)
│   ├── retrochimera/         Upstream Python package
│   └── pyproject.toml        Install it: cd core && pip install -e .
└── models/                 ← Model checkpoints, 9GB, **not in git**
    ├── retrochimera_pistachio/
    └── retrochimera_uspto50k/
```

> 🔴 **The model does not live in the API process.** `app.py` only proxies: the process that
> actually loads models and runs searches is `model_server.py`, a **separate resident
> process** (default `127.0.0.1:8001`). That lets the API start in seconds while the model
> loads in the background; the two talk through `model_client.py` (HTTP first, falling back
> to a state file).
> The trade-off: model load failures only appear in the model service's log, surfaced to
> clients through the `error` field of `/api/models`.
> To restart the model service: `POST /api/model-service/restart`.

**You do not need to clone the upstream repo** — it is vendored here (`core/`, ~1.1MB).
After installing dependencies, run `cd core && pip install -e .`.

> What those 10 markdown files in `core/` cover, what the upstream model can do, and its
> known pitfalls → see [`上游文档导读.md`](上游文档导读.md) (Chinese).

Only `models/` lives outside the repo (9GB is too large); copy it over manually when moving
machines (see §3.3).

**No hard-coded drive letters anywhere** — every path is derived from the repository's own
location, so a `git clone` into any directory works. Model locations can be overridden with
environment variables (see §3.3 / §5).

---

## 2. Requirements

| Item | Requirement | Notes |
| --- | --- | --- |
| OS | Windows 10/11 or Linux/macOS | One-click launchers for both (`.bat` / `start_api.sh`); production deployment see [`deploy/部署Linux.md`](deploy/部署Linux.md) |
| Python | **3.9** (3.10 theoretically OK) | Upstream `pyproject.toml` requires `>=3.9`; 3.9.7 is the tested version. Other versions are not fully verified |
| GPU | NVIDIA, ≥ 6GB VRAM | CPU-only works but single-step prediction goes from seconds to tens of seconds |
| CUDA | 12.1 | Matches `torch==2.2.2+cu121` |
| Disk | ≥ 15GB | 9GB of weights + ~5GB of dependencies |
| RAM | ≥ 16GB | ~18GB with `RETRO_RULE_WORKERS=4`; drop to 2 if memory-tight |

---

## 3. Getting started (follow this on a new machine)

### 3.1 Create a conda environment and install dependencies

```bat
conda create -n retrochimera python=3.9 -y
conda activate retrochimera
```

Install PyTorch first (**use the official wheel index**, otherwise you get the CPU build;
use cu121 if you have a GPU):

```bat
pip install torch==2.2.2 torchvision==0.17.2 --index-url https://download.pytorch.org/whl/cu121
```

> Replace `cu121` with `cpu` if you have no GPU.

Then install the rest (run from the **repository root**; below `<repo root>` stands in for,
e.g. `F:\retrochimera`):

```bat
cd <repo root>
pip install -r web\requirements.txt
```

> 🔴 **The comments in `requirements.txt` are required reading, not optional.** Watch out
> especially for **torch-geometric**: its wheels come from
> `https://data.pyg.org/whl/torch-2.2.0+cu121.html` — note the directory says
> `torch-2.2.0`, not `2.2.2` (an upstream typo; copy it verbatim).
>
> If you'd rather not install step by step, use the automated Linux script (order is
> baked in): `./scripts/setup_env_linux.sh --venv`.

### 3.2 Install the upstream package (source is already here, no clone needed)

```bat
cd <repo root>\core
pip install -e .
```

If it hangs on build dependencies:

```bat
pip install -e . --no-build-isolation
```

Verify:

```bat
python -c "import retrochimera; print(retrochimera.__file__)"
```

The path should land on `<repo root>\core\retrochimera\__init__.py`.
Version details and upgrade instructions are in `core/VENDORED.md`.

### 3.3 Place the model checkpoints

Put the model directories under `models/`:

```
models/retrochimera_pistachio/     ~4.2GB, recommended (largest training set)
models/retrochimera_uspto50k/      ~0.3GB, lightweight
```

> If you only have the `.zip` files, unzip them into directories of the same name.
> The `.zip` itself can be deleted afterwards.

Model locations can be overridden with environment variables — they don't have to live
under `models/`:

```bat
set RETRO_MODELS_DIR=D:\models
set RETRO_MODEL_PISTACHIO=E:\my\pistachio_model
```

### 3.4 Write the configuration

```bat
copy web\.env.example web\.env
```

Then edit `web\.env`. At minimum fill in the LLM group (optional — without it everything
else works, only the "AI process analysis" button reports "not configured"):

```
RETRO_LLM_BASE_URL=https://token.sensenova.cn/v1
RETRO_LLM_API_KEY=sk-your-key
RETRO_LLM_MODEL=glm-5.2
```

`.env` is **hot-reloaded on mtime change** — save and it takes effect, **no restart needed**.

---

## 4. Running it

### Option 1: double-click (recommended on Windows)

```
<repo root>\启动后端API.bat
```

It auto-detects Python (in order: `RETRO_PYTHON` env var → active conda env → `.venv` →
default conda path → `PATH`), checks whether ports are in use, then starts the API.

To point it at a different interpreter:

```bat
set RETRO_PYTHON=D:\conda\envs\retrochimera\python.exe
```

### Option 2: command line

```bat
conda activate retrochimera
cd <repo root>
python web\app.py
```

Or use the CLI launcher (it strips interfering `sys.path` injections for you):

```bat
python run_api.py
set RETRO_PORT=8011 && python run_api.py
```

### Option 3: Linux / macOS

```bash
./scripts/setup_env_linux.sh --venv    # build environment (CPU; add --gpu cu121 for GPU)
python scripts/download_model.py       # download models (~9GB)
./start_api.sh --check                 # env audit: versions/deps/GPU/weights/ports
./start_api.sh                         # start
```

For production deployment (systemd / Docker, including reverse-proxy and GPU setup) see
**[`deploy/部署Linux.md`](deploy/部署Linux.md)**.

Once started it listens on **http://127.0.0.1:8000**. Opening it in a browser shows only a
JSON endpoint list (this service hosts no pages); for the UI, run the frontend:

```bat
cd <retroweb repo>
npm install && npm run dev        # http://localhost:5177
```

> A burst of red `BrokenPipeError` lines on shutdown is **normal** (worker cleanup).

### Bonus: bundled CLI tools

All under `scripts/`, run from the repository root. They **do not** require the API service
to be running — they load the model themselves.

| Tool | Purpose | Example |
| --- | --- | --- |
| `scripts/check_install.py` | Install verification: loads a model and runs a real prediction | `python scripts/check_install.py` |
| `scripts/predict.py` | Single-step prediction with confidence tiers and reaction depictions | `python scripts/predict.py "CC(=O)Oc1ccccc1C(=O)O" 5` |
| `scripts/search.py` | Multi-step route search (Retro*), can emit JSON | `python scripts/search.py "Oc1ccc(OCc2ccccc2)c(Br)c1" 80 150 3` |
| `scripts/repair_deps.py` | Dependency self-check/repair: probe each import, reinstall what's broken | `python scripts/repair_deps.py` |

`predict.py` writes structure images to `<repo root>/outputs/` (not committed).

---

## 5. Configuration

All via environment variables. Precedence: **real environment variable > `web/.env` >
repo-root `.env`**; an empty value counts as unset.

| Variable | Default | Notes |
| --- | --- | --- |
| `RETRO_PORT` | `8000` | Listen port |
| `RETRO_RULE_WORKERS` | `4` | Rule application processes. 16 (all cores) consumes 70GB and crashes (WinError 1455) — don't raise it casually |
| `RETRO_MODELS_DIR` | `<repo root>/models` | Model root directory |
| `RETRO_MODEL_PISTACHIO` | — | Override the pistachio model path alone |
| `RETRO_MODEL_USPTO50K` | — | Override the uspto50k model path alone |
| `RETRO_OUTPUT_DIR` | `<repo root>/outputs` | Where `scripts/predict.py` writes depictions |
| `RETRO_MODEL_PORT` | `8001` | Model service port |
| `RETRO_MODEL_HOST` | `127.0.0.1` | Model service host. **Set to the service name (e.g. `model`) under Docker** — containers have separate network namespaces |
| `RETRO_PRELOAD` | — | Preload a model at model-service startup (`none` to skip) |
| `RETRO_CORS_ORIGINS` | empty (disabled) | Allowed frontend origins, comma-separated; `*` allows all. **Required when frontend and backend are on different origins** |
| `RETRO_LLM_BASE_URL` | — | OpenAI-compatible endpoint, e.g. `https://token.sensenova.cn/v1` |
| `RETRO_LLM_API_KEY` | — | API key |
| `RETRO_LLM_MODEL` | — | Model name |
| `RETRO_LLM_MAX_TOKENS` | `16384` | **Don't lower this.** Reasoning tokens count against the output budget; too small truncates before any body text is emitted |
| `RETRO_LLM_TIMEOUT` | `300` | Per-request timeout (seconds). Reasoning models have been measured taking 55s for a single step |
| `RETRO_LLM_JSON_MODE` | `1` | Request JSON output |
| `RETRO_LLM_TEMPERATURE` | `0.3` | Sampling temperature |

---

## 6. Wiring up a frontend

During development the `retroweb` frontend proxies `/retro/*` → `http://127.0.0.1:8000/api/*`
through Vite, so it is same-origin and **needs no CORS**.

In production, if frontend and backend are on different domains, you must set:

```bat
set RETRO_CORS_ORIGINS=https://your-frontend.example.com
```

The symptom of forgetting this is a browser console CORS error and a page that spins forever.

> This service **does not host a frontend**: `/` returns only the endpoint list JSON;
> `/api/*` are the real endpoints. To check liveness quickly, hit `/api/models`.

---

## 7. When the LLM won't connect

Three steps locate the vast majority of problems:

1. **Check auth** — hit the endpoint's model list directly:
   ```bat
   curl -H "Authorization: Bearer sk-xxx" https://your-endpoint/v1/models
   ```
   `200` means the key works; `401` means the key is wrong.

2. **Verify key and model are both in your plan**:
   ```bat
   curl "http://127.0.0.1:8000/api/llm/status?probe=1"
   ```

3. **List actually-usable models**:
   ```bat
   curl http://127.0.0.1:8000/api/llm/models
   ```

Two traps we hit repeatedly:

- **`/v1/models` lies** — a listed model isn't necessarily in your plan. In our measurements
  3 of 9 listed models failed; trust step 2's `probe` result instead.
- **Reasoning models emit only `reasoning_content`** — the model outputs its thinking first,
  so `delta.content` stays empty for a long time. This project emits a separate `thinking`
  SSE event and the UI shows "thinking"; a custom client that only reads `content` will
  think it has hung.

Rate limits are computed **per model endpoint**, so switching models switches you to a
separate quota.

Currently verified working configuration:

```
RETRO_LLM_BASE_URL=https://token.sensenova.cn/v1
RETRO_LLM_MODEL=glm-5.2
RETRO_LLM_MAX_TOKENS=16384
RETRO_LLM_TIMEOUT=300
```

---

## 8. API overview

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Endpoint list (self-describing JSON — this service hosts no pages). Includes `search_algorithms`, the **only** source for the frontend's algorithm dropdown |
| GET | `/api/models` | Model status matrix: idle/loading/ready/failed/unavailable per model + service overview (with load `progress`) |
| POST | `/api/load` | Load / switch model (starts in background, returns immediately; watch `/api/models`) |
| POST | `/api/forward/load` | Load the forward model (the feasibility filter's judge; hot-attachable, no process restart) |
| POST | `/api/model-service/<action>` | Resident model service start / stop / restart |
| POST | `/api/predict` | **Single-step** retrosynthesis prediction (the frontend grows the route tree by calling it repeatedly) |
| POST | `/api/search` | Multi-step route search in one shot (synchronous; `algorithm` accepts `retro_star`/`bfs`) |
| POST | `/api/parse` | molfile / SMILES → canonical structure + formula / molecular weight |
| POST | `/api/depict` | SMILES → molfile with 2D coordinates (for the drawing board) |
| GET/POST | `/api/jobs` | Submit and list long jobs (`kind=predict|search`) |
| GET | `/api/jobs/<id>` | Job status / result (`?since=` for incremental log fetches) |
| POST | `/api/jobs/<id>/cancel` | Cancel a job |
| GET/POST | `/api/llm/status` | LLM configuration status (`?probe=1` makes a real request) |
| GET/POST | `/api/llm/models` | Models visible to the configured key |
| GET/POST | `/api/llm/settings` | List / save endpoint profiles (multiple bindings, effective immediately, keys masked) |
| POST | `/api/llm/settings/active` | Switch the active endpoint profile |
| POST | `/api/llm/keys/reset` | Retry failed keys |
| POST | `/api/llm/analyze` | Process analysis (single response) |
| POST | `/api/llm/analyze/stream` | Process analysis (**SSE streaming**) |
| POST | `/api/llm/conditions/stream` | Candidate reaction condition enumeration (SSE; pick conditions, then analyze) |
| POST | `/api/llm/followup/stream` | Process-analysis follow-up questions (SSE, carries context) |
| GET/POST | `/api/feedback` | Write / read back "prediction vs. actual" feedback (stored in `web/data/feedback.jsonl`) |

> There is **no** `/api/health`. To check liveness, use `/api/models` (it also tells you
> how far along the model load is). And there are no HTML routes by design — the backend
> serves data only.

---

## 9. Known pitfalls

- **The model runs in its own process; the API process never touches it.** There's no model
  object in `app.py`, only a `ModelServiceClient`. Search logic lives in `model_runtime.py`
  (shared by both sides), and after changing it you **must restart the model service** —
  otherwise the process doing the search still runs old code.
- **The first load is slow (~2 minutes cold)** because it reads several GB of weights.
  `/api/models` returns `progress` (0–100, an **estimate**) and a `phase` string so the
  frontend can draw a progress bar.
- **Raising `RETRO_RULE_WORKERS` crashes the process** — see §5.
- **The search budget `max_calls` is "new calls for this run"**, not an absolute value.
  `model_runtime.run_retro_search` expresses it as "current baseline + this run's budget";
  because the model service is resident, passing an absolute value lets the counter climb
  across searches until every search starts with **zero results**.
- **Never reorder the `bootstrap/` imports.** `bootstrap.envfix` (DLL search path) and
  `bootstrap.limit_workers` (subprocess cap) are **side-effecting**: importing them takes
  effect, and they **must precede** torch / retrochimera / syntheseus. Comment them out or
  move them later and you effectively uninstall the fix — symptoms are `_ssl` load failure
  and WinError 1455 respectively. See `bootstrap/__init__.py`.
- **The upstream code in `core/` is a runtime dependency** — deleting it breaks startup.
  It's vendored, so you can restore it by re-pulling this repo (no need to clone upstream).

---

## 10. Search algorithms

The API parameter is **`algorithm`** (not the official CLI's `search_algorithm`),
accepting **`retro_star`** (default) or **`bfs`**. Both the synchronous `/api/search` and
the asynchronous `/api/jobs(kind=search)` support it.

| Key | Label | Behaviour | When to pick it |
|---|---|---|---|
| `retro_star` | Retro* | Best-first: expands the most promising node by "estimated remaining steps + reaction cost" | Default. Tight budgets, need one good route fast |
| `bfs` | Breadth-first BFS | Expands level by level with no selection | Want more candidates, or suspect the heuristic buried the best route |

> 🔴 **MCTS / PDVN exist upstream but are not exposed here**: their constructors require a
> policy model (`assert self.policy is not None`), and this repository ships no such
> artifact, so they cannot start.
>
> The single source of truth for valid values is `SEARCH_ALGORITHMS` in
> `web/model_runtime.py`; unknown values fall back silently to `retro_star`. Frontends
> should read the dropdown options from `GET /api` → `search_algorithms`, not hard-code them.

---

## 11. License

The new code in this repository (`web/`, `bootstrap/`, `scripts/`, `deploy/`, …) is
released under the **MIT License** — see [`LICENSE`](LICENSE), Copyright (c) 2026 yijiu2025.

This repository contains **third-party code**; redistributing it also requires complying
with [`NOTICE`](NOTICE):

- **`core/`** — a vendored copy of RetroChimera v1.3.0 (MIT, Copyright (c) Microsoft
  Corporation). This is substantive code, so its original license notice **must** be
  preserved (see `core/LICENSE`).
- **`core/retrochimera/dgllife/`** and **`core/retrochimera/opennmt/`** — from
  dgl-lifesci and OpenNMT-py respectively; their original notices live in those directories.
- **Runtime dependencies** — PyTorch (BSD-3), PyG (MIT), Syntheseus (MIT), RDKit (BSD),
  Flask (BSD-3), and others, each under its own license.
- **Model checkpoints** — the checkpoints under `models/` are **not in this repository**
  (9GB, downloaded separately); their terms follow the
  [upstream repository](https://github.com/microsoft/retrochimera).
  ⚠️ **The MIT license covers code; it does not automatically cover model weights.**

---

## 12. Disclaimer

This project is a **research** tool. Upstream explicitly warns that its predictions may
**hallucinate** — producing chemically implausible or even nonexistent reactions.

> 🔴 **Do not** use this tool's output directly for actual synthesis, drug manufacturing,
> or any safety-critical decision. Generated routes must be independently verified by a
> **qualified chemist** before use. See the hallucination-risk section of
> [`core/README.md`](core/README.md).

The software is provided "as is", without warranty of any kind, express or implied,
including but not limited to merchantability, fitness for a particular purpose, and
non-infringement. See the warranty disclaimer in LICENSE.