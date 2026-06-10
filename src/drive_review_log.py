"""Terminal logging for the Drive review app (visible in Streamlit server console)."""
from __future__ import annotations

import logging
import sys

_LOG_NAME = "drive_review"


def get_logger() -> logging.Logger:
    log = logging.getLogger(_LOG_NAME)
    if not log.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [drive_review] %(levelname)s %(message)s", datefmt="%H:%M:%S")
        )
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def setup_drive_review_logging(level: int = logging.INFO) -> logging.Logger:
    log = get_logger()
    log.setLevel(level)
    return log
