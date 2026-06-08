"""Per-SKU Shopify review state: metadata edits, approval, upload tracking."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .file_lock import file_lock

REVIEW_STATUSES = {"pending_review", "approved", "uploaded", "failed"}
UPLOAD_STATUSES = {"pending", "uploaded", "failed"}


def _now_utc() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class ReviewState:
    sku: str
    review_status: str
    upload_status: str
    product_id: str
    handle: str
    title: str
    category: str
    product_type: str
    description: str
    tags: str
    prompt1_text: str
    prompt2_text: str
    approved_prompt1_version: int | None
    approved_prompt2_version: int | None
    shopify_media_ids: list[str]
    last_error: str
    updated_at_utc: str


def _default_record(sku: str) -> dict[str, Any]:
    return {
        "sku": sku,
        "review_status": "pending_review",
        "upload_status": "pending",
        "product_id": "",
        "handle": "",
        "title": "",
        "category": "",
        "product_type": "",
        "description": "",
        "tags": "",
        "prompt1_text": "",
        "prompt2_text": "",
        "approved_prompt1_version": None,
        "approved_prompt2_version": None,
        "shopify_media_ids": [],
        "last_error": "",
        "updated_at_utc": "",
    }


class ReviewStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"skus": {}})

    def _read(self) -> dict[str, Any]:
        with file_lock(self.lock_path):
            return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, data: dict[str, Any]) -> None:
        with file_lock(self.lock_path):
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)

    def get_record(self, sku: str) -> dict[str, Any]:
        data = self._read()
        rec = (data.get("skus") or {}).get(sku) or _default_record(sku)
        return dict(rec)

    def get(self, sku: str) -> ReviewState:
        rec = self.get_record(sku)
        p1 = rec.get("approved_prompt1_version")
        p2 = rec.get("approved_prompt2_version")
        return ReviewState(
            sku=sku,
            review_status=str(rec.get("review_status") or "pending_review"),
            upload_status=str(rec.get("upload_status") or "pending"),
            product_id=str(rec.get("product_id") or ""),
            handle=str(rec.get("handle") or ""),
            title=str(rec.get("title") or ""),
            category=str(rec.get("category") or ""),
            product_type=str(rec.get("product_type") or ""),
            description=str(rec.get("description") or ""),
            tags=str(rec.get("tags") or ""),
            prompt1_text=str(rec.get("prompt1_text") or ""),
            prompt2_text=str(rec.get("prompt2_text") or ""),
            approved_prompt1_version=int(p1) if p1 is not None else None,
            approved_prompt2_version=int(p2) if p2 is not None else None,
            shopify_media_ids=list(rec.get("shopify_media_ids") or []),
            last_error=str(rec.get("last_error") or ""),
            updated_at_utc=str(rec.get("updated_at_utc") or ""),
        )

    def update(self, sku: str, **patch: Any) -> dict[str, Any]:
        data = self._read()
        db = data.setdefault("skus", {})
        rec = db.get(sku) or _default_record(sku)
        rec.update(patch)
        rec["sku"] = sku
        rec["updated_at_utc"] = _now_utc()
        db[sku] = rec
        self._write(data)
        return dict(rec)

    def approve(
        self,
        sku: str,
        *,
        title: str,
        category: str = "",
        product_type: str = "",
        description: str = "",
        tags: str = "",
        prompt1_version: int | None = None,
        prompt2_version: int | None = None,
        product_id: str = "",
        handle: str = "",
    ) -> dict[str, Any]:
        return self.update(
            sku,
            review_status="approved",
            title=title.strip(),
            category=category.strip(),
            product_type=product_type.strip(),
            description=description.strip(),
            tags=tags.strip(),
            approved_prompt1_version=prompt1_version,
            approved_prompt2_version=prompt2_version,
            product_id=product_id,
            handle=handle,
            last_error="",
        )

    def mark_uploaded(self, sku: str, *, shopify_media_ids: list[str] | None = None) -> dict[str, Any]:
        patch: dict[str, Any] = {
            "review_status": "uploaded",
            "upload_status": "uploaded",
            "last_error": "",
        }
        if shopify_media_ids is not None:
            patch["shopify_media_ids"] = shopify_media_ids
        return self.update(sku, **patch)

    def mark_failed(self, sku: str, error: str) -> dict[str, Any]:
        return self.update(
            sku,
            review_status="failed",
            upload_status="failed",
            last_error=str(error)[:2000],
        )

    def reset_uploaded_to_approved(self) -> int:
        """Demote all uploaded SKUs back to approved (clears upload tracking, keeps titles/media picks)."""
        count = 0
        for sku, rec in self.all_records().items():
            if str(rec.get("review_status") or "") != "uploaded":
                continue
            self.update(
                sku,
                review_status="approved",
                upload_status="pending",
                last_error="",
            )
            count += 1
        return count

    def all_records(self) -> dict[str, dict[str, Any]]:
        data = self._read()
        return dict(data.get("skus") or {})

    def product_names(self) -> dict[str, str]:
        """Approved/edited titles for XLSX export."""
        out: dict[str, str] = {}
        for sku, rec in self.all_records().items():
            title = str(rec.get("title") or "").strip()
            status = str(rec.get("review_status") or "")
            if title and status in {"approved", "uploaded"}:
                out[sku] = title
        return out
