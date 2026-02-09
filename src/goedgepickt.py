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
            "perPage": limit,
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
    
    async def get_products(self, limit: int = 500) -> list:
        """Haal alle producten met voorraadinfo op."""
        params = {
            "webshopUuid": self.webshop_id,
            "perPage": limit
        }
        result = await self._request("GET", "/products", params=params)
        return result.get("items", [])
    
    async def get_product(self, product_uuid: str) -> dict:
        """Haal een specifiek product op."""
        return await self._request("GET", f"/products/{product_uuid}")
    
    async def get_low_stock_products(self, threshold: int = 10) -> list:
        """Haal producten met lage voorraad op."""
        products = await self.get_products()
        return [p for p in products if p.get("stockLevel", 0) <= threshold]
    
    # ============ VERZENDINGEN ============
    
    async def get_shipments(
        self,
        created_after: Optional[str] = None,
        limit: int = 50,
        page: int = 1
    ) -> tuple:
        """Haal verzendingen op. Returns (items, pageInfo)."""
        params = {"perPage": limit, "page": page}
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
        
        # Orders vandaag + deze week (alleen counts)
        _, today_info = await self.get_orders(created_after=today_str, limit=1, page=1)
        _, week_info = await self.get_orders(created_after=week_start, limit=1, page=1)
        
        orders_today_count = today_info.get("totalItems", 0)
        orders_week_count = week_info.get("totalItems", 0)
        
        # Status verdeling: tel over ALLE orders van vandaag (niet alleen 50)
        orders_by_status = {}
        page = 1
        max_pages = 20  # safety limit
        while page <= max_pages:
            items, pg_info = await self.get_orders(created_after=today_str, limit=50, page=page)
            if not items:
                break
            for order in items:
                status = order.get("status", "unknown")
                orders_by_status[status] = orders_by_status.get(status, 0) + 1
            last_page = pg_info.get("lastPage", 1)
            if page >= last_page:
                break
            page += 1
        
        # Shipments deze week
        _, ship_info = await self.get_shipments(created_after=week_start, limit=1, page=1)
        shipments_week_count = ship_info.get("totalItems", 0)
        
        return {
            "orders": {
                "today": orders_today_count,
                "week": orders_week_count,
                "by_status": orders_by_status
            },
            "shipments": {
                "week": shipments_week_count
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
