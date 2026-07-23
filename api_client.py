# api_client.py
"""
Client-side data layer for the Telegram bot.

This talks to the SAME FastAPI backend (app.py) that index.html uses,
instead of reading static categoury.json / cofenet-items.json / docType.json
files. Results are cached in memory (with a TTL, just like app.py's own
SimpleCache) and mirrored to disk so the bot can still boot and serve
(slightly stale) data if the API is temporarily unreachable.

Design goals:
  - Behave like index.html: fetch categories / doc-types / the lightweight
    service list up front, then lazily fetch each service's full detail
    (documents/forms) only when a user actually opens that service -
    see buildDocsFromDetail() / serviceCache in index.html.
  - Never crash the bot just because the API is down - fall back to the
    last known-good in-memory data, then to the on-disk cache.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────
API_BASE = os.environ.get("COFENET_API_BASE", "http://127.0.0.1:8001")
COFENET_Server_URL = os.environ.get("COFENET_Server_URL", "https://cofenet-online.ir")
CACHE_DIR = Path(os.environ.get("COFENET_CACHE_DIR", "cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# typeId -> the English key the bot's UI logic (validation messages,
# is_image_document, etc.) expects. This is fixed/local rather than taken
# from the API's (Persian) doc-type titles, so existing bot logic keeps
# working no matter what language the backend's /api/doc-types returns.
DOC_TYPE_ID_TO_KEY = {
    1: "Text",
    2: "Number",
    3: "Image",
    4: "Sheba",
    5: "Mobile",
    6: "NationalCode",
    7: "PostalCode",
    8: "Date",
}

NO_DOCS_PLACEHOLDER = {
    "title": "نیازی به مدارک نیست",
    "typeId": 1,
    "type": "Text",
    "regex": ".*",
}


def _map_field_type_to_type_id(field_type: str) -> int:
    """Mirrors index.html's mapFieldTypeToTypeId()."""
    return {
        "nationalCode": 6,
        "date": 8,
        "file": 3,
        "number": 2,
    }.get(field_type, 1)


