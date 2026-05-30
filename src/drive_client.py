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


def get_drive_service(*, client_secret_path: Path, token_path: Path):
    """
    Returns a Google Drive v3 service (googleapiclient.discovery.build).
    Requires google-auth-oauthlib + google-api-python-client.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/drive.readonly"]
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
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), scopes=scopes)
        # Runs a local callback server on the host machine. This is a one-time consent per user.
        creds = flow.run_local_server(port=0)
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

