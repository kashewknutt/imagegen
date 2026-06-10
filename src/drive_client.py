from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .file_lock import file_lock


@dataclass(frozen=True)
class DriveFile:
    id: str
    name: str
    mime_type: str
    size: int
    modified_time: str


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    with file_lock(lock):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


def ensure_client_secret_saved(*, dest_path: Path, uploaded_bytes: bytes) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    lock = dest_path.with_suffix(dest_path.suffix + ".lock")
    with file_lock(lock):
        dest_path.write_bytes(uploaded_bytes)


DRIVE_READONLY_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
DRIVE_WRITE_SCOPES = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive"]

# Fixed port so Web OAuth clients can register exact redirect URIs in Google Cloud Console.
DRIVE_OAUTH_PORT = int(__import__("os").environ.get("DRIVE_OAUTH_PORT", "8765"))


def oauth_redirect_uris(port: int | None = None) -> list[str]:
    port = port or DRIVE_OAUTH_PORT
    return [f"http://localhost:{port}/", f"http://127.0.0.1:{port}/"]


def _client_secret_block(client_secret_path: Path) -> dict[str, Any]:
    data = _load_json(client_secret_path)
    if "installed" in data:
        return dict(data["installed"])
    if "web" in data:
        return dict(data["web"])
    return {}


def _client_secret_kind(client_secret_path: Path) -> str:
    data = _load_json(client_secret_path)
    if "installed" in data:
        return "installed"
    if "web" in data:
        return "web"
    return "unknown"


def oauth_gcp_project_id(client_secret_path: Path) -> str:
    """GCP project_id embedded in OAuth client JSON (where Drive API must be enabled)."""
    return str(_client_secret_block(client_secret_path).get("project_id") or "").strip()


def format_drive_http_error(exc: Exception) -> str:
    """User-facing hint for common Drive API failures."""
    text = str(exc)
    if "accessNotConfigured" in text or "has not been used in project" in text:
        import re

        m = re.search(r"project (\d+)", text)
        num = m.group(1) if m else "YOUR_PROJECT_NUMBER"
        return (
            "Google Drive API is not enabled for the OAuth GCP project. "
            f"Enable it: https://console.cloud.google.com/apis/library/drive.googleapis.com "
            f"(project number {num}), or run: "
            "gcloud services enable drive.googleapis.com --project=imagegen-497618"
        )
    return text