# ─────────────────────────────────────────────────────────────
#  DISK CACHE HELPERS (fallback for when the API is unreachable)
# ─────────────────────────────────────────────────────────────
def _cache_file(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def _read_disk_cache(name: str):
    try:
        with open(_cache_file(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_disk_cache(name: str, data: Any) -> None:
    try:
        tmp = _cache_file(name).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        tmp.replace(_cache_file(name))
    except Exception as e:
        logger.warning(f"Could not write disk cache '{name}': {e}")


class _TTLValue:
    """A single cached value with an expiry time."""
    __slots__ = ("value", "expires_at")

    def __init__(self):
        self.value = None
        self.expires_at = 0.0

    def is_fresh(self) -> bool:
        return self.value is not None and time.time() < self.expires_at

    def set(self, value: Any, ttl: float) -> None:
        self.value = value
        self.expires_at = time.time() + ttl


# ─────────────────────────────────────────────────────────────
#  MAIN DATA STORE
# ─────────────────────────────────────────────────────────────
class CofenetDataStore:
    CATEGORIES_TTL = 68400
    SERVICES_TTL = 68400
    DOC_TYPES_TTL = 68400
    SERVICE_DETAIL_TTL = 68400
    PAYMENT_INFO_TTL = 86400  # 24 hours

    def __init__(self, base_url: str = API_BASE):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=DEFAULT_TIMEOUT)

        self.categories: List[Dict] = []
        self.services: List[Dict] = []
        self.doc_types: List[Dict] = []
        self.category_names: Dict[int, str] = {}
        self.category_sort: Dict[int, int] = {}
        self.doc_type_map: Dict[int, Dict] = {}
        self.payment_info: Optional[Dict] = None  # <-- 新增

        self._cat_ttl = _TTLValue()
        self._svc_ttl = _TTLValue()
        self._doc_ttl = _TTLValue()
        self._payment_ttl = _TTLValue()  # <-- 新增
        self._detail_cache: Dict[str, _TTLValue] = {}
        self._category_id_by_name: Dict[str, int] = {}
        self._refresh_lock = asyncio.Lock()
    async def close(self):
        await self._client.aclose()

    async def _get_json(self, path: str, params: Optional[dict] = None):
        resp = await self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    # ---------------------------------------------------------
    #  Categories
    # ---------------------------------------------------------
    async def refresh_categories(self, force: bool = False) -> bool:
        if not force and self._cat_ttl.is_fresh():
            return True
        data = None
        try:
            fetched = await self._get_json("/api/categories")
            if isinstance(fetched, list) and fetched:
                data = fetched
                _write_disk_cache("categories", data)
        except Exception as e:
            logger.warning(f"Could not fetch /api/categories: {e}")

        if data is None:
            if self._cat_ttl.value is not None:
                logger.info("Serving stale in-memory categories (API unreachable)")
                return False
            data = _read_disk_cache("categories") or []
            if data:
                logger.info("Loaded categories from disk cache fallback")
            else:
                logger.error("No categories available: API down and no disk cache")

        self._cat_ttl.set(data, self.CATEGORIES_TTL)
        self.categories[:] = data
        self.category_names.clear()
        self.category_names.update({c["id"]: c["name"] for c in self.categories})
        self.category_sort.clear()
        self.category_sort.update({c["id"]: c.get("sort", 999) for c in self.categories})
        self._category_id_by_name = {c["name"]: c["id"] for c in self.categories}
        return True

    # ---------------------------------------------------------
    #  Document types
    # ---------------------------------------------------------
    async def refresh_doc_types(self, force: bool = False) -> bool:
        if not force and self._doc_ttl.is_fresh():
            return True
        data = None
        try:
            fetched = await self._get_json("/api/doc-types")
            if isinstance(fetched, list) and fetched:
                data = fetched
                _write_disk_cache("doc_types", data)
        except Exception as e:
            logger.warning(f"Could not fetch /api/doc-types: {e}")

        if data is None:
            if self._doc_ttl.value is not None:
                return False
            data = _read_disk_cache("doc_types") or []

        self._doc_ttl.set(data, self.DOC_TYPES_TTL)
        self.doc_types[:] = data
        self.doc_type_map.clear()
        self.doc_type_map.update({d["Id"]: d for d in self.doc_types})
        return True

    # ---------------------------------------------------------
    #  Services (lightweight list - full docs are fetched lazily)
    # ---------------------------------------------------------
    async def _fetch_all_services_live(self) -> Optional[List[Dict]]:
        items: List[Dict] = []
        cursor = None
        try:
            while True:
                params = {"limit": 300}
                if cursor:
                    params["cursor"] = cursor
                page = await self._get_json("/api/services/all", params=params)
                items.extend(page.get("items", []))
                if not page.get("hasMore") or not page.get("nextCursor"):
                    break
                cursor = page["nextCursor"]
                if len(items) > 20000:  # safety valve against a runaway loop
                    break
            return items
        except Exception as e:
            logger.warning(f"Could not fetch /api/services/all: {e}")
            return None

    def _adapt_service(self, item: Dict) -> Dict:
        cat_name = item.get("category")
        cat_id = self._category_id_by_name.get(cat_name)
        return {
            "Id": item.get("serviceId"),
            "Title": item.get("serviceTitle", ""),
            "Price": item.get("price", 0) or 0,
            "Sort": item.get("sort", 999),
            "Categories": [cat_id] if cat_id is not None else [],
            "IsActive": bool(item.get("isEnabled", True)),
            "IsTop10": bool(item.get("isSpecial", False)),
            "Duration": item.get("duration", ""),
            "Description": item.get("description", ""),
            "NoticeText": (item.get("data") or {}).get("noticeText") or '',
            "Documents": None,  # populated on demand, see get_service_documents()
        }

    async def refresh_services(self, force: bool = False) -> bool:
        if not force and self._svc_ttl.is_fresh():
            return True

        raw = await self._fetch_all_services_live()
        if raw is None:
            if self._svc_ttl.value is not None:
                return False
            raw = _read_disk_cache("services_raw") or []
        else:
            _write_disk_cache("services_raw", raw)

        adapted = [self._adapt_service(item) for item in raw]

        # Carry forward any documents we already fetched lazily, so we don't
        # re-hit /api/services/{id} for every service on every refresh.
        old_docs = {s["Id"]: s.get("Documents") for s in self.services if s.get("Documents")}
        for s in adapted:
            if s["Id"] in old_docs:
                s["Documents"] = old_docs[s["Id"]]

        self._svc_ttl.set(adapted, self.SERVICES_TTL)
        self.services[:] = adapted
        return True

    # ---------------------------------------------------------
    #  Full refresh (categories must load before services, for name->id map)
    # ---------------------------------------------------------
    async def refresh_payment_info(self, force: bool = False) -> bool:
        if not force and self._payment_ttl.is_fresh():
            return True
        
        data = None
        try:
            fetched = await self._get_json("/api/payment/info")
            if fetched and isinstance(fetched, dict):
                data = fetched
                _write_disk_cache("payment_info", data)
        except Exception as e:
            logger.warning(f"Could not fetch /api/payment/info: {e}")
        
        if data is None:
            if self._payment_ttl.value is not None:
                logger.info("Serving stale in-memory payment info (API unreachable)")
                return False
            data = _read_disk_cache("payment_info") or {}
            if data:
                logger.info("Loaded payment info from disk cache fallback")
            else:
                logger.error("No payment info available: API down and no disk cache")
                data = {
                    "cardNumber": "5041-7210-0916-7876",
                    "accountHolder": "محمد حسین نوابی",
                    "bankName": "بانک رسالت"
                }
        
        self._payment_ttl.set(data, self.PAYMENT_INFO_TTL)
        self.payment_info = data
        return True

    def get_payment_info(self) -> Dict:
        if self.payment_info:
            return self.payment_info
        return {
            "cardNumber": "5041-7210-0916-7876",
            "accountHolder": "محمد حسین نوابی",
            "bankName": "بانک رسالت"
        }

    async def refresh_all(self, force: bool = False) -> None:
        async with self._refresh_lock:
            await self.refresh_categories(force=force)
            await self.refresh_doc_types(force=force)
            await self.refresh_services(force=force)
            await self.refresh_payment_info(force=force)  # <-- 新增
    # ---------------------------------------------------------
    #  Per-service documents (lazy - mirrors index.html's serviceCache /
    #  buildDocsFromDetail, fetched only when a user opens a service)
    # ---------------------------------------------------------
    async def get_service_documents(self, service: Dict) -> List[Dict]:
        service_id = service.get("Id")

        cached = self._detail_cache.get(service_id)
        if cached and cached.is_fresh():
            service["Documents"] = cached.value
            return cached.value

        docs = await self._fetch_service_documents_live(service_id)
        if docs is None:
            if service.get("Documents"):
                return service["Documents"]
            disk = _read_disk_cache(f"service_detail_{service_id}")
            docs = disk if disk else [dict(NO_DOCS_PLACEHOLDER)]

        entry = self._detail_cache.setdefault(service_id, _TTLValue())
        entry.set(docs, self.SERVICE_DETAIL_TTL)
        service["Documents"] = docs
        return docs

    async def _fetch_service_documents_live(self, service_id: str) -> Optional[List[Dict]]:
        try:
            detail = await self._get_json(f"/api/services/{service_id}")
        except Exception as e:
            logger.warning(f"Could not fetch /api/services/{service_id}: {e}")
            return None

        forms = (detail.get("data") or {}).get("forms") or []
        form = forms[0] if forms else None
        fields = form.get("fields", []) if form else []

        if not fields:
            docs = [dict(NO_DOCS_PLACEHOLDER)]
        else:
            docs = []
            for f in fields:
                type_id = _map_field_type_to_type_id(f.get("type", "text"))
                dt = self.doc_type_map.get(type_id, {})
                docs.append({
                    "title": f.get("label", ""),
                    "placeholder": f.get("placeholder", ""),
                    "options": f.get("options", []),
                    "typeId": type_id,
                    "type": DOC_TYPE_ID_TO_KEY.get(type_id, "Text"),
                    "regex": dt.get("REx", ".*"),
                })

        _write_disk_cache(f"service_detail_{service_id}", docs)
        return docs


# Module-level singleton the bot imports and shares.
data_store = CofenetDataStore()
