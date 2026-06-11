# Drive-Backed Review App

Standalone Streamlit app for reviewing SKU workspaces with **local-first** reads and **push-only** Drive sync.

- **Reads** SKU workspaces from local `outputs/` and stock rows from the [Google Sheet](https://docs.google.com/spreadsheets/d/1TGAQxp446FbA-nPCeOgRmYiXiMjv_ywX/edit) (`drive_xlsx_file_id`).
- **Writes** the sheet back only when you approve/upload/sync to Shopify and Drive — not on every read.

## Prerequisites

- Repo venv: `./venv/bin/pip install -r requirements.txt`
- `gcloud` logged in to the correct GCP project
- Google OAuth client JSON for Drive write access
- Copy `.env.example` → `.env` and set `SHOPIFY_*` (and optional `FIRESTORE_PROJECT_ID`, Drive folder IDs)

### Enable Drive API on the OAuth project

OAuth and Firestore may use **different** GCP projects. The Drive API must be enabled on the project that owns your OAuth client (see `project_id` inside `client_secret.json`, e.g. `imagegen-497618`):

```bash
gcloud services enable drive.googleapis.com --project=imagegen-497618
```

Or open: [Drive API library](https://console.cloud.google.com/apis/library/drive.googleapis.com) and select that project.

### OAuth client type (important)

Use either:

1. **Desktop app** (recommended) — download JSON, save as `drive_review/credentials/client_secret.json`. No redirect URIs to configure.

2. **Web application** — if you use a Web client (JSON contains `"web": {...}`), you **must** add these **Authorized redirect URIs** in [Google Cloud Console](https://console.cloud.google.com/apis/credentials) → your OAuth 2.0 Client:

   - `http://localhost:8765/`
   - `http://127.0.0.1:8765/`

   Adding only `localhost` or `http://localhost` without the port will cause **Error 400: redirect_uri_mismatch**.

   Optional: change port with `export DRIVE_OAUTH_PORT=8080` and register matching URIs.

## One-time setup

From repo root:

```bash
chmod +x scripts/setup_drive_review_gcp.sh
./scripts/setup_drive_review_gcp.sh
gcloud auth application-default login
```

Copy your Google OAuth client JSON to:

```
drive_review/credentials/client_secret.json
```

Set `firestore_project_id` in `drive_review/config.yaml` if the setup script did not fill it.

## Run

```bash
./venv/bin/streamlit run drive_review/app.py
```

## Drive resources

- Outputs folder ID: `1MHbmR8ruqc9P0K3wExA89FOIUTE-CRXB`
- Stock XLSX file ID: `1TGAQxp446FbA-nPCeOgRmYiXiMjv_ywX`

## CLI helpers

```bash
./venv/bin/python drive_review/audit_typo_skus.py
./venv/bin/python drive_review/apply_typo_cleanup.py --dry-run
./venv/bin/python drive_review/apply_typo_cleanup.py
```

## Local workspace

Ensure `outputs/` contains your SKU folders (and `outputs/review_state.json` if you use review state). Point `local_outputs_dir` in `drive_review/config.yaml` at that directory (default: `outputs`). Stock data is exported from the Google Sheet on Drive (`drive_xlsx_file_id`) into `drive_review/cache/stock_sheet.xlsx` when the app connects (refreshed when Drive `modifiedTime` changes).

## Workflow

1. Upload Google OAuth client JSON in the sidebar (one-time).
2. Set Shopify credentials in `.env` — the app connects automatically.
3. **Tools → Tally everything** — compares Drive vs local `outputs/`, Stock.xlsx, review state, and Shopify (metadata only, no downloads).
4. Run **Typo Cleanup** audit/apply — scans/migrates locally, then pushes only changed SKUs + XLSX to Drive.
5. Open **Review Queue** (lists local `outputs/` SKUs), lease next SKU, regenerate prompts as needed.
6. Approve, then upload/update Shopify — changes push to Drive and replace the XLSX on Drive.
