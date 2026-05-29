from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_RELS_NS = {"r": "http://schemas.openxmlformats.org/package/2006/relationships"}


@dataclass(frozen=True)
class XlsxRow:
    sheet: str
    row_index_1based: int
    values: dict[str, Any]


def list_sheets(xlsx_path: Path) -> list[str]:
    with zipfile.ZipFile(xlsx_path, "r") as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        return [sh.attrib.get("name", "") for sh in wb.findall("m:sheets/m:sheet", _NS)]


def iter_rows(
    xlsx_path: Path,
    sheets: list[str],
    *,
    max_rows_per_sheet: int = 0,
) -> list[XlsxRow]:
    """
    Minimal XLSX reader (no openpyxl dependency). Supports shared strings and inline values.
    Assumes first non-empty row is the header for each sheet.
    """
    with zipfile.ZipFile(xlsx_path, "r") as z:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid_to_target = {r.attrib["Id"]: r.attrib["Target"] for r in rels.findall("r:Relationship", _RELS_NS)}

        # shared strings table
        shared: list[str] = []
        if "xl/sharedStrings.xml" in z.namelist():
            sroot = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in sroot.findall("m:si", _NS):
                text = "".join(t.text or "" for t in si.findall(".//m:t", _NS))
                shared.append(text)

        name_to_sheet_xml: dict[str, str] = {}
        for sh in wb.findall("m:sheets/m:sheet", _NS):
            name = sh.attrib.get("name", "")
            rid = sh.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rid_to_target.get(rid)
            if not name or not target:
                continue
            name_to_sheet_xml[name] = "xl/" + target.lstrip("/")

        def cell_value(c: ET.Element) -> str:
            t = c.attrib.get("t")
            v = c.find("m:v", _NS)
            if v is None or v.text is None:
                return ""
            raw = v.text
            if t == "s":
                try:
                    return shared[int(raw)]
                except Exception:
                    return raw
            return raw

        def col_index_from_ref(cell_ref: str) -> int:
            # "C12" -> 3
            letters = ""
            for ch in cell_ref:
                if ch.isalpha():
                    letters += ch.upper()
                else:
                    break
            idx = 0
            for ch in letters:
                idx = idx * 26 + (ord(ch) - ord("A") + 1)
            return idx

        out: list[XlsxRow] = []
        for sheet_name in sheets:
            sheet_xml = name_to_sheet_xml.get(sheet_name)
            if not sheet_xml or sheet_xml not in z.namelist():
                continue
            root = ET.fromstring(z.read(sheet_xml))
            sheet_rows = root.findall(".//m:sheetData/m:row", _NS)

            headers: list[str] = []
            header_row_idx = None

            def read_row_cells(row_el: ET.Element) -> dict[int, str]:
                cells: dict[int, str] = {}
                for c in row_el.findall("m:c", _NS):
                    ref = c.attrib.get("r", "")
                    if not ref:
                        continue
                    col = col_index_from_ref(ref)
                    cells[col] = cell_value(c)
                return cells

            # Find header row: first row with at least 3 non-empty cells
            for r in sheet_rows:
                cells = read_row_cells(r)
                non_empty = [v for v in cells.values() if str(v).strip() != ""]
                if len(non_empty) >= 3:
                    max_col = max(cells.keys()) if cells else 0
                    headers = [cells.get(i, "").strip() for i in range(1, max_col + 1)]
                    # Normalize header names; fallback to colN for blanks
                    headers = [h if h else f"col{i}" for i, h in enumerate(headers, start=1)]
                    header_row_idx = int(r.attrib.get("r", "0") or 0)
                    break

            if not headers or not header_row_idx:
                continue

            count = 0
            for r in sheet_rows:
                ridx = int(r.attrib.get("r", "0") or 0)
                if ridx <= header_row_idx:
                    continue
                cells = read_row_cells(r)
                if not cells:
                    continue
                # Build row dict
                values: dict[str, Any] = {}
                max_col = max(cells.keys())
                for i in range(1, max_col + 1):
                    key = headers[i - 1] if i - 1 < len(headers) else f"col{i}"
                    values[key] = cells.get(i, "")
                # Skip fully blank rows
                if all(str(v).strip() == "" for v in values.values()):
                    continue
                out.append(XlsxRow(sheet=sheet_name, row_index_1based=ridx, values=values))
                count += 1
                if max_rows_per_sheet and count >= max_rows_per_sheet:
                    break

        return out


def index_by_sku(rows: list[XlsxRow], sku_column: str = "SKU") -> dict[str, XlsxRow]:
    out: dict[str, XlsxRow] = {}
    for r in rows:
        sku = str(r.values.get(sku_column, "")).strip()
        if not sku:
            continue
        if sku not in out:
            out[sku] = r
    return out

