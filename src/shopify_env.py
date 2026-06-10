"""Load Shopify credentials from environment variables."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from src.shopify_client import ShopifyClient, ShopifyConfig
from src.shopify_settings import ShopifySettings, load_shopify_settings, save_shopify_settings
from src.shopify_token_cache import CachedToken, load_cached_token, save_cached_token


@dataclass(frozen=True)
class ShopifyEnvConfig:
    shop_domain: str
    client_id: str
    client_secret: str
    access_token: str
    api_version: str

    @property
    def configured(self) -> bool:
        if not self.shop_domain:
            return False
        if self.access_token:
            return True
        return bool(self.client_id and self.client_secret)


def load_shopify_env() -> ShopifyEnvConfig:
    load_dotenv(override=False)
    return ShopifyEnvConfig(
        shop_domain=(os.getenv("SHOPIFY_SHOP_DOMAIN") or os.getenv("shopify_shop_domain") or "").strip(),
        client_id=(os.getenv("SHOPIFY_CLIENT_ID") or os.getenv("shopify_client_id") or "").strip(),
        client_secret=(os.getenv("SHOPIFY_CLIENT_SECRET") or os.getenv("shopify_client_secret") or "").strip(),
        access_token=(os.getenv("SHOPIFY_ACCESS_TOKEN") or os.getenv("shopify_access_token") or "").strip(),
        api_version=(os.getenv("SHOPIFY_API_VERSION") or os.getenv("shopify_api_version") or "2024-01").strip()
        or "2024-01",
    )


def _resolve_access_token(env: ShopifyEnvConfig, outputs_dir: Path) -> str:
    if env.access_token:
        return env.access_token

    settings_path = outputs_dir / ".shopify_settings.json"
    cache_path = outputs_dir / ".shopify_token_cache.json"
    cache_key = f"{env.shop_domain}|{env.client_id}"

    if env.shop_domain and env.client_id:
        try:
            save_shopify_settings(
                settings_path,
                ShopifySettings(
                    shop_domain=env.shop_domain,
                    client_id=env.client_id,
                    api_version=env.api_version,
                ),
            )
        except Exception:
            pass

    cached = load_cached_token(cache_path, cache_key) if (env.shop_domain and env.client_id) else None
    if cached and cached.access_token:
        return cached.access_token

    if env.client_id and env.client_secret:
        data = ShopifyClient.oauth_token_client_credentials(
            shop_domain=env.shop_domain,
            client_id=env.client_id,
            client_secret=env.client_secret,
        )
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError(f"Token response missing access_token: {data}")
        save_cached_token(
            cache_path,
            cache_key,
            CachedToken(
                access_token=token,
                expires_at_epoch=time.time() + float(data.get("expires_in") or 0),
                scope=str(data.get("scope") or ""),
            ),
        )
        return token

    saved = load_shopify_settings(settings_path)
    if saved and env.shop_domain and saved.shop_domain == env.shop_domain:
        cached = load_cached_token(cache_path, f"{saved.shop_domain}|{saved.client_id}")
        if cached and cached.access_token:
            return cached.access_token

    raise RuntimeError(
        "Missing Shopify token. Set SHOPIFY_ACCESS_TOKEN or SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET in .env"
    )


def shopify_client_from_env(outputs_dir: Path) -> ShopifyClient | None:
    env = load_shopify_env()
    if not env.configured:
        return None
    token = _resolve_access_token(env, outputs_dir)
    return ShopifyClient(
        ShopifyConfig(
            shop_domain=env.shop_domain,
            admin_access_token=token,
            api_version=env.api_version,
        )
    )
