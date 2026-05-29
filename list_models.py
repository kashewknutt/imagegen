from __future__ import annotations

import json

from src.config import load_config
from src.genai_client import GenAiImageClient


def main() -> None:
    cfg = load_config()
    client = GenAiImageClient(cfg.model, cfg.min_seconds_between_requests)
    models = client.list_models()
    print(json.dumps(models, indent=2))


if __name__ == "__main__":
    main()

