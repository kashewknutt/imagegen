# imagegen — CSV → reference image → 2 generations → approve/regenerate

This project reads SKUs from a CSV, finds the matching reference image from `dslr_shots/`, generates **two images per SKU** using Google “NanoBanana” (Gemini image model), and lets you approve/regenerate in a Streamlit UI.

## 1) Setup

1. Activate your venv (you already have `venv/` in this repo).
2. Install deps:
   - `./venv/bin/pip install -r requirements.txt`
3. Copy env template and fill values:
   - `cp .env.example .env`

### Auth

This repo now uses **Gemini Developer API (AI Studio) only**.

- Set in `.env`:
  - `GOOGLE_API_KEY=...` (or `GEMINI_API_KEY=...`)

## 2) Configure

Edit `config.yaml`:
- `csv_path`: `1.csv` or `2.csv`
- `images_dir`: `dslr_shots`
- `allow_url_fallback`: whether to download from `Media Links` when local match fails

## 3) Run

- `./venv/bin/python -m streamlit run app.py`

By default this repo is currently set to folder-first mode (`input_mode: folder` in `config.yaml`), which:
- groups images in `images_dir` by SKU base name (treating `SKU_1`, `SKU_2` as variants),
- shows all variants per SKU,
- lets you select one or many references per SKU,
- generates Prompt 1 + Prompt 2 immediately for your selection,
- then you approve and move to the next SKU group.

### Model selection + cost log

- Use the Streamlit sidebar to **list available models** and **choose the current image model** for generation.
- Every generation attempt appends token usage (and optional estimated cost) to `outputs/cost_log.csv` (`cost_log_csv` in `config.yaml`).
- To estimate cost in USD, fill `pricing_usd_per_million_tokens` in `config.yaml` for the model(s) you use.

### Optional pre-step: deduplicate `_1/_2/_3` variants

If `dslr_shots/` contains multiple images for the same SKU as `SKU_1.jpg`, `SKU_2.jpg`, etc, run:

- `./venv/bin/python -m streamlit run dedupe_app.py`

This shows all duplicates and lets you choose which to keep. Removed files are moved to `outputs/_removed_images/`.

The app will:
- pick the next pending SKU,
- generate Prompt 1 + Prompt 2,
- show Original + both outputs,
- let you Approve / Regenerate / Skip.

If a SKU cannot be matched to a reference image automatically, the UI will ask you to manually pick a reference from `dslr_shots/` once, and it will be remembered for that SKU in `outputs/state.json`.

Outputs:
- `outputs/{SKU}/prompt1_v{n}.png`
- `outputs/{SKU}/prompt2_v{n}.png`
- `outputs/missing_local_images.csv` for SKUs that had no match in `dslr_shots/`.

## Notes
- CSVs like `1.csv`/`2.csv` contain many rows with blank SKU; this tool groups those rows under the last non-empty SKU.
- Rate limiting is configurable via `min_seconds_between_requests` and retry/backoff logic.
