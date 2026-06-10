#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "ERROR: No GCP project configured. Run: gcloud config set project YOUR_PROJECT_ID"
  exit 1
fi

echo "Using GCP project (Firestore/default): ${PROJECT_ID}"
echo "Active account: $(gcloud config get-value account 2>/dev/null || true)"

OAUTH_PROJECT_ID=""
CLIENT_SECRET="drive_review/credentials/client_secret.json"
if [[ -f "${CLIENT_SECRET}" ]] && command -v python3 >/dev/null 2>&1; then
  OAUTH_PROJECT_ID="$(python3 - <<'PY'
import json
from pathlib import Path
p = Path("drive_review/credentials/client_secret.json")
data = json.loads(p.read_text(encoding="utf-8"))
block = data.get("installed") or data.get("web") or {}
print(block.get("project_id") or "")
PY
)"
fi

if [[ -n "${OAUTH_PROJECT_ID}" ]]; then
  echo "OAuth client GCP project (Drive API): ${OAUTH_PROJECT_ID}"
fi

APIS=(
  firestore.googleapis.com
  drive.googleapis.com
  sheets.googleapis.com
)

for api in "${APIS[@]}"; do
  echo "Enabling ${api} on ${PROJECT_ID}..."
  gcloud services enable "${api}" --project="${PROJECT_ID}"
done

if [[ -n "${OAUTH_PROJECT_ID}" && "${OAUTH_PROJECT_ID}" != "${PROJECT_ID}" ]]; then
  echo "Enabling drive.googleapis.com on OAuth project ${OAUTH_PROJECT_ID}..."
  gcloud services enable drive.googleapis.com --project="${OAUTH_PROJECT_ID}"
fi

echo "Checking Firestore database..."
if ! gcloud firestore databases list --project="${PROJECT_ID}" --format="value(name)" 2>/dev/null | grep -q .; then
  echo "Creating Firestore database (native mode)..."
  gcloud firestore databases create \
    --project="${PROJECT_ID}" \
    --location=nam5 \
    --type=firestore-native \
    --quiet || true
else
  echo "Firestore database already exists."
fi

CONFIG_FILE="drive_review/config.yaml"
if [[ -f "${CONFIG_FILE}" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<PY
from pathlib import Path
import re
p = Path("${CONFIG_FILE}")
text = p.read_text(encoding="utf-8")
if re.search(r'^firestore_project_id:\s*""\s*$', text, re.M):
    text = re.sub(
        r'^firestore_project_id:\s*""\s*$',
        'firestore_project_id: "${PROJECT_ID}"',
        text,
        count=1,
        flags=re.M,
    )
    p.write_text(text, encoding="utf-8")
    print(f"Updated firestore_project_id in {p}")
PY
  fi
fi

cat <<EOF

Setup complete.

Next manual steps:
1. In Google Cloud Console -> APIs & Services -> Credentials:
   - Prefer OAuth client ID type **Desktop app** (simplest; no redirect URIs).
   - If using **Web application** instead, add Authorized redirect URIs:
     http://localhost:8765/ and http://127.0.0.1:8765/
   - Save client JSON to: drive_review/credentials/client_secret.json
2. Authenticate Firestore locally for the same Google account / service account:
   - gcloud auth application-default login
   OR set GOOGLE_APPLICATION_CREDENTIALS to a service account key with Firestore access.
3. Re-consent Drive with write scope in the new app (first Connect Google Drive click).
4. Run the app from repo root:
   ./venv/bin/streamlit run drive_review/app.py

Configured:
- Firestore project: ${PROJECT_ID}
- Drive outputs folder id: 1MHbmR8ruqc9P0K3wExA89FOIUTE-CRXB
- Drive XLSX file id: 1TGAQxp446FbA-nPCeOgRmYiXiMjv_ywX
EOF
