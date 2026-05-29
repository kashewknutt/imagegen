from __future__ import annotations

from pathlib import Path


def base_key_from_stem(stem: str) -> str:
    """
    Treat trailing \"_<digits>\" as a variant suffix:
      DIARFHW26004_2 -> DIARFHW26004
    """
    parts = stem.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return stem


def base_key_from_path(path: Path) -> str:
    return base_key_from_stem(path.stem)