def get_drive_service(
    *,
    client_secret_path: Path,
    token_path: Path,
    write: bool = False,
):
    """
    Returns a Google Drive v3 service (googleapiclient.discovery.build).
    Requires google-auth-oauthlib + google-api-python-client.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = DRIVE_WRITE_SCOPES if write else DRIVE_READONLY_SCOPES
    creds = None
    if token_path.exists():
        data = _load_json(token_path)
        try:
            creds = Credentials.from_authorized_user_info(data, scopes=scopes)
        except Exception:
            creds = None

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _save_json(token_path, json.loads(creds.to_json()))
    if not creds or not creds.valid:
        kind = _client_secret_kind(client_secret_path)
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), scopes=scopes)
        try:
            # Web OAuth clients require exact redirect URIs — see oauth_redirect_uris().
            creds = flow.run_local_server(
                port=DRIVE_OAUTH_PORT,
                open_browser=True,
                redirect_uri_trailing_slash=True,
            )
        except Exception as e:
            err = str(e).lower()
            if "redirect_uri_mismatch" in err or "redirect" in err:
                uris = oauth_redirect_uris()
                raise RuntimeError(
                    "Google OAuth redirect_uri_mismatch. "
                    f"Your client_secret.json is type '{kind}'. "
                    + (
                        "For a Web application client, add these Authorized redirect URIs "
                        f"in Google Cloud Console -> Credentials -> your OAuth client: {uris}. "
                        "Or create a Desktop app OAuth client instead (no redirect URIs needed)."
                        if kind == "web"
                        else f"Ensure localhost redirect URIs are allowed: {uris}"
                    )
                ) from e
            raise
        _save_json(token_path, json.loads(creds.to_json()))

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_videos_for_sku(*, service, folder_id: str, sku: str, page_size: int = 50) -> list[DriveFile]:
    q = (
        f"'{folder_id}' in parents and trashed=false and "
        f"(mimeType contains 'video/') and name contains '{sku}'"
    )
    resp = service.files().list(
        q=q,
        pageSize=page_size,
        fields="files(id,name,mimeType,size,modifiedTime)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    out: list[DriveFile] = []
    for f in resp.get("files", []):
        try:
            out.append(
                DriveFile(
                    id=str(f.get("id") or ""),
                    name=str(f.get("name") or ""),
                    mime_type=str(f.get("mimeType") or "video/mp4"),
                    size=int(f.get("size") or 0),
                    modified_time=str(f.get("modifiedTime") or ""),
                )
            )
        except Exception:
            continue
    return [x for x in out if x.id]


def download_file_to_cache(*, service, file_id: str, cache_path: Path) -> Path:
    """
    Downloads a Drive file to cache_path if missing. Returns cache_path.
    """
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    from googleapiclient.http import MediaIoBaseDownload

    req = service.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, req)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        _ = status
        time.sleep(0.01)
    cache_path.write_bytes(fh.getvalue())
    return cache_path


def list_children(
    *,
    service,
    parent_id: str,
    mime_type: str | None = None,
    page_size: int = 200,
) -> list[DriveFile]:
    parts = [f"'{parent_id}' in parents", "trashed=false"]
    if mime_type:
        parts.append(f"mimeType='{mime_type}'")
    q = " and ".join(parts)
    out: list[DriveFile] = []
    page_token: str | None = None
    while True:
        resp = (
            service.files()
            .list(
                q=q,
                pageSize=page_size,
                pageToken=page_token,
                fields="nextPageToken,files(id,name,mimeType,size,modifiedTime)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for f in resp.get("files", []):
            try:
                out.append(
                    DriveFile(
                        id=str(f.get("id") or ""),
                        name=str(f.get("name") or ""),
                        mime_type=str(f.get("mimeType") or ""),
                        size=int(f.get("size") or 0),
                        modified_time=str(f.get("modifiedTime") or ""),
                    )
                )
            except Exception:
                continue
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return [x for x in out if x.id]


def find_child_folder(*, service, parent_id: str, name: str) -> DriveFile | None:
    q = (
        f"'{parent_id}' in parents and trashed=false and "
        f"mimeType='application/vnd.google-apps.folder' and name='{name}'"
    )
    resp = (
        service.files()
        .list(
            q=q,
            pageSize=5,
            fields="files(id,name,mimeType,size,modifiedTime)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    files = resp.get("files") or []
    if not files:
        return None
    f = files[0]
    return DriveFile(
        id=str(f.get("id") or ""),
        name=str(f.get("name") or ""),
        mime_type=str(f.get("mimeType") or "application/vnd.google-apps.folder"),
        size=int(f.get("size") or 0),
        modified_time=str(f.get("modifiedTime") or ""),
    )


def create_folder(*, service, parent_id: str, name: str) -> DriveFile:
    existing = find_child_folder(service=service, parent_id=parent_id, name=name)
    if existing:
        return existing
    body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    f = (
        service.files()
        .create(body=body, fields="id,name,mimeType,size,modifiedTime", supportsAllDrives=True)
        .execute()
    )
    return DriveFile(
        id=str(f.get("id") or ""),
        name=str(f.get("name") or ""),
        mime_type="application/vnd.google-apps.folder",
        size=0,
        modified_time=str(f.get("modifiedTime") or ""),
    )


def upload_or_update_file(
    *,
    service,
    local_path: Path,
    parent_id: str,
    name: str | None = None,
    file_id: str | None = None,
    mime_type: str | None = None,
) -> DriveFile:
    from googleapiclient.http import MediaFileUpload

    local_path = Path(local_path)
    if not local_path.is_file():
        raise FileNotFoundError(local_path)
    upload_name = name or local_path.name
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True)
    if file_id:
        f = (
            service.files()
            .update(
                fileId=file_id,
                media_body=media,
                fields="id,name,mimeType,size,modifiedTime",
                supportsAllDrives=True,
            )
            .execute()
        )
    else:
        body = {"name": upload_name, "parents": [parent_id]}
        f = (
            service.files()
            .create(
                body=body,
                media_body=media,
                fields="id,name,mimeType,size,modifiedTime",
                supportsAllDrives=True,
            )
            .execute()
        )
    return DriveFile(
        id=str(f.get("id") or ""),
        name=str(f.get("name") or ""),
        mime_type=str(f.get("mimeType") or mime_type or ""),
        size=int(f.get("size") or 0),
        modified_time=str(f.get("modifiedTime") or ""),
    )


def delete_file(*, service, file_id: str) -> None:
    service.files().delete(fileId=file_id, supportsAllDrives=True).execute()


def move_file_to_parent(*, service, file_id: str, new_parent_id: str) -> None:
    meta = service.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
    prev_parents = ",".join(meta.get("parents", []))
    (
        service.files()
        .update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=prev_parents,
            fields="id,parents",
            supportsAllDrives=True,
        )
        .execute()
    )

