from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import requests


@dataclass(frozen=True)
class ShopifyConfig:
    shop_domain: str  # e.g. "example.myshopify.com"
    admin_access_token: str
    api_version: str = "2024-01"


class ShopifyClient:
    def __init__(self, cfg: ShopifyConfig) -> None:
        self.cfg = cfg

    def _endpoint(self) -> str:
        domain = self.cfg.shop_domain.strip().replace("https://", "").replace("http://", "")
        return f"https://{domain}/admin/api/{self.cfg.api_version}/graphql.json"

    def _rest_base(self) -> str:
        domain = self.cfg.shop_domain.strip().replace("https://", "").replace("http://", "")
        return f"https://{domain}/admin/api/{self.cfg.api_version}"

    def _rest_headers(self) -> dict[str, str]:
        return {"X-Shopify-Access-Token": self.cfg.admin_access_token, "Content-Type": "application/json"}

    @staticmethod
    def oauth_token_client_credentials(*, shop_domain: str, client_id: str, client_secret: str) -> dict[str, Any]:
        """
        Dev Dashboard apps can use client credentials grant to fetch a 24h Admin API token.
        POST https://{shop}.myshopify.com/admin/oauth/access_token
          grant_type=client_credentials
          client_id=...
          client_secret=...
        """
        domain = shop_domain.strip().replace("https://", "").replace("http://", "")
        if domain.endswith("/admin"):
            domain = domain[: -len("/admin")]
        if not domain.endswith(".myshopify.com"):
            domain = f"{domain}.myshopify.com"
        url = f"https://{domain}/admin/oauth/access_token"
        r = requests.post(
            url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Token request failed HTTP {r.status_code}: {r.text[:500]}")
        if r.status_code >= 400:
            raise RuntimeError(f"Token request failed HTTP {r.status_code}: {data}")
        # Expected: access_token, scope, expires_in (seconds, ~86399)
        return data

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": self.cfg.admin_access_token,
        }
        payload = {"query": query, "variables": variables or {}}
        r = requests.post(self._endpoint(), headers=headers, data=json.dumps(payload), timeout=60)
        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"Shopify GraphQL HTTP {r.status_code}: {r.text[:500]}")
        if r.status_code >= 400:
            raise RuntimeError(f"Shopify GraphQL HTTP {r.status_code}: {data}")
        if "errors" in data and data["errors"]:
            raise RuntimeError(f"Shopify GraphQL errors: {data['errors']}")
        return data.get("data") or {}

    def ping(self) -> str:
        data = self.graphql("query { shop { name } }")
        shop = (data.get("shop") or {}).get("name") or ""
        return str(shop)

    def product_create(
        self,
        *,
        title: str,
        description_html: str,
        vendor: str = "",
        product_type: str = "",
        category_gid: str | None = None,
        tags: list[str] | None = None,
        status: str | None = "ACTIVE",
    ) -> dict[str, str]:
        # Note: On some Admin API versions, ProductCreateInput does NOT accept a `variants` field.
        # We create the product first, then update the default variant with SKU/price.
        query = """
        mutation ProductCreate($product: ProductCreateInput!) {
          productCreate(product: $product) {
            product {
              id
              handle
              title
              variants(first: 1) { edges { node { id sku inventoryItem { id } } } }
            }
            userErrors { field message }
          }
        }
        """
        base_product: dict[str, Any] = {
            "title": title,
            "descriptionHtml": description_html,
            **({"status": status} if status else {}),
            **({"vendor": vendor} if vendor else {}),
            **({"productType": product_type} if product_type else {}),
            **({"tags": tags} if tags else {}),
            **({"category": category_gid} if category_gid else {}),
        }

        # Some API versions don't support the `category` field. If Shopify returns a schema error,
        # retry without it rather than failing the whole upload.
        try:
            data = self.graphql(query, {"product": base_product})
        except RuntimeError as e:
            msg = str(e)
            if category_gid and ("category" in msg and "Field is not defined" in msg):
                base_product.pop("category", None)
                data = self.graphql(query, {"product": base_product})
            else:
                raise
        payload = (data.get("productCreate") or {})
        errs = payload.get("userErrors") or []
        if errs:
            raise RuntimeError(f"productCreate userErrors: {errs}")
        prod = (payload.get("product") or {})
        variant_id = ""
        inventory_item_id = ""
        try:
            edges = (((prod.get("variants") or {}).get("edges")) or [])
            if edges:
                node = (edges[0].get("node") or {})
                variant_id = str(node.get("id") or "")
                inv = node.get("inventoryItem") or {}
                if isinstance(inv, dict):
                    inventory_item_id = str(inv.get("id") or "")
        except Exception:
            pass

        return {
            "id": str(prod.get("id") or ""),
            "handle": str(prod.get("handle") or ""),
            "title": str(prod.get("title") or ""),
            "variant_id": variant_id,
            "inventory_item_id": inventory_item_id,
        }

    def taxonomy_search_categories(self, *, search: str, first: int = 25) -> list[dict[str, str]]:
        """
        Returns a list of taxonomy categories matching the search string.
        Each item includes: id (GID), name, fullName.
        """
        query = """
        query TaxonomyCategories($search: String!, $first: Int!) {
          taxonomy {
            categories(search: $search, first: $first) {
              edges {
                node { id name fullName }
              }
            }
          }
        }
        """
        data = self.graphql(query, {"search": search, "first": int(first)})
        edges = (((data.get("taxonomy") or {}).get("categories") or {}).get("edges") or [])
        out: list[dict[str, str]] = []
        for e in edges:
            node = (e.get("node") or {})
            out.append(
                {
                    "id": str(node.get("id") or ""),
                    "name": str(node.get("name") or ""),
                    "fullName": str(node.get("fullName") or ""),
                }
            )
        return [r for r in out if r.get("id")]

    def metafield_definitions_for_category(self, *, category_gid: str, first: int = 50) -> list[dict[str, str]]:
        """
        Returns metafield definitions applicable to PRODUCTS of the given taxonomy category.
        Useful for Shopify "category metafields" (standard definitions).
        """
        query = """
        query CategoryMetafieldDefinitions($first: Int!, $subtype: MetafieldDefinitionConstraintSubtypeIdentifierInput!) {
          metafieldDefinitions(
            first: $first,
            ownerType: PRODUCT,
            constraintSubtype: $subtype,
            constraintStatus: CONSTRAINED_ONLY
          ) {
            edges {
              node {
                name
                namespace
                key
                type { name }
              }
            }
          }
        }
        """
        data = self.graphql(
            query,
            {"first": int(first), "subtype": {"key": "category", "value": category_gid}},
        )
        edges = ((data.get("metafieldDefinitions") or {}).get("edges") or [])
        out: list[dict[str, str]] = []
        for e in edges:
            node = (e.get("node") or {})
            t = node.get("type") or {}
            tname = ""
            if isinstance(t, dict):
                tname = str(t.get("name") or "")
            out.append(
                {
                    "name": str(node.get("name") or ""),
                    "namespace": str(node.get("namespace") or ""),
                    "key": str(node.get("key") or ""),
                    "type": tname,
                }
            )
        return [x for x in out if x.get("namespace") and x.get("key") and x.get("type")]

    def product_update_title(self, *, product_id: str, title: str) -> dict[str, str]:
        """Update a product's title via productUpdate."""
        mutation = """
        mutation ProductUpdateTitle($input: ProductInput!) {
          productUpdate(input: $input) {
            product { id title handle }
            userErrors { field message }
          }
        }
        """
        data = self.graphql(mutation, {"input": {"id": product_id, "title": title}})
        payload = data.get("productUpdate") or {}
        errs = payload.get("userErrors") or []
        if errs:
            raise RuntimeError(f"productUpdate userErrors: {errs}")
        prod = payload.get("product") or {}
        return {
            "id": str(prod.get("id") or product_id),
            "title": str(prod.get("title") or title),
            "handle": str(prod.get("handle") or ""),
        }

    def product_update_metafields(self, *, product_id: str, metafields: list[dict[str, str]]) -> None:
        """
        Sets metafields on a product using productUpdate. Each metafield item must include:
          namespace, key, type, value
        """
        mutation = """
        mutation ProductUpdateMetafields($input: ProductInput!) {
          productUpdate(input: $input) {
            product { id }
            userErrors { field message }
          }
        }
        """
        data = self.graphql(mutation, {"input": {"id": product_id, "metafields": metafields}})
        payload = data.get("productUpdate") or {}
        errs = payload.get("userErrors") or []
        if errs:
            raise RuntimeError(f"productUpdate userErrors: {errs}")

    def rest_locations(self) -> list[dict[str, Any]]:
        url = f"{self._rest_base()}/locations.json"
        r = requests.get(url, headers={"X-Shopify-Access-Token": self.cfg.admin_access_token}, timeout=30)
        data = r.json() if r.text else {}
        if r.status_code >= 400:
            raise RuntimeError(f"Locations HTTP {r.status_code}: {data}")
        return list(data.get("locations") or [])

    def rest_inventory_set(self, *, location_id: int, inventory_item_id: int, available: int) -> None:
        url = f"{self._rest_base()}/inventory_levels/set.json"
        payload = {"location_id": int(location_id), "inventory_item_id": int(inventory_item_id), "available": int(available)}
        r = requests.post(url, headers=self._rest_headers(), data=json.dumps(payload), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Inventory set HTTP {r.status_code}: {r.text[:500]}")

    def rest_inventory_item_cost(self, *, inventory_item_id: int, cost: float) -> None:
        url = f"{self._rest_base()}/inventory_items/{int(inventory_item_id)}.json"
        payload = {"inventory_item": {"id": int(inventory_item_id), "cost": str(cost), "tracked": True}}
        r = requests.put(url, headers=self._rest_headers(), data=json.dumps(payload), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Inventory item update HTTP {r.status_code}: {r.text[:500]}")

    def rest_variant_weight(self, *, variant_id: int, weight_kg: float) -> None:
        url = f"{self._rest_base()}/variants/{int(variant_id)}.json"
        payload = {"variant": {"id": int(variant_id), "weight": float(weight_kg), "weight_unit": "kg"}}
        r = requests.put(url, headers=self._rest_headers(), data=json.dumps(payload), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Variant update HTTP {r.status_code}: {r.text[:500]}")

    def rest_variant_update(self, *, variant_id: int, sku: str | None = None, price: str | None = None) -> None:
        url = f"{self._rest_base()}/variants/{int(variant_id)}.json"
        v: dict[str, Any] = {"id": int(variant_id)}
        if sku is not None and str(sku).strip():
            v["sku"] = str(sku).strip()
        if price is not None and str(price).strip():
            v["price"] = str(price).strip()
        payload = {"variant": v}
        r = requests.put(url, headers=self._rest_headers(), data=json.dumps(payload), timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"Variant update HTTP {r.status_code}: {r.text[:500]}")

    def staged_upload_create(
        self,
        *,
        filename: str,
        mime_type: str,
        resource: str = "FILE",
        file_size: int | None = None,
        http_method: str = "POST",
    ) -> dict[str, Any]:
        query = """
        mutation StagedUploadsCreate($input: [StagedUploadInput!]!) {
          stagedUploadsCreate(input: $input) {
            stagedTargets { url resourceUrl parameters { name value } }
            userErrors { field message }
          }
        }
        """
        inp: dict[str, Any] = {"resource": resource, "filename": filename, "mimeType": mime_type, "httpMethod": http_method}
        if file_size is not None:
            inp["fileSize"] = int(file_size)
        data = self.graphql(query, {"input": [inp]})
        payload = (data.get("stagedUploadsCreate") or {})
        errs = payload.get("userErrors") or []
        if errs:
            raise RuntimeError(f"stagedUploadsCreate userErrors: {errs}")
        targets = payload.get("stagedTargets") or []
        if not targets:
            raise RuntimeError("stagedUploadsCreate returned no stagedTargets")
        return targets[0]

    def upload_to_staged_target(self, *, target: dict[str, Any], filename: str, mime_type: str, file_bytes: bytes) -> None:
        """
        Uploads bytes to a staged target. Shopify commonly returns a `url` + form `parameters`
        for a POST upload (GCS/S3). Using those parameters is required to avoid 403.
        """
        url = str(target.get("url") or "")
        if not url:
            raise RuntimeError("staged target missing url/resourceUrl")
        data: dict[str, str] = {}
        for p in (target.get("parameters") or []):
            try:
                n = str(p.get("name") or "")
                v = str(p.get("value") or "")
                if n:
                    data[n] = v
            except Exception:
                continue
        files = {"file": (filename, file_bytes, mime_type)}
        r = requests.post(url, data=data, files=files, timeout=300)
        if r.status_code >= 400:
            raise RuntimeError(f"staged upload failed {r.status_code}: {r.text[:500]}")

    def file_create_from_staged(self, *, resource_url: str, alt: str, content_type: str) -> str:
        query = """
        mutation CreateFileFromStaged($files: [FileCreateInput!]!) {
          fileCreate(files: $files) {
            files { id fileStatus alt }
            userErrors { field message }
          }
        }
        """
        data = self.graphql(
            query,
            {
                "files": [
                    {
                        "originalSource": resource_url,
                        "alt": alt,
                        "contentType": content_type,
                    }
                ]
            },
        )
        payload = (data.get("fileCreate") or {})
        errs = payload.get("userErrors") or []
        if errs:
            raise RuntimeError(f"fileCreate userErrors: {errs}")
        files = payload.get("files") or []
        if not files:
            raise RuntimeError("fileCreate returned no files")
        return str(files[0].get("id") or "")

    def file_poll_ready(self, *, file_id: str, max_tries: int = 60, sleep_seconds: float = 2.0) -> dict[str, str]:
        import time

        query = """
        query FileStatus($id: ID!) {
          node(id: $id) {
            ... on File {
              fileStatus
              preview { status image { url } }
            }
          }
        }
        """
        last = {}
        for _ in range(max_tries):
            data = self.graphql(query, {"id": file_id})
            node = data.get("node") or {}
            status = str(node.get("fileStatus") or "")
            preview = node.get("preview") or {}
            img = (preview.get("image") or {}) if isinstance(preview, dict) else {}
            url = str((img or {}).get("url") or "")
            last = {"fileStatus": status, "preview_url": url}
            # For images, preview image URL is sufficient to attach as MediaImage.
            # For videos, callers should attach using the staged resource URL (or other known URL).
            if status == "READY":
                return last
            if status == "FAILED":
                raise RuntimeError(f"File processing FAILED for {file_id}")
            time.sleep(sleep_seconds)
        return last

    def product_set_featured_media(self, *, product_id: str, media_id: str) -> None:
        """Set the product thumbnail/featured image to an existing media item."""
        mutation = """
        mutation ProductSetFeaturedMedia($input: ProductInput!) {
          productUpdate(input: $input) {
            product { id featuredImage { url } }
            userErrors { field message }
          }
        }
        """
        data = self.graphql(mutation, {"input": {"id": product_id, "featuredMediaId": media_id}})
        payload = data.get("productUpdate") or {}
        errs = payload.get("userErrors") or []
        if errs:
            raise RuntimeError(f"productUpdate featuredMediaId userErrors: {errs}")

    def product_reorder_media(self, *, product_id: str, moves: list[dict[str, str]]) -> None:
        """Reorder product media. Each move: {id: media_id, newPosition: \"0\"}."""
        mutation = """
        mutation ProductReorderMedia($id: ID!, $moves: [MoveInput!]!) {
          productReorderMedia(id: $id, moves: $moves) {
            job { id }
            mediaUserErrors { field message }
          }
        }
        """
        data = self.graphql(mutation, {"id": product_id, "moves": moves})
        payload = data.get("productReorderMedia") or {}
        errs = payload.get("mediaUserErrors") or []
        if errs:
            raise RuntimeError(f"productReorderMedia userErrors: {errs}")

    def product_create_media(self, *, product_id: str, media: list[dict[str, str]]) -> list[dict[str, str]]:
        query = """
        mutation ProductCreateMedia($id: ID!, $media: [CreateMediaInput!]!) {
          productCreateMedia(productId: $id, media: $media) {
            media { id status alt ... on MediaImage { image { url } } }
            mediaUserErrors { field message }
          }
        }
        """
        data = self.graphql(query, {"id": product_id, "media": media})
        payload = (data.get("productCreateMedia") or {})
        errs = payload.get("mediaUserErrors") or []
        if errs:
            raise RuntimeError(f"productCreateMedia userErrors: {errs}")
        out: list[dict[str, str]] = []
        for m in (payload.get("media") or []):
            mid = str(m.get("id") or "")
            status = str(m.get("status") or "")
            alt = str(m.get("alt") or "")
            img = m.get("image") or {}
            url = ""
            if isinstance(img, dict):
                url = str(img.get("url") or "")
            out.append({"id": mid, "status": status, "alt": alt, "url": url})
        return out

    def collection_find_by_title(self, *, title: str) -> dict[str, str] | None:
        query = """
        query FindCollection($q: String!) {
          collections(first: 1, query: $q) {
            edges { node { id title handle } }
          }
        }
        """
        q = f"title:{title}"
        data = self.graphql(query, {"q": q})
        edges = ((data.get("collections") or {}).get("edges") or [])
        if not edges:
            return None
        node = (edges[0].get("node") or {})
        return {"id": str(node.get("id") or ""), "title": str(node.get("title") or ""), "handle": str(node.get("handle") or "")}

    def collection_create(self, *, title: str, handle: str | None = None, rule_set: dict[str, Any] | None = None) -> dict[str, str]:
        query = """
        mutation CollectionCreate($input: CollectionInput!) {
          collectionCreate(input: $input) {
            collection { id title handle }
            userErrors { field message }
          }
        }
        """
        inp: dict[str, Any] = {"title": title}
        if handle:
            inp["handle"] = handle
        if rule_set:
            inp["ruleSet"] = rule_set
        data = self.graphql(query, {"input": inp})
        payload = (data.get("collectionCreate") or {})
        errs = payload.get("userErrors") or []
        if errs:
            raise RuntimeError(f"collectionCreate userErrors: {errs}")
        col = payload.get("collection") or {}
        return {"id": str(col.get("id") or ""), "title": str(col.get("title") or ""), "handle": str(col.get("handle") or "")}

    def collection_create_smart_by_tag(self, *, title: str, tag: str, handle: str | None = None) -> dict[str, str]:
        """
        Creates an automated (smart) collection by providing a ruleSet.
        Uses a single rule: TAG EQUALS <tag>
        """
        rule_set = {
            "appliedDisjunctively": False,
            "rules": [{"column": "TAG", "relation": "EQUALS", "condition": tag}],
        }
        return self.collection_create(title=title, handle=handle, rule_set=rule_set)

    def collection_create_smart_by_product_type(self, *, title: str, product_type: str, handle: str | None = None) -> dict[str, str]:
        """
        Creates an automated (smart) collection by product type.
        CollectionRuleColumn uses TYPE for product type in Shopify Admin GraphQL.
        """
        rule_set = {
            "appliedDisjunctively": False,
            "rules": [{"column": "TYPE", "relation": "EQUALS", "condition": product_type}],
        }
        return self.collection_create(title=title, handle=handle, rule_set=rule_set)

    def list_products(
        self,
        *,
        first: int = 10,
        after: str | None = None,
        query: str | None = None,
        media_first: int = 50,
    ) -> dict[str, Any]:
        """
        List products with metadata and media for inventory review.
        Returns: {products: [...], pageInfo: {hasNextPage, hasPreviousPage, startCursor, endCursor}}
        """
        gql = """
        query ListProducts($first: Int!, $after: String, $query: String, $mediaFirst: Int!) {
          products(first: $first, after: $after, query: $query) {
            pageInfo {
              hasNextPage
              hasPreviousPage
              startCursor
              endCursor
            }
            edges {
              cursor
              node {
                id
                title
                handle
                descriptionHtml
                productType
                status
                category { fullName name }
                featuredImage { url altText }
                media(first: $mediaFirst) {
                  edges {
                    node {
                      ... on MediaImage {
                        id
                        alt
                        mediaContentType
                        status
                        image { url altText }
                      }
                    }
                  }
                }
                variants(first: 5) {
                  edges { node { sku } }
                }
              }
            }
          }
        }
        """
        variables: dict[str, Any] = {"first": int(first), "mediaFirst": int(media_first)}
        if after:
            variables["after"] = after
        if query:
            variables["query"] = query
        data = self.graphql(gql, variables)
        payload = data.get("products") or {}
        page_info = payload.get("pageInfo") or {}
        products: list[dict[str, Any]] = []
        for edge in (payload.get("edges") or []):
            node = edge.get("node") or {}
            media_items: list[dict[str, str]] = []
            for me in ((node.get("media") or {}).get("edges") or []):
                mnode = me.get("node") or {}
                mid = str(mnode.get("id") or "")
                if not mid:
                    continue
                img = mnode.get("image") or {}
                url = ""
                if isinstance(img, dict):
                    url = str(img.get("url") or "")
                media_items.append(
                    {
                        "id": mid,
                        "url": url,
                        "alt": str(mnode.get("alt") or ""),
                        "status": str(mnode.get("status") or ""),
                        "content_type": str(mnode.get("mediaContentType") or "IMAGE"),
                    }
                )
            skus: list[str] = []
            for ve in ((node.get("variants") or {}).get("edges") or []):
                sku = str(((ve.get("node") or {}).get("sku") or "")).strip()
                if sku:
                    skus.append(sku)
            cat = node.get("category") or {}
            category_name = ""
            if isinstance(cat, dict):
                category_name = str(cat.get("fullName") or cat.get("name") or "")
            feat = node.get("featuredImage") or {}
            featured_url = ""
            if isinstance(feat, dict):
                featured_url = str(feat.get("url") or "")
            product_type = str(node.get("productType") or "")
            title = str(node.get("title") or "")
            primary_image_url = featured_url
            if not primary_image_url:
                for m in media_items:
                    if str(m.get("url") or "").strip():
                        primary_image_url = str(m.get("url") or "")
                        break
            products.append(
                {
                    "id": str(node.get("id") or ""),
                    "title": title,
                    "handle": str(node.get("handle") or ""),
                    "description_html": str(node.get("descriptionHtml") or ""),
                    "product_type": product_type,
                    "status": str(node.get("status") or ""),
                    "category": category_name,
                    "featured_image_url": featured_url,
                    "primary_image_url": primary_image_url,
                    "skus": skus,
                    "sku": str(skus[0] if skus else ""),
                    "media": media_items,
                    "cursor": str(edge.get("cursor") or ""),
                }
            )
        return {
            "products": products,
            "pageInfo": {
                "hasNextPage": bool(page_info.get("hasNextPage")),
                "hasPreviousPage": bool(page_info.get("hasPreviousPage")),
                "startCursor": str(page_info.get("startCursor") or ""),
                "endCursor": str(page_info.get("endCursor") or ""),
            },
        }

    def delete_product_media(self, *, product_id: str, media_ids: list[str]) -> list[str]:
        """Delete one or more media items from a product. Returns deleted media IDs."""
        if not media_ids:
            return []
        mutation = """
        mutation ProductDeleteMedia($productId: ID!, $mediaIds: [ID!]!) {
          productDeleteMedia(productId: $productId, mediaIds: $mediaIds) {
            deletedMediaIds
            mediaUserErrors { field message }
          }
        }
        """
        data = self.graphql(mutation, {"productId": product_id, "mediaIds": media_ids})
        payload = data.get("productDeleteMedia") or {}
        errs = payload.get("mediaUserErrors") or []
        if errs:
            raise RuntimeError(f"productDeleteMedia userErrors: {errs}")
        return [str(x) for x in (payload.get("deletedMediaIds") or [])]

    def upload_image_bytes(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        mime_type: str = "image/jpeg",
        alt: str = "",
        max_poll_tries: int = 30,
        poll_sleep_seconds: float = 2.0,
    ) -> str:
        """
        Stage-upload image bytes, create a Shopify file, poll until READY, and return CDN URL.
        """
        target = self.staged_upload_create(filename=filename, mime_type=mime_type, resource="FILE", http_method="POST")
        self.upload_to_staged_target(target=target, filename=filename, mime_type=mime_type, file_bytes=file_bytes)
        resource_url = str(target.get("resourceUrl") or target.get("url") or "")
        if not resource_url:
            raise RuntimeError(f"staged upload missing resourceUrl: {target}")
        file_id = self.file_create_from_staged(resource_url=resource_url, alt=alt, content_type="IMAGE")
        ready = self.file_poll_ready(file_id=file_id, max_tries=max_poll_tries, sleep_seconds=poll_sleep_seconds)
        cdn = str(ready.get("preview_url") or "").strip()
        if not cdn:
            raise RuntimeError(f"File did not become READY: {ready}")
        return cdn

    def attach_product_image(self, *, product_id: str, image_url: str, alt: str = "") -> list[dict[str, str]]:
        """Attach an existing CDN URL as product media."""
        return self.product_create_media(
            product_id=product_id,
            media=[{"mediaContentType": "IMAGE", "originalSource": image_url, "alt": alt}],
        )

    def replace_product_image(
        self,
        *,
        product_id: str,
        old_media_id: str,
        file_bytes: bytes,
        filename: str,
        mime_type: str = "image/jpeg",
        alt: str = "",
    ) -> dict[str, Any]:
        """
        Upload new image, attach to product, then delete old media.
        Upload-first avoids leaving the product without images if attach fails.
        """
        cdn_url = self.upload_image_bytes(
            file_bytes=file_bytes,
            filename=filename,
            mime_type=mime_type,
            alt=alt,
        )
        attached = self.attach_product_image(product_id=product_id, image_url=cdn_url, alt=alt)
        deleted = self.delete_product_media(product_id=product_id, media_ids=[old_media_id])
        return {"cdn_url": cdn_url, "attached": attached, "deleted_media_ids": deleted}
