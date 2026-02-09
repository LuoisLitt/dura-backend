"""
Goedgepickt API Connector
Documentatie: https://developers.goedgepickt.nl/
"""

import httpx
from datetime import datetime, timedelta
from typing import Optional
import os


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
    
    async def _request(self, method: str, endpoint: str, params: dict = None, data: dict = None) -> dict:
        """Maak een request naar de Goedgepickt API."""
        url = f"{self.BASE_URL}{endpoint}"
        
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method=method,
                url=url,
                headers=self.headers,
                params=params,
                json=data,
                timeout=30.0
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
        """Haal de nieuwste orders op via createdAfter + laatste pagina."""
        # Stap 1: Zoek orders van afgelopen 7 dagen
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        items, page_info = await self.get_orders(created_after=week_ago, limit=50, page=1)
        
        last_page = page_info.get("lastPage", 1)
        total = page_info.get("totalItems", 0)
        
        if last_page <= 1:
            items.sort(key=lambda x: x.get("createDate", ""), reverse=True)
            return items[:limit]
        
        # Stap 2: Haal de laatste pagina op (daar zitten de nieuwste)
        items, _ = await self.get_orders(created_after=week_ago, limit=50, page=last_page)
        
        # Stap 3: Als er ook een voorlaatste pagina is, pak die ook
        if last_page > 1:
            items2, _ = await self.get_orders(created_after=week_ago, limit=50, page=last_page - 1)
            items = items2 + items
        
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
                stock = p.get("stock", p.get("stockLevel", 0)) or 0
                if stock <= threshold:
                    low_stock.append(p)
            last_page = page_info.get("lastPage", 1)
            if page >= last_page:
                break
            page += 1
        # Sort: lowest stock first
        low_stock.sort(key=lambda p: p.get("stock", p.get("stockLevel", 0)) or 0)
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
        week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
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
    
    async def get_dashboard_stats(self) -> dict:
        """Verzamel alle statistieken voor het dashboard."""
        today_str = datetime.now().strftime("%Y-%m-%d")
        week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
        
        # Vorige week berekenen
        prev_week_start = (datetime.now() - timedelta(days=datetime.now().weekday() + 7)).strftime("%Y-%m-%d")
        prev_week_end = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
        
        # Orders vandaag + deze week (alleen counts via page=1 limit=1)
        _, today_info = await self.get_orders(created_after=today_str, limit=1, page=1)
        _, week_info = await self.get_orders(created_after=week_start, limit=1, page=1)
        
        # Vorige week orders count
        _, prev_week_info = await self.get_orders(created_after=prev_week_start, limit=1, page=1)
        prev_week_total = prev_week_info.get("totalItems", 0)
        this_week_total = week_info.get("totalItems", 0)
        # prev_week_total is alles SINCE prev_week_start (inclusief deze week), dus we moeten aftrekken
        prev_week_orders = max(0, prev_week_total - this_week_total)
        
        orders_today_count = today_info.get("totalItems", 0)
        orders_week_count = this_week_total
        
        # Status verdeling + extra data: tel over orders van vandaag (max 5 pagina's voor snelheid)
        orders_by_status = {}
        today_orders_sample = []  # Bewaar orders voor extra berekeningen
        page = 1
        max_pages = 5
        while page <= max_pages:
            items, pg_info = await self.get_orders(created_after=today_str, limit=50, page=page)
            if not items:
                break
            today_orders_sample.extend(items)
            for order in items:
                status = order.get("status", "unknown")
                orders_by_status[status] = orders_by_status.get(status, 0) + 1
            last_page = pg_info.get("lastPage", 1)
            if page >= last_page:
                break
            page += 1
        
        # === Feature 1: Omzet berekenen ===
        revenue_today = 0.0
        revenue_week = 0.0
        for order in today_orders_sample:
            try:
                paid = float(order.get("totalPaid", "0") or "0")
                revenue_today += paid
            except (ValueError, TypeError):
                pass
        
        # Week omzet: haal sample van week orders
        week_orders_sample = []
        wp = 1
        while wp <= 5:
            items, wpg = await self.get_orders(created_after=week_start, limit=50, page=wp)
            if not items:
                break
            week_orders_sample.extend(items)
            if wp >= wpg.get("lastPage", 1):
                break
            wp += 1
        
        for order in week_orders_sample:
            try:
                paid = float(order.get("totalPaid", "0") or "0")
                revenue_week += paid
            except (ValueError, TypeError):
                pass
        
        # Schaal op als we een sample hebben
        if len(week_orders_sample) > 0 and orders_week_count > len(week_orders_sample):
            scale = orders_week_count / len(week_orders_sample)
            revenue_week = revenue_week * scale
        
        # === Feature 2: Gemiddelde verwerkingstijd ===
        processing_times = []
        for order in today_orders_sample:
            create_date = order.get("createDate")
            finish_date = order.get("finishDate")
            if create_date and finish_date:
                try:
                    created = datetime.fromisoformat(create_date.replace("Z", "+00:00"))
                    finished = datetime.fromisoformat(finish_date.replace("Z", "+00:00"))
                    diff_minutes = (finished - created).total_seconds() / 60
                    if 0 < diff_minutes < 10080:  # Max 1 week
                        processing_times.append(diff_minutes)
                except (ValueError, TypeError):
                    pass
        
        avg_processing_minutes = 0
        if processing_times:
            avg_processing_minutes = sum(processing_times) / len(processing_times)
        
        # === Feature 4: Probleem orders ===
        problem_count = 0
        now = datetime.now()
        for order in today_orders_sample:
            # attentionNeeded flag
            if order.get("attentionNeeded") == 1 or order.get("attentionNeeded") == "1" or order.get("attentionNeeded") is True:
                problem_count += 1
                continue
            # Orders ouder dan 4 uur die nog niet afgerond zijn
            status = order.get("status", "")
            if status not in ("completed", "delivered", "shipped"):
                create_date = order.get("createDate")
                if create_date:
                    try:
                        created = datetime.fromisoformat(create_date.replace("Z", "+00:00"))
                        # Maak offset-naive voor vergelijking
                        created_naive = created.replace(tzinfo=None)
                        if (now - created_naive).total_seconds() > 4 * 3600:
                            problem_count += 1
                    except (ValueError, TypeError):
                        pass
        
        # === Feature 6: Top webshops ===
        webshop_counts = {}
        for order in today_orders_sample:
            shop = order.get("webshopName", "Onbekend")
            webshop_counts[shop] = webshop_counts.get(shop, 0) + 1
        
        top_webshops = sorted(webshop_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        top_webshops_list = [{"name": name, "count": count} for name, count in top_webshops]
        
        # Shipments vandaag + deze week
        _, ship_today_info = await self.get_shipments(created_after=today_str, limit=1, page=1)
        _, ship_week_info = await self.get_shipments(created_after=week_start, limit=1, page=1)
        shipments_today_count = ship_today_info.get("totalItems", 0)
        shipments_week_count = ship_week_info.get("totalItems", 0)
        
        # Vorige week shipments
        _, prev_ship_info = await self.get_shipments(created_after=prev_week_start, limit=1, page=1)
        prev_ship_total = prev_ship_info.get("totalItems", 0)
        prev_week_shipments = max(0, prev_ship_total - shipments_week_count)
        
        # Orders per dag deze week (voor chart)
        orders_per_day = {}
        current = datetime.strptime(week_start, "%Y-%m-%d")
        today_dt = datetime.now()
        while current <= today_dt:
            day_str = current.strftime("%Y-%m-%d")
            next_day = current + timedelta(days=1)
            _, day_info = await self.get_orders(created_after=day_str, limit=1, page=1)
            day_total = day_info.get("totalItems", 0)
            # Subtract next days if possible (createdAfter is inclusive)
            orders_per_day[day_str] = day_total
            current = next_day
        
        return {
            "orders": {
                "today": orders_today_count,
                "week": orders_week_count,
                "by_status": orders_by_status
            },
            "shipments": {
                "today": shipments_today_count,
                "week": shipments_week_count
            },
            "orders_per_day": orders_per_day,
            "revenue": {
                "today": round(revenue_today, 2),
                "week": round(revenue_week, 2)
            },
            "avg_processing_time": round(avg_processing_minutes, 1),
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
