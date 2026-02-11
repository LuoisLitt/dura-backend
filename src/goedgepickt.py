"""
Goedgepickt API Connector
Documentatie: https://developers.goedgepickt.nl/
"""

import asyncio
import httpx
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
import os

CET = ZoneInfo("Europe/Amsterdam")


class GoedgepicktAPI:
    """Connector voor Goedgepickt WMS API."""
    
    BASE_URL = "https://account.goedgepickt.nl/api/v1"
    
    def __init__(self, api_key: str, webshop_id: str):
        self.api_key = api_key
        self.webshop_id = webshop_id
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        self._http_client: Optional[httpx.AsyncClient] = None
    
    def _get_http_client(self) -> httpx.AsyncClient:
        """Hergebruik een enkele httpx client (connection pooling)."""
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                headers=self.headers,
                timeout=httpx.Timeout(15.0, connect=5.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._http_client
    
    async def _request(self, method: str, endpoint: str, params: dict = None, data: dict = None, timeout: float = 15.0) -> dict:
        """Maak een request naar de Goedgepickt API."""
        url = f"{self.BASE_URL}{endpoint}"
        client = self._get_http_client()
        response = await client.request(
            method=method,
            url=url,
            params=params,
            json=data,
            timeout=timeout
        )
        response.raise_for_status()
        return response.json()
    
    # ============ WEBSHOPS ============
    
    async def get_webshops(self) -> list:
        """Haal alle webshops op (pagineer door alle pagina's)."""
        all_webshops = []
        page = 1
        while True:
            result = await self._request("GET", "/webshops", params={"perPage": 50, "page": page})
            items = result.get("items", [])
            all_webshops.extend(items)
            
            page_info = result.get("pageInfo", {})
            last_page = page_info.get("lastPage", 1)
            
            if page >= last_page or not items:
                break
            page += 1
        
        return all_webshops
    
    # ============ ORDERS ============
    
    async def get_orders(
        self,
        status: Optional[str] = None,
        created_after: Optional[str] = None,
        limit: int = 50,
        page: int = 1
    ) -> tuple:
        """
        Haal orders op. Gebruik createdAfter voor recente orders.
        Returns (items, pageInfo).
        """
        params = {
            "perPage": min(limit, 50),  # Max 50 per pagina
            "page": page
        }

        if created_after:
            params["createdAfter"] = created_after
        if status:
            params["status"] = status

        result = await self._request("GET", "/orders", params=params)
        items = result.get("items", [])
        page_info = result.get("pageInfo", {})
        return items, page_info
    
    async def get_latest_orders(self, limit: int = 50) -> list:
        """Haal de nieuwste orders op - snel, max 1 API call."""
        # Alleen vandaag ophalen, dat is snel genoeg voor "recente orders"
        today_str = datetime.now(tz=CET).strftime("%Y-%m-%d")
        items, page_info = await self.get_orders(created_after=today_str, limit=50, page=1)
        
        items.sort(key=lambda x: x.get("createDate", ""), reverse=True)
        return items[:limit]
    
    async def get_order(self, order_uuid: str) -> dict:
        """Haal een specifieke order op."""
        return await self._request("GET", f"/orders/{order_uuid}")
    
    async def get_orders_since(self, since: str, limit: int = 50) -> tuple:
        """Haal orders op sinds datum. Returns (items, total_count)."""
        items, page_info = await self.get_orders(created_after=since, limit=limit, page=1)
        total = page_info.get("totalItems", len(items))
        return items, total
    
    # ============ PRODUCTEN / VOORRAAD ============
    
    async def get_products(self, limit: int = 50, page: int = 1) -> tuple:
        """
        Haal producten met voorraadinfo op. Max 50 per pagina.
        Returns (items, pageInfo).
        """
        params = {
            "webshopUuid": self.webshop_id,
            "perPage": min(limit, 50),
            "page": page
        }
        result = await self._request("GET", "/products", params=params)
        items = result.get("items", [])
        page_info = result.get("pageInfo", {})
        return items, page_info
    
    async def get_all_products(self, max_pages: int = 10) -> list:
        """Haal alle producten op (pagineer door meerdere pagina's)."""
        all_products = []
        page = 1
        while page <= max_pages:
            items, page_info = await self.get_products(limit=50, page=page)
            if not items:
                break
            all_products.extend(items)
            last_page = page_info.get("lastPage", 1)
            if page >= last_page:
                break
            page += 1
        return all_products
    
    async def get_product(self, product_uuid: str) -> dict:
        """Haal een specifiek product op."""
        return await self._request("GET", f"/products/{product_uuid}")
    
    @staticmethod
    def _safe_stock(val) -> int:
        """Convert stock value to int safely (API retourneert dict/str/int)."""
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                return 0
        if isinstance(val, dict):
            # Goedgepickt retourneert stock als {"freeStock": N, "totalStock": N}
            free = val.get("freeStock", val.get("totalStock", 0))
            if isinstance(free, (int, float)):
                return int(free)
            return 0
        return 0

    async def get_in_stock_products(self, max_pages: int = 2500) -> list:
        """
        Haal ALLE producten met stock > 0 op door alle pagina's te scannen.
        Batches van 5 concurrent requests met pauze om rate limits te voorkomen.
        Returns lijst met alleen noodzakelijke velden per product.
        """
        import sys
        import time as _time
        start = _time.monotonic()

        # Eerste pagina ophalen om lastPage te bepalen
        print("[INVENTORY] Fetching first page...", flush=True)
        sys.stdout.flush()
        items_first, page_info = await self.get_products(limit=50, page=1)
        last_page = min(page_info.get("lastPage", 1), max_pages)
        total_api = page_info.get("totalItems", 0)
        print(f"[INVENTORY] First page OK: {last_page} total pages, {total_api} products", flush=True)
        sys.stdout.flush()

        active = []
        errors = 0

        def _extract_active(items: list) -> list:
            result = []
            for p in items:
                stock = self._safe_stock(p.get("stock", p.get("stockLevel", 0)))
                if stock > 0:
                    result.append({
                        "uuid": p.get("uuid"),
                        "sku": p.get("sku", ""),
                        "name": p.get("name", ""),
                        "stock": stock,
                        "picture": p.get("picture"),
                    })
            return result

        # Verwerk eerste pagina
        active.extend(_extract_active(items_first))
        print(f"[INVENTORY] Page 1: {len(active)} active products found", flush=True)
        sys.stdout.flush()

        if last_page <= 1:
            elapsed = round(_time.monotonic() - start, 1)
            print(f"[INVENTORY] Done: {len(active)} active from 1 page in {elapsed}s", flush=True)
            return active

        # Batch ophalen: 2 concurrent requests per batch, 2s pauze (voorkom rate limits)
        BATCH_SIZE = 2
        MAX_RETRIES = 3

        async def fetch_page_with_retry(page_num: int) -> list:
            nonlocal errors
            for attempt in range(MAX_RETRIES):
                try:
                    items, _ = await self.get_products(limit=50, page=page_num)
                    return _extract_active(items)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429:
                        wait = 5 * (attempt + 1)  # 5s, 10s, 15s
                        if attempt == 0:
                            print(f"[INVENTORY] Rate limited at page {page_num}, waiting {wait}s", flush=True)
                        await asyncio.sleep(wait)
                        continue
                    errors += 1
                    return []
                except Exception as e:
                    if attempt == MAX_RETRIES - 1:
                        print(f"[INVENTORY] Page {page_num} failed: {e}", flush=True)
                    errors += 1
                    return []
            errors += 1
            return []

        # Batches van 5 pagina's tegelijk, 1s pauze tussen batches
        pages = list(range(2, last_page + 1))
        for batch_start in range(0, len(pages), BATCH_SIZE):
            batch = pages[batch_start:batch_start + BATCH_SIZE]
            results = await asyncio.gather(
                *[fetch_page_with_retry(p) for p in batch],
                return_exceptions=True
            )
            for r in results:
                if isinstance(r, list):
                    active.extend(r)
                elif isinstance(r, Exception):
                    errors += 1

            # 2s pauze tussen batches (rate limit budget)
            await asyncio.sleep(2.0)

            # Progress log elke 100 pagina's
            pages_done = batch_start + len(batch)
            if pages_done % 100 < BATCH_SIZE:
                elapsed_so_far = round(_time.monotonic() - start, 1)
                print(f"[INVENTORY] Progress: {pages_done}/{len(pages)} pages, {len(active)} active, {errors} errors ({elapsed_so_far}s)", flush=True)
                sys.stdout.flush()

        # Sorteer op stock (laagste eerst)
        active.sort(key=lambda p: p["stock"])

        elapsed = round(_time.monotonic() - start, 1)
        print(f"[INVENTORY] DONE: {len(active)} active products from {last_page} pages ({total_api} total, {errors} errors) in {elapsed}s", flush=True)
        sys.stdout.flush()
        return active

    async def get_low_stock_products(self, threshold: int = 25) -> list:
        """Haal producten met lage voorraad op (scan alle pagina's)."""
        low_stock = []
        page = 1
        max_pages = 20
        while page <= max_pages:
            items, page_info = await self.get_products(limit=50, page=page)
            if not items:
                break
            for p in items:
                stock = self._safe_stock(p.get("stock", p.get("stockLevel", 0)))
                if stock <= threshold:
                    low_stock.append({
                        "uuid": p.get("uuid"),
                        "sku": p.get("sku"),
                        "name": p.get("name"),
                        "stock": stock,
                        "minimalStock": p.get("stock", {}).get("minimalStock", 0) if isinstance(p.get("stock"), dict) else 0,
                        "picture": p.get("picture"),
                    })
            last_page = page_info.get("lastPage", 1)
            if page >= last_page:
                break
            page += 1
        # Sort: lowest stock first
        low_stock.sort(key=lambda p: p.get("stock", 0))
        return low_stock
    
    # ============ VERZENDINGEN ============
    
    async def get_shipments(
        self,
        created_after: Optional[str] = None,
        limit: int = 50,
        page: int = 1
    ) -> tuple:
        """Haal verzendingen op. Returns (items, pageInfo)."""
        params = {"perPage": min(limit, 50), "page": page}
        if created_after:
            params["createdAfter"] = created_after
        
        result = await self._request("GET", "/shipments", params=params)
        items = result.get("items", [])
        page_info = result.get("pageInfo", {})
        return items, page_info
    
    async def get_latest_shipments(self, limit: int = 50) -> list:
        """Haal nieuwste verzendingen op."""
        week_ago = (datetime.now(tz=CET) - timedelta(days=7)).strftime("%Y-%m-%d")
        items, page_info = await self.get_shipments(created_after=week_ago, limit=50, page=1)
        
        last_page = page_info.get("lastPage", 1)
        if last_page <= 1:
            items.sort(key=lambda x: x.get("createDate", ""), reverse=True)
            return items[:limit]
        
        items, _ = await self.get_shipments(created_after=week_ago, limit=50, page=last_page)
        if last_page > 1:
            items2, _ = await self.get_shipments(created_after=week_ago, limit=50, page=last_page - 1)
            items = items2 + items
        
        items.sort(key=lambda x: x.get("createDate", ""), reverse=True)
        return items[:limit]
    
    async def get_shipment_tracking(self, shipment_uuid: str) -> dict:
        """Haal tracking info voor een verzending op."""
        return await self._request("GET", f"/shipments/{shipment_uuid}/tracking")
    
    # ============ PICKS / MEDEWERKERS ============
    
    async def get_picks(
        self,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        user_uuid: Optional[str] = None
    ) -> list:
        """Haal pick-acties op (voor medewerker statistieken)."""
        params = {"webshopUuid": self.webshop_id}
        
        if date_from:
            params["createdAtFrom"] = date_from.strftime("%Y-%m-%d")
        if date_to:
            params["createdAtTo"] = date_to.strftime("%Y-%m-%d")
        if user_uuid:
            params["userUuid"] = user_uuid
        
        result = await self._request("GET", "/picks", params=params)
        return result.get("items", [])
    
    async def get_users(self) -> list:
        """Haal alle gebruikers/medewerkers op."""
        result = await self._request("GET", "/users")
        return result.get("items", [])
    
    # ============ STATISTIEKEN ============
    
    async def _safe_fetch(self, coro, fallback=None, timeout_sec: float = 15.0):
        """Voer een coroutine uit met timeout en fallback bij failure."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout_sec)
        except Exception as e:
            print(f"[SafeFetch] Failed ({type(e).__name__}): {e}")
            return fallback

    async def _fetch_page_with_retry(self, fetch_fn, label: str = "page", max_retries: int = 3):
        """Fetch een pagina met retry bij 429 rate limit."""
        for attempt in range(max_retries):
            try:
                return await fetch_fn()
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429 and attempt < max_retries - 1:
                    wait = 3 * (attempt + 1)
                    print(f"[Dashboard] Rate limited on {label}, waiting {wait}s")
                    await asyncio.sleep(wait)
                    continue
                raise
        return None

    async def _fetch_today_orders_all_pages(self, created_after: str, max_pages: int = 100) -> list:
        """Haal alle orders op. Stopt pas als een pagina leeg terugkomt (API metadata is onbetrouwbaar)."""
        items_first, pg_info = await self.get_orders(created_after=created_after, limit=50, page=1)
        if not items_first:
            return []
        all_orders = list(items_first)

        api_last_page = pg_info.get("lastPage", 1)
        api_total = pg_info.get("totalItems", 0)
        print(f"[Orders] Page 1: {len(items_first)} items, API says totalItems={api_total} lastPage={api_last_page}", flush=True)

        # Test: ook proberen MET webshopUuid om verschil te zien
        try:
            _, pg_info_ws = await self.get_orders(created_after=created_after, limit=1, page=1)
            # Nu met webshopUuid
            params_ws = {"webshopUuid": self.webshop_id, "perPage": 1, "page": 1, "createdAfter": created_after}
            result_ws = await self._request("GET", "/orders", params=params_ws)
            ws_total = result_ws.get("pageInfo", {}).get("totalItems", 0)
            ws_last = result_ws.get("pageInfo", {}).get("lastPage", 0)
            print(f"[Orders] WITH webshopUuid: totalItems={ws_total} lastPage={ws_last}", flush=True)
            print(f"[Orders] WITHOUT webshopUuid: totalItems={api_total} lastPage={api_last_page}", flush=True)
        except Exception as e:
            print(f"[Orders] webshopUuid test failed: {e}", flush=True)

        current_page = 2
        BATCH_SIZE = 2
        empty_count = 0

        while current_page <= max_pages and empty_count == 0:
            batch = list(range(current_page, min(current_page + BATCH_SIZE, max_pages + 1)))
            if not batch:
                break
            tasks = [
                self._fetch_page_with_retry(
                    lambda p=p: self.get_orders(created_after=created_after, limit=50, page=p),
                    label=f"orders p{p}"
                ) for p in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            batch_items = 0
            for r in results:
                if isinstance(r, tuple) and len(r) == 2 and r[0]:
                    all_orders.extend(r[0])
                    batch_items += len(r[0])
                else:
                    empty_count += 1
            current_page += BATCH_SIZE
            if batch_items == 0:
                break
            await asyncio.sleep(1.5)

        print(f"[Orders] TOTAL: Fetched {len(all_orders)} orders, stopped at page {current_page-1} (API said lastPage={api_last_page}, totalItems={api_total})", flush=True)
        return all_orders

    async def _fetch_all_shipments(self, created_after: str, max_pages: int = 100) -> list:
        """Haal alle shipments op. Stopt pas als een pagina leeg terugkomt (API metadata is onbetrouwbaar)."""
        items_first, pg_info = await self.get_shipments(created_after=created_after, limit=50, page=1)
        if not items_first:
            return []
        all_shipments = list(items_first)

        api_last_page = pg_info.get("lastPage", 1)
        current_page = 2
        BATCH_SIZE = 2
        empty_count = 0

        while current_page <= max_pages and empty_count == 0:
            batch = list(range(current_page, min(current_page + BATCH_SIZE, max_pages + 1)))
            if not batch:
                break
            tasks = [
                self._fetch_page_with_retry(
                    lambda p=p: self.get_shipments(created_after=created_after, limit=50, page=p),
                    label=f"shipments p{p}"
                ) for p in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            batch_items = 0
            for r in results:
                if isinstance(r, tuple) and len(r) == 2 and r[0]:
                    all_shipments.extend(r[0])
                    batch_items += len(r[0])
                else:
                    empty_count += 1
            current_page += BATCH_SIZE
            if batch_items == 0:
                break
            await asyncio.sleep(1.5)

        if len(all_shipments) > 50:
            print(f"[Dashboard] Fetched {len(all_shipments)} shipments (API said lastPage={api_last_page})", flush=True)
        return all_shipments

    async def get_dashboard_stats(self) -> dict:
        """Verzamel alle statistieken voor het dashboard. Parallel waar mogelijk."""
        import asyncio
        
        today_str = datetime.now(tz=CET).strftime("%Y-%m-%d")
        week_start = (datetime.now(tz=CET) - timedelta(days=datetime.now(tz=CET).weekday())).strftime("%Y-%m-%d")
        prev_week_start = (datetime.now(tz=CET) - timedelta(days=datetime.now(tz=CET).weekday() + 7)).strftime("%Y-%m-%d")
        
        # ========== FASE 1: Parallel basis-counts ophalen ==========
        async def fetch_today_count():
            _, info = await self.get_orders(created_after=today_str, limit=1, page=1)
            return info.get("totalItems", 0)
        
        async def fetch_week_count():
            _, info = await self.get_orders(created_after=week_start, limit=1, page=1)
            return info.get("totalItems", 0)
        
        async def fetch_prev_week_count():
            _, info = await self.get_orders(created_after=prev_week_start, limit=1, page=1)
            return info.get("totalItems", 0)
        
        async def fetch_ship_today():
            _, info = await self.get_shipments(created_after=today_str, limit=1, page=1)
            return info.get("totalItems", 0)
        
        async def fetch_ship_week():
            _, info = await self.get_shipments(created_after=week_start, limit=1, page=1)
            return info.get("totalItems", 0)
        
        async def fetch_prev_ship():
            _, info = await self.get_shipments(created_after=prev_week_start, limit=1, page=1)
            return info.get("totalItems", 0)
        
        async def fetch_today_orders():
            return await self._fetch_today_orders_all_pages(today_str, max_pages=100)

        async def fetch_processed_count():
            """Exacte processed count via status-specifieke API queries."""
            statuses = ["shipped", "completed", "delivered"]
            tasks = [self.get_orders(created_after=today_str, status=s, limit=1, page=1) for s in statuses]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            total = 0
            by_status = {}
            for s, r in zip(statuses, results):
                if isinstance(r, tuple) and len(r) == 2:
                    count = r[1].get("totalItems", 0)
                    total += count
                    by_status[s] = count
            return total, by_status

        async def fetch_today_shipments_all():
            """Alle shipments van vandaag voor carrier verdeling."""
            return await self._fetch_all_shipments(today_str, max_pages=50)

        # Alles parallel
        (
            orders_today_count,
            orders_week_count,
            prev_week_total,
            shipments_today_count,
            shipments_week_count,
            prev_ship_total,
            today_orders_sample,
            processed_result,
            today_shipments,
        ) = await asyncio.gather(
            self._safe_fetch(fetch_today_count(), fallback=0),
            self._safe_fetch(fetch_week_count(), fallback=0),
            self._safe_fetch(fetch_prev_week_count(), fallback=0),
            self._safe_fetch(fetch_ship_today(), fallback=0),
            self._safe_fetch(fetch_ship_week(), fallback=0),
            self._safe_fetch(fetch_prev_ship(), fallback=0),
            self._safe_fetch(fetch_today_orders(), fallback=[], timeout_sec=60.0),
            self._safe_fetch(fetch_processed_count(), fallback=(0, {})),
            self._safe_fetch(fetch_today_shipments_all(), fallback=[], timeout_sec=60.0),
        )
        
        prev_week_orders = max(0, (prev_week_total or 0) - (orders_week_count or 0))
        prev_week_shipments = max(0, (prev_ship_total or 0) - (shipments_week_count or 0))

        # Unpack exacte processed count van status-specifieke API queries
        if isinstance(processed_result, tuple):
            processed_exact, processed_by_status = processed_result
        else:
            processed_exact, processed_by_status = 0, {}
        
        # Override counts met werkelijk opgehaald aantal (API totalItems is vaak vertraagd/gecacht)
        if today_orders_sample and len(today_orders_sample) > 0:
            actual_count = len(today_orders_sample)
            if actual_count > (orders_today_count or 0):
                print(f"[Dashboard] Orders today: API reported {orders_today_count}, actual fetched {actual_count}", flush=True)
                orders_today_count = actual_count

        if today_shipments and len(today_shipments) > 0:
            actual_ship = len(today_shipments)
            if actual_ship > (shipments_today_count or 0):
                print(f"[Dashboard] Shipments today: API reported {shipments_today_count}, actual fetched {actual_ship}", flush=True)
                shipments_today_count = actual_ship

        # ========== FASE 2: Bereken stats uit opgehaalde data (CPU only, geen API) ==========
        orders_by_status = {}
        revenue_today = 0.0
        processing_times = []
        problem_count = 0
        webshop_counts = {}
        now = datetime.now(tz=CET)
        
        for order in (today_orders_sample or []):
            # Status
            status = order.get("status", "unknown")
            orders_by_status[status] = orders_by_status.get(status, 0) + 1
            
            # Revenue
            try:
                revenue_today += float(order.get("totalPaid", "0") or "0")
            except (ValueError, TypeError):
                pass
            
            # Processing time
            create_date = order.get("createDate")
            finish_date = order.get("finishDate")
            if create_date and finish_date:
                try:
                    created = datetime.fromisoformat(create_date.replace("Z", "+00:00"))
                    finished = datetime.fromisoformat(finish_date.replace("Z", "+00:00"))
                    diff_minutes = (finished - created).total_seconds() / 60
                    if 0 < diff_minutes < 10080:
                        processing_times.append(diff_minutes)
                except (ValueError, TypeError):
                    pass
            
            # Problem orders
            if order.get("attentionNeeded") in (1, "1", True):
                problem_count += 1
            elif status not in ("completed", "delivered", "shipped") and create_date:
                try:
                    created = datetime.fromisoformat(create_date.replace("Z", "+00:00"))
                    if (now - created).total_seconds() > 4 * 3600:
                        problem_count += 1
                except (ValueError, TypeError):
                    pass
            
            # Webshops
            shop = order.get("webshopName", "Onbekend")
            webshop_counts[shop] = webshop_counts.get(shop, 0) + 1
        
        revenue_today = round(revenue_today, 2)
        avg_processing_minutes = round(sum(processing_times) / len(processing_times), 1) if processing_times else 0
        
        top_webshops = sorted(webshop_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        top_webshops_list = [{"name": name, "count": count} for name, count in top_webshops]
        
        # Gebruik exacte API count voor processed orders (niet sample-based)
        if processed_exact > 0:
            processed_orders = processed_exact
            for s, c in processed_by_status.items():
                orders_by_status[s] = c
        else:
            processed_statuses = {"shipped", "completed", "delivered"}
            processed_orders = sum(count for s, count in orders_by_status.items() if s in processed_statuses)

        # Carrier verdeling uit alle shipments van vandaag (genormaliseerd)
        carrier_counts = {}
        for s in (today_shipments or []):
            raw = (s.get("shippingMethod") or s.get("shippingCarrier")
                   or s.get("carrier") or s.get("carrierName") or "Onbekend").lower()
            if "dhl" in raw:
                cname = "DHL"
            elif "postnl" in raw or "tnt" in raw:
                cname = "PostNL"
            elif "dpd" in raw:
                cname = "DPD"
            else:
                cname = raw[:20].title() if raw else "Onbekend"
            carrier_counts[cname] = carrier_counts.get(cname, 0) + 1
        top_carriers = sorted(carrier_counts.items(), key=lambda x: x[1], reverse=True)
        
        # ========== FASE 3: Week revenue + orders per dag (parallel) ==========
        # Week revenue
        if week_start == today_str:
            revenue_week = revenue_today
        else:
            async def fetch_week_revenue():
                # Page 1 first to get lastPage
                items_first, pg_info = await self.get_orders(created_after=week_start, limit=50, page=1)
                if not items_first:
                    print("[Dashboard] Week revenue: €0.00 from 0 pages")
                    return 0.0
                
                total = 0.0
                for o in items_first:
                    try:
                        total += float(o.get("totalPaid", "0") or "0")
                    except (ValueError, TypeError):
                        pass
                
                last_page = pg_info.get("lastPage", 1)
                if last_page > 1:
                    # Batched fetch: 3 pages tegelijk met retry
                    max_pg = min(last_page, 50)
                    pages = list(range(2, max_pg + 1))
                    BATCH = 3
                    for b_start in range(0, len(pages), BATCH):
                        batch = pages[b_start:b_start + BATCH]
                        tasks = [
                            self._fetch_page_with_retry(
                                lambda p=p: self.get_orders(created_after=week_start, limit=50, page=p),
                                label=f"revenue p{p}"
                            ) for p in batch
                        ]
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        for r in results:
                            if isinstance(r, Exception) or r is None:
                                continue
                            items, _ = r
                            for o in items:
                                try:
                                    total += float(o.get("totalPaid", "0") or "0")
                                except (ValueError, TypeError):
                                    pass
                        await asyncio.sleep(1.5)
                
                print(f"[Dashboard] Week revenue: €{total:.2f} from {last_page} pages (parallel)")
                return round(total, 2)
            revenue_week = await self._safe_fetch(fetch_week_revenue(), fallback=None, timeout_sec=60.0)
            if revenue_week is None:
                # Fallback: tel revenue_today, maar log warning
                print("[Dashboard] WARN: week revenue fetch failed, falling back to today revenue")
                revenue_week = revenue_today
        
        # Orders per dag — parallel fetch voor elke dag
        current = datetime.strptime(week_start, "%Y-%m-%d").replace(tzinfo=CET)
        today_dt = datetime.now(tz=CET)
        day_strs = []
        while current <= today_dt:
            day_strs.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
        
        async def fetch_day_count(day_str):
            _, info = await self.get_orders(created_after=day_str, limit=1, page=1)
            return (day_str, info.get("totalItems", 0))
        
        day_results = await asyncio.gather(
            *[self._safe_fetch(fetch_day_count(d), fallback=(d, 0)) for d in day_strs]
        )
        cumulative_counts = {d: c for d, c in day_results if d is not None}

        # Bereken per-dag aantallen (verschil opeenvolgende cumulatieve totalen)
        sorted_days = sorted(cumulative_counts.keys())
        orders_per_day = {}
        for i, day in enumerate(sorted_days):
            if i < len(sorted_days) - 1:
                next_day = sorted_days[i + 1]
                orders_per_day[day] = max(0, cumulative_counts[day] - cumulative_counts[next_day])
            else:
                # Laatste dag (vandaag): cumulatief getal IS het dagaantal
                orders_per_day[day] = cumulative_counts[day]
        
        return {
            "orders": {
                "today": orders_today_count or 0,
                "week": orders_week_count or 0,
                "by_status": orders_by_status,
                "processed": processed_orders
            },
            "shipments": {
                "today": shipments_today_count or 0,
                "week": shipments_week_count or 0,
                "by_carrier": carrier_counts,
                "top_carriers": [{"name": name, "count": count} for name, count in top_carriers[:10]],
            },
            "orders_per_day": orders_per_day,
            "revenue": {
                "today": round(revenue_today, 2),
                "week": round(revenue_week, 2)
            },
            "avg_processing_time": avg_processing_minutes,
            "problem_orders": problem_count,
            "top_webshops": top_webshops_list,
            "prev_week": {
                "orders": prev_week_orders,
                "shipments": prev_week_shipments
            }
        }
    
    async def test_connection(self) -> bool:
        """Test of de API credentials werken."""
        try:
            await self._request("GET", "/orders", params={"perPage": 1})
            return True
        except Exception as e:
            print(f"Connection test failed: {e}")
            return False


# Singleton instance
_client: Optional[GoedgepicktAPI] = None


def get_client() -> GoedgepicktAPI:
    """Haal de Goedgepickt client op (singleton)."""
    global _client
    if _client is None:
        api_key = os.getenv("GOEDGEPICKT_API_KEY")
        webshop_id = os.getenv("GOEDGEPICKT_WEBSHOP_ID")
        
        if not api_key or not webshop_id:
            raise ValueError("GOEDGEPICKT_API_KEY en GOEDGEPICKT_WEBSHOP_ID moeten ingesteld zijn")
        
        _client = GoedgepicktAPI(api_key, webshop_id)
    
    return _client
