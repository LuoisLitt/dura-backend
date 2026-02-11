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
        """Convert stock value to int safely (API soms retourneert dict/str)."""
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            try:
                return int(val)
            except ValueError:
                return 0
        return 0

    async def get_in_stock_products(self, max_pages: int = 2500) -> list:
        """
        Haal ALLE producten met stock > 0 op door alle pagina's te scannen.
        Verwerkt in kleine batches met pauze om 429 rate limits te voorkomen.
        Returns lijst met alleen noodzakelijke velden per product.
        """
        import time as _time
        start = _time.monotonic()

        # Eerste pagina ophalen om lastPage te bepalen
        items_first, page_info = await self.get_products(limit=50, page=1)
        last_page = min(page_info.get("lastPage", 1), max_pages)
        total_api = page_info.get("totalItems", 0)

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

        if last_page <= 1:
            elapsed = round(_time.monotonic() - start, 1)
            print(f"[INVENTORY] Indexed {len(active)} active products from 1 page in {elapsed}s")
            return active

        # Sequentieel ophalen met korte pauze — Goedgepickt rate limit is streng
        MAX_RETRIES = 3

        async def fetch_page_with_retry(page_num: int) -> list:
            nonlocal errors
            for attempt in range(MAX_RETRIES):
                try:
                    items, _ = await self.get_products(limit=50, page=page_num)
                    return _extract_active(items)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429:
                        wait = 3 * (attempt + 1)  # 3s, 6s, 9s
                        print(f"[INVENTORY] Page {page_num} rate limited, waiting {wait}s (attempt {attempt+1})")
                        await asyncio.sleep(wait)
                        continue
                    print(f"[INVENTORY] Page {page_num} HTTP {e.response.status_code}")
                    errors += 1
                    return []
                except Exception as e:
                    print(f"[INVENTORY] Page {page_num} error: {e}")
                    errors += 1
                    return []
            print(f"[INVENTORY] Page {page_num} failed after {MAX_RETRIES} retries")
            errors += 1
            return []

        # Sequentieel: 1 request per keer, 0.3s pauze (~200 req/min)
        for page_num in range(2, last_page + 1):
            result = await fetch_page_with_retry(page_num)
            active.extend(result)
            await asyncio.sleep(0.3)

            # Progress log elke 200 pagina's
            if page_num % 200 == 0:
                elapsed_so_far = round(_time.monotonic() - start, 1)
                print(f"[INVENTORY] Progress: page {page_num}/{last_page}, {len(active)} active so far ({elapsed_so_far}s)")

        # Sorteer op stock (laagste eerst)
        active.sort(key=lambda p: p["stock"])

        elapsed = round(_time.monotonic() - start, 1)
        print(f"[INVENTORY] Indexed {len(active)} active products from {last_page} pages ({total_api} total, {errors} errors) in {elapsed}s")
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

    async def _fetch_today_orders_all_pages(self, today_str: str, max_pages: int = 50) -> list:
        """Haal alle orders van vandaag op (met page limit voor snelheid)."""
        items_first, pg_info = await self.get_orders(created_after=today_str, limit=50, page=1)
        if not items_first:
            return []
        last_page = pg_info.get("lastPage", 1)
        all_orders = list(items_first)
        
        if last_page > 1:
            # Fetch remaining pages in parallel (max 5 concurrent)
            remaining_pages = range(2, min(last_page + 1, max_pages + 1))
            tasks = [self.get_orders(created_after=today_str, limit=50, page=p) for p in remaining_pages]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, tuple) and len(r) == 2:
                    all_orders.extend(r[0])
        return all_orders

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
            return await self._fetch_today_orders_all_pages(today_str, max_pages=50)
        
        # Alles parallel
        (
            orders_today_count,
            orders_week_count,
            prev_week_total,
            shipments_today_count,
            shipments_week_count,
            prev_ship_total,
            today_orders_sample,
        ) = await asyncio.gather(
            self._safe_fetch(fetch_today_count(), fallback=0),
            self._safe_fetch(fetch_week_count(), fallback=0),
            self._safe_fetch(fetch_prev_week_count(), fallback=0),
            self._safe_fetch(fetch_ship_today(), fallback=0),
            self._safe_fetch(fetch_ship_week(), fallback=0),
            self._safe_fetch(fetch_prev_ship(), fallback=0),
            self._safe_fetch(fetch_today_orders(), fallback=[], timeout_sec=15.0),
        )
        
        prev_week_orders = max(0, (prev_week_total or 0) - (orders_week_count or 0))
        prev_week_shipments = max(0, (prev_ship_total or 0) - (shipments_week_count or 0))
        
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
        
        processed_statuses = {"shipped", "completed", "delivered"}
        processed_orders = sum(count for s, count in orders_by_status.items() if s in processed_statuses)
        
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
                    # Fetch remaining pages IN PARALLEL (max 50 pages)
                    max_pages = min(last_page, 50)
                    tasks = [self.get_orders(created_after=week_start, limit=50, page=p)
                             for p in range(2, max_pages + 1)]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in results:
                        if isinstance(r, Exception):
                            print(f"[Dashboard] Week revenue page error: {r}")
                            continue
                        items, _ = r
                        for o in items:
                            try:
                                total += float(o.get("totalPaid", "0") or "0")
                            except (ValueError, TypeError):
                                pass
                
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
        orders_per_day = {d: c for d, c in day_results if d is not None}
        
        return {
            "orders": {
                "today": orders_today_count or 0,
                "week": orders_week_count or 0,
                "by_status": orders_by_status,
                "processed": processed_orders
            },
            "shipments": {
                "today": shipments_today_count or 0,
                "week": shipments_week_count or 0
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
