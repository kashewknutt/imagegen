"""Load Shopify credentials from environment variables."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from src.shopify_client import ShopifyClient, ShopifyConfig
from src.shopify_settings import ShopifySettings, load_shopify_settings, save_shopify_settings
from src.shopify_token_cache import CachedToken, load_cached_token, save_cached_token


def _normalize_shop_domain(domain: str) -> str:
    d = (domain or "").strip().replace("https://", "").replace("http://", "").rstrip("/")
    if not d:
        return ""
    if d.endswith("/admin"):
        d = d[: -len("/admin")]
    if not d.endswith(".myshopify.com"):
        d = f"{d}.myshopify.com"
    return d


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


def load_shopify_env(*, env_path: Path | None = None) -> ShopifyEnvConfig:
    if env_path and env_path.is_file():
        load_dotenv(env_path, override=False)
    else:
        load_dotenv(override=False)
    return ShopifyEnvConfig(
        shop_domain=_normalize_shop_domain(
            os.getenv("SHOPIFY_SHOP_DOMAIN") or os.getenv("shopify_shop_domain") or ""
        ),
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


def shopify_client_from_env(outputs_dir: Path, *, env_path: Path | None = None) -> ShopifyClient | None:
    env = load_shopify_env(env_path=env_path)
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


@dataclass
class ShopifyConnectionState:
    connected: bool
    client: ShopifyClient | None = None
    shop_name: str = ""
    shop_domain: str = ""
    error: str = ""
    missing_env: list[str] = field(default_factory=list)

    @property
    def status_label(self) -> str:
        if self.connected and self.shop_name:
            return f"Connected — {self.shop_name}"
        if self.missing_env:
            return f"Not configured — missing {', '.join(self.missing_env)}"
        if self.error:
            return f"Connection failed — {self.error}"
        return "Not connected"


def _missing_shopify_env_vars(env: ShopifyEnvConfig) -> list[str]:
    missing: list[str] = []
    if not env.shop_domain:
        missing.append("SHOPIFY_SHOP_DOMAIN")
    if not env.access_token:
        if not env.client_id:
            missing.append("SHOPIFY_CLIENT_ID")
        if not env.client_secret:
            missing.append("SHOPIFY_CLIENT_SECRET")
    return missing


def ensure_shopify_connection(
    outputs_dir: Path,
    *,
    env_path: Path | None = None,
) -> ShopifyConnectionState:
    """
    Load .env credentials, obtain/refresh token, and ping Shopify.
    Called on every app load so connection status is never ambiguous.
    """
    env = load_shopify_env(env_path=env_path)
    missing = _missing_shopify_env_vars(env)
    if missing:
        return ShopifyConnectionState(
            connected=False,
            shop_domain=env.shop_domain,
            error="Set required variables in .env (see .env.example).",
            missing_env=missing,
        )
    try:
        client = shopify_client_from_env(outputs_dir, env_path=env_path)
        if client is None:
            return ShopifyConnectionState(
                connected=False,
                shop_domain=env.shop_domain,
                error="Shopify client could not be created from .env.",
                missing_env=missing,
            )
        shop_name = client.ping()
        return ShopifyConnectionState(
            connected=True,
            client=client,
            shop_name=str(shop_name or env.shop_domain),
            shop_domain=env.shop_domain,
        )
    except Exception as e:
        return ShopifyConnectionState(
            connected=False,
            shop_domain=env.shop_domain,
            error=str(e),
            missing_env=missing,
        )
