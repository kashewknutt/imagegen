from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CsvEntry:
    sku: str
    title: str
    product_type: str
    media_links_raw: str


def iter_entries(csv_path: Path) -> list[CsvEntry]:
    """
    CSVs like your `1.csv`/`2.csv` contain multiple rows per SKU:
    - first row has SKU populated
    - subsequent rows often have SKU blank but have Media Type/Media Links
    We group all rows under the last non-empty SKU and keep the first row's metadata.
    """
    entries: dict[str, CsvEntry] = {}

    with csv_path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        current_sku: str | None = None
        for row in reader:
            sku = (row.get("SKU") or "").strip()
            if sku:
                current_sku = sku
                if sku not in entries:
                    entries[sku] = CsvEntry(
                        sku=sku,
                        title=(row.get("Title") or "").strip(),
                        product_type=(row.get("Product Type") or "").strip(),
                        media_links_raw=(row.get("Media Links") or "").strip(),
                    )
                else:
                    existing = entries[sku]
                    combined = ",".join(filter(None, [existing.media_links_raw, (row.get("Media Links") or "").strip()]))
                    entries[sku] = CsvEntry(
                        sku=existing.sku,
                        title=existing.title,
                        product_type=existing.product_type,
                        media_links_raw=combined,
                    )
            else:
                if not current_sku:
                    continue
                existing = entries[current_sku]
                extra = (row.get("Media Links") or "").strip()
                if extra:
                    combined = ",".join(filter(None, [existing.media_links_raw, extra]))
                    entries[current_sku] = CsvEntry(
                        sku=existing.sku,
                        title=existing.title,
                        product_type=existing.product_type,
                        media_links_raw=combined,
                    )

    return list(entries.values())
