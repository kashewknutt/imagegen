from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .file_lock import file_lock


@dataclass(frozen=True)
class ShopifySettings:
    shop_domain: str
    client_id: str
    api_version: str


def load_shopify_settings(path: Path) -> ShopifySettings | None:
    if not path.exists():
        return None
    lock_path = path.with_suffix(path.suffix + ".lock")
    with file_lock(lock_path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
    sd = str(data.get("shop_domain") or "").strip()
    cid = str(data.get("client_id") or "").strip()
    api_ver = str(data.get("api_version") or "").strip() or "2024-01"
    if not sd:
        return None
    return ShopifySettings(shop_domain=sd, client_id=cid, api_version=api_ver)


def save_shopify_settings(path: Path, settings: ShopifySettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with file_lock(lock_path):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "shop_domain": settings.shop_domain,
                    "client_id": settings.client_id,
                    "api_version": settings.api_version,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        tmp.replace(path)

