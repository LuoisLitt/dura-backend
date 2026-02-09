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
        """Haal alle webshops op die aan dit account gekoppeld zijn."""
        result = await self._request("GET", "/webshops")
        return result.get("items", result.get("data", []))
    
    # ============ ORDERS ============
    
    async def get_orders(
        self,
        status: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        limit: int = 100,
        page: int = 1,
        webshop_uuid: Optional[str] = None
    ) -> list:
        """
        Haal orders op.
        
        Status opties: open, processing, picked, packed, shipped, delivered
        """
        params = {
            "perPage": limit,
            "page": page,
            "sort[0][field]": "createdAt",
            "sort[0][direction]": "desc"
        }
        # Gebruik specifieke webshop of de default
        ws = webshop_uuid or self.webshop_id
        if ws:
            params["webshopUuid"] = ws
        
        if status:
            params["status"] = status
        if date_from:
            params["createdAtFrom"] = date_from.strftime("%Y-%m-%d")
        if date_to:
            params["createdAtTo"] = date_to.strftime("%Y-%m-%d")
        
        result = await self._request("GET", "/orders", params=params)
        items = result.get("items", [])
        # Sla pagination info op voor later gebruik
        self._last_pagination = {
            "totalItems": result.get("totalItems", result.get("total", 0)),
            "lastPage": result.get("lastPage", result.get("last_page", 0)),
            "currentPage": result.get("currentPage", result.get("current_page", page)),
            "perPage": limit
        }
        # Fallback sort als API sort niet werkt
        items.sort(key=lambda x: x.get("createDate", ""), reverse=True)
        return items
    
    async def get_latest_orders(self, limit: int = 20) -> list:
        """Haal de nieuwste orders op door eerst de laatste pagina te vinden."""
        # Stap 1: Haal eerste pagina op om totaal te weten
        await self.get_orders(limit=50, page=1)
        pagination = getattr(self, '_last_pagination', {})
        last_page = pagination.get("lastPage", 0)
        
        if last_page <= 1:
            # Maar 1 pagina, gewoon die returnen
            return await self.get_orders(limit=limit, page=1)
        
        # Stap 2: Haal de laatste pagina(s) op
        all_orders = []
        # Pak de laatste 2 pagina's voor genoeg data
        for p in range(max(1, last_page - 1), last_page + 1):
            try:
                orders = await self.get_orders(limit=50, page=p)
                all_orders.extend(orders)
            except:
                pass
        
        # Sort nieuwste eerst
        all_orders.sort(key=lambda x: x.get("createDate", ""), reverse=True)
        return all_orders[:limit]
    
    async def get_order(self, order_uuid: str) -> dict:
        """Haal een specifieke order op."""
        return await self._request("GET", f"/orders/{order_uuid}")
    
    async def get_orders_today(self) -> list:
        """Haal alle orders van vandaag op."""
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.get_orders(date_from=today)
    
    async def get_orders_this_week(self) -> list:
        """Haal alle orders van deze week op."""
        today = datetime.now()
        start_of_week = today - timedelta(days=today.weekday())
        start_of_week = start_of_week.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.get_orders(date_from=start_of_week)
    
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
        status: Optional[str] = None,
        date_from: Optional[datetime] = None,
        limit: int = 100
    ) -> list:
        """
        Haal verzendingen op.
        
        Status opties: pending, shipped, delivered, returned
        """
        params = {
            "webshopUuid": self.webshop_id,
            "perPage": limit
        }
        
        if status:
            params["status"] = status
        if date_from:
            params["createdAtFrom"] = date_from.strftime("%Y-%m-%d")
        
        result = await self._request("GET", "/shipments", params=params)
        items = result.get("items", [])
        # Sort nieuwste eerst
        items.sort(key=lambda x: x.get("createDate", ""), reverse=True)
        return items
    
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
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today - timedelta(days=today.weekday())
        
        # Haal data parallel op
        orders_today = await self.get_orders(date_from=today)
        orders_week = await self.get_orders(date_from=week_start)
        products = await self.get_products()
        shipments_week = await self.get_shipments(date_from=week_start)
        
        # Bereken statistieken
        orders_by_status = {}
        for order in orders_today:
            status = order.get("status", "unknown")
            orders_by_status[status] = orders_by_status.get(status, 0) + 1
        
        low_stock = [p for p in products if p.get("stockLevel", 0) <= p.get("minStockLevel", 10)]
        critical_stock = [p for p in products if p.get("stockLevel", 0) <= 10]
        
        # Carrier verdeling
        carrier_stats = {}
        delivered_on_time = 0
        total_delivered = 0
        
        for shipment in shipments_week:
            carrier = shipment.get("carrier", "unknown")
            carrier_stats[carrier] = carrier_stats.get(carrier, 0) + 1
            
            if shipment.get("status") == "delivered":
                total_delivered += 1
                # Check on-time (vereenvoudigd)
                if shipment.get("deliveredOnTime", True):
                    delivered_on_time += 1
        
        on_time_rate = (delivered_on_time / total_delivered * 100) if total_delivered > 0 else 100
        
        return {
            "orders": {
                "today": len(orders_today),
                "week": len(orders_week),
                "by_status": orders_by_status
            },
            "inventory": {
                "total_products": len(products),
                "low_stock": len(low_stock),
                "critical_stock": len(critical_stock),
                "low_stock_items": low_stock[:10]  # Top 10
            },
            "shipments": {
                "week": len(shipments_week),
                "on_time_rate": round(on_time_rate, 1),
                "by_carrier": carrier_stats
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
