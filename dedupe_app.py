from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import streamlit as st
from PIL import Image

from src.config import load_config
from src.image_resolve import SUPPORTED_EXTS


@dataclass(frozen=True)
class Candidate:
    path: Path
    size_bytes: int
    pixels: int


def _base_key(stem: str) -> str:
    # Treat trailing "_<digits>" as a suffix variant.
    # Example: "DIARFHW26004_2" -> "DIARFHW26004"
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return stem


def _safe_open(path: Path) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


def _scan(images_dir: Path) -> Dict[str, List[Candidate]]:
    groups: Dict[str, List[Candidate]] = {}
    for p in sorted(images_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        base = _base_key(p.stem)
        try:
            size_bytes = p.stat().st_size
        except Exception:
            size_bytes = 0
        img = _safe_open(p)
        pixels = 0 if img is None else int(img.width * img.height)
        groups.setdefault(base, []).append(Candidate(path=p, size_bytes=size_bytes, pixels=pixels))
    return groups


def _suggest_keep(cands: List[Candidate]) -> Path:
    # Heuristic: prefer highest pixels, then largest file size, then stable name.
    cands_sorted = sorted(cands, key=lambda c: (-c.pixels, -c.size_bytes, c.path.name))
    return cands_sorted[0].path


def _move_to_removed(path: Path, removed_dir: Path) -> Path:
    removed_dir.mkdir(parents=True, exist_ok=True)
    dest = removed_dir / path.name
    if dest.exists():
        # avoid clobber
        i = 1
        while True:
            candidate = removed_dir / f"{path.stem}__dup{i}{path.suffix}"
            if not candidate.exists():
                dest = candidate
                break
            i += 1
    shutil.move(str(path), str(dest))
    return dest


def main() -> None:
    cfg = load_config()
    images_dir = cfg.images_dir
    removed_dir = cfg.outputs_dir / "_removed_images"

    st.set_page_config(page_title="Deduplicate SKU Images", layout="wide")
    st.title("Deduplicate SKU Images")
    st.caption(f"Scanning: `{images_dir}`  |  Move removed → `{removed_dir}`")

    if not images_dir.exists():
        st.error(f"images_dir not found: {images_dir}")
        return

    groups = _scan(images_dir)
    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}

    st.sidebar.header("Duplicate Groups")
    st.sidebar.caption(f"Groups with duplicates: {len(dup_groups)}")

    if not dup_groups:
        st.success("No duplicate groups found (no *_1/_2/_3 variants detected).")
        return

    keys = sorted(dup_groups.keys())
    chosen_key = st.sidebar.selectbox("Choose SKU group", keys)
    cands = dup_groups[chosen_key]

    st.subheader(f"{chosen_key} ({len(cands)} candidates)")

    suggested = _suggest_keep(cands)
    st.info(f"Suggested keep: `{suggested.name}` (highest resolution/size heuristic)")

    if "keep_map" not in st.session_state or st.session_state.get("keep_key") != chosen_key:
        st.session_state.keep_key = chosen_key
        st.session_state.keep_map = {str(c.path): (c.path == suggested) for c in cands}

    cols = st.columns(min(4, len(cands)))
    for idx, cand in enumerate(cands):
        col = cols[idx % len(cols)]
        with col:
            st.code(cand.path.name)
            img = _safe_open(cand.path)
            if img is None:
                st.warning("Unreadable image")
            else:
                st.image(img, width="stretch")
            st.caption(f"{cand.pixels:,} px | {cand.size_bytes/1_000_000:.2f} MB")
            st.session_state.keep_map[str(cand.path)] = st.checkbox(
                "Keep",
                value=bool(st.session_state.keep_map.get(str(cand.path), False)),
                key=f"keep::{chosen_key}::{cand.path.name}",
            )

    keep_paths = [Path(p) for p, keep in st.session_state.keep_map.items() if keep]
    remove_paths = [Path(p) for p, keep in st.session_state.keep_map.items() if not keep]

    st.divider()
    left, mid, right = st.columns([1, 1, 2])
    with left:
        if st.button("Keep only suggested", use_container_width=True):
            st.session_state.keep_map = {str(c.path): (c.path == suggested) for c in cands}
            st.rerun()
    with mid:
        if st.button("Keep all", use_container_width=True):
            st.session_state.keep_map = {str(c.path): True for c in cands}
            st.rerun()
    with right:
        st.write(f"Keeping: {len(keep_paths)} | Removing: {len(remove_paths)}")

    st.warning("Remove action moves files out of `dslr_shots/` into `outputs/_removed_images/` (not permanent delete).")
    if st.button("Apply (move removed files)", type="primary", use_container_width=True, disabled=(len(remove_paths) == 0)):
        moved: List[Tuple[str, str]] = []
        for p in remove_paths:
            if p.exists():
                dest = _move_to_removed(p, removed_dir)
                moved.append((p.name, dest.name))
        st.success(f"Moved {len(moved)} files.")
        st.session_state.pop("keep_map", None)
        st.session_state.pop("keep_key", None)
        st.rerun()


if __name__ == "__main__":
    main()

