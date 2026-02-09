"""
Dura Fulfilment Dashboard Backend
API server die Goedgepickt data beschikbaar maakt voor het dashboard.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from typing import Optional
import os
import time
import asyncio
from dotenv import load_dotenv

from goedgepickt import get_client, GoedgepicktAPI

# Load environment variables
load_dotenv()

# ============ IN-MEMORY CACHE ============

class SimpleCache:
    """Simpele in-memory cache met TTL."""
    
    def __init__(self):
        self._store = {}
        self._locks = {}
    
    def get(self, key: str):
        """Haal item op uit cache. Returns None als verlopen of niet gevonden."""
        if key not in self._store:
            return None
        value, expires_at = self._store[key]
        if time.time() > expires_at:
            del self._store[key]
            return None
        return value
    
    def set(self, key: str, value, ttl_seconds: int):
        """Sla item op in cache met TTL."""
        self._store[key] = (value, time.time() + ttl_seconds)
    
    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]
    
    async def get_or_fetch(self, key: str, ttl_seconds: int, fetch_fn):
        """Haal uit cache of fetch via functie (met lock om stampede te voorkomen)."""
        cached = self.get(key)
        if cached is not None:
            return cached
        
        lock = self._get_lock(key)
        async with lock:
            # Double-check na lock
            cached = self.get(key)
            if cached is not None:
                return cached
            
            result = await fetch_fn()
            self.set(key, result, ttl_seconds)
            return result

cache = SimpleCache()

# Cache TTLs
CACHE_TTL_DASHBOARD = 60    # 60 seconden
CACHE_TTL_ORDERS = 30       # 30 seconden
CACHE_TTL_INVENTORY = 120   # 2 minuten
CACHE_TTL_SHIPMENTS = 30    # 30 seconden

# Initialize FastAPI
app = FastAPI(
    title="Dura Fulfilment Dashboard API",
    description="Backend API voor het Dura Fulfilment management dashboard",
    version="2.1.0"
)

# CORS configuratie
cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============ HEALTH CHECK ============

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Dura Fulfilment Dashboard API",
        "version": "2.1.0",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/health")
async def health_check():
    try:
        client = get_client()
        gp_connected = await client.test_connection()
    except:
        gp_connected = False
    
    return {
        "status": "healthy" if gp_connected else "degraded",
        "goedgepickt_connected": gp_connected,
        "timestamp": datetime.now().isoformat()
    }


# ============ WEBSHOPS ============

@app.get("/api/webshops")
async def get_webshops():
    """Haal alle webshops op (180+)."""
    try:
        client = get_client()
        webshops = await client.get_webshops()
        return {"success": True, "count": len(webshops), "data": webshops}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ DASHBOARD ============

@app.get("/api/dashboard")
async def get_dashboard():
    """Dashboard KPIs: orders vandaag, deze week, status verdeling. Gecached voor 60s."""
    try:
        async def fetch():
            client = get_client()
            return await client.get_dashboard_stats()
        
        stats = await cache.get_or_fetch("dashboard", CACHE_TTL_DASHBOARD, fetch)
        return {
            "success": True,
            "data": stats,
            "cached": cache.get("dashboard") is not None,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ ORDERS ============

@app.get("/api/orders")
async def get_orders(
    status: Optional[str] = None,
    created_after: Optional[str] = None,
    page: int = 1,
    limit: int = 50
):
    """Haal orders op. Gecached per unieke query voor 30s."""
    try:
        cache_key = f"orders:{status}:{created_after}:{page}:{limit}"
        
        async def fetch():
            client = get_client()
            items, page_info = await client.get_orders(
                status=status,
                created_after=created_after,
                limit=limit,
                page=page
            )
            return {
                "items": items,
                "page_info": page_info
            }
        
        result = await cache.get_or_fetch(cache_key, CACHE_TTL_ORDERS, fetch)
        items = result["items"]
        page_info = result["page_info"]
        
        return {
            "success": True,
            "count": len(items),
            "total": page_info.get("totalItems", 0),
            "page": page,
            "lastPage": page_info.get("lastPage", 1),
            "data": items
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orders/latest")
async def get_latest_orders(limit: int = 50):
    """Haal de nieuwste orders op (afgelopen 7 dagen, nieuwste eerst). Gecached voor 30s."""
    try:
        cache_key = f"orders_latest:{limit}"
        
        async def fetch():
            client = get_client()
            return await client.get_latest_orders(limit=limit)
        
        orders = await cache.get_or_fetch(cache_key, CACHE_TTL_ORDERS, fetch)
        return {
            "success": True,
            "count": len(orders),
            "data": orders
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orders/{order_uuid}")
async def get_order(order_uuid: str):
    """Haal een specifieke order op."""
    try:
        client = get_client()
        order = await client.get_order(order_uuid)
        return {"success": True, "data": order}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ VOORRAAD ============

@app.get("/api/inventory")
async def get_inventory(low_stock_only: bool = False, page: int = 1):
    """Haal voorraad/producten op. Gecached voor 120s."""
    try:
        cache_key = f"inventory:{low_stock_only}:{page}"
        
        async def fetch():
            client = get_client()
            if low_stock_only:
                products = await client.get_low_stock_products()
                return {"items": products, "total": len(products), "lastPage": 1}
            else:
                items, page_info = await client.get_products(limit=50, page=page)
                return {
                    "items": items,
                    "total": page_info.get("totalItems", len(items)),
                    "lastPage": page_info.get("lastPage", 1)
                }
        
        result = await cache.get_or_fetch(cache_key, CACHE_TTL_INVENTORY, fetch)
        
        return {
            "success": True,
            "count": len(result["items"]),
            "total": result["total"],
            "page": page,
            "lastPage": result["lastPage"],
            "data": result["items"]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/inventory/alerts")
async def get_inventory_alerts():
    """Haal producten met lage voorraad op. Gecached voor 120s."""
    try:
        cache_key = "inventory_alerts"
        
        async def fetch():
            client = get_client()
            return await client.get_low_stock_products(threshold=25)
        
        alerts = await cache.get_or_fetch(cache_key, CACHE_TTL_INVENTORY, fetch)
        return {
            "success": True,
            "count": len(alerts),
            "data": alerts
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ VERZENDINGEN ============

@app.get("/api/shipments")
async def get_shipments(
    created_after: Optional[str] = None,
    page: int = 1,
    limit: int = 50
):
    """Haal verzendingen op. Gecached voor 30s."""
    try:
        cache_key = f"shipments:{created_after}:{page}:{limit}"
        
        async def fetch():
            client = get_client()
            items, page_info = await client.get_shipments(
                created_after=created_after,
                limit=limit,
                page=page
            )
            return {"items": items, "page_info": page_info}
        
        result = await cache.get_or_fetch(cache_key, CACHE_TTL_SHIPMENTS, fetch)
        items = result["items"]
        page_info = result["page_info"]
        
        return {
            "success": True,
            "count": len(items),
            "total": page_info.get("totalItems", 0),
            "page": page,
            "lastPage": page_info.get("lastPage", 1),
            "data": items
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/shipments/latest")
async def get_latest_shipments(limit: int = 50):
    """Haal de nieuwste verzendingen op. Gecached voor 30s."""
    try:
        cache_key = f"shipments_latest:{limit}"
        
        async def fetch():
            client = get_client()
            return await client.get_latest_shipments(limit=limit)
        
        shipments = await cache.get_or_fetch(cache_key, CACHE_TTL_SHIPMENTS, fetch)
        return {
            "success": True,
            "count": len(shipments),
            "data": shipments
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ PICKS / MEDEWERKERS ============

@app.get("/api/users")
async def get_users():
    """Haal alle gebruikers/medewerkers op. Gecached voor 120s."""
    try:
        cache_key = "users"
        async def fetch():
            client = get_client()
            return await client.get_users()
        
        users = await cache.get_or_fetch(cache_key, 120, fetch)
        return {"success": True, "count": len(users), "data": users}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/picks")
async def get_picks(date_from: Optional[str] = None, date_to: Optional[str] = None, user_uuid: Optional[str] = None):
    """Haal pick-acties op. Gecached voor 60s."""
    try:
        cache_key = f"picks:{date_from}:{date_to}:{user_uuid}"
        async def fetch():
            client = get_client()
            df = datetime.strptime(date_from, "%Y-%m-%d") if date_from else None
            dt = datetime.strptime(date_to, "%Y-%m-%d") if date_to else None
            return await client.get_picks(date_from=df, date_to=dt, user_uuid=user_uuid)
        
        picks = await cache.get_or_fetch(cache_key, 60, fetch)
        return {"success": True, "count": len(picks), "data": picks}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/picks/stats")
async def get_pick_stats():
    """Medewerker pick-statistieken van vandaag. Gecached voor 60s."""
    try:
        cache_key = "pick_stats_today"
        async def fetch():
            client = get_client()
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            picks = await client.get_picks(date_from=today)
            users = await client.get_users()
            
            # Maak user lookup
            user_map = {u.get("uuid", ""): u for u in users}
            
            # Tel picks per user
            stats = {}
            for pick in picks:
                uid = pick.get("userUuid") or pick.get("user_uuid") or "unknown"
                if uid not in stats:
                    user = user_map.get(uid, {})
                    stats[uid] = {
                        "uuid": uid,
                        "name": user.get("name") or user.get("firstName", "?") + " " + user.get("lastName", ""),
                        "email": user.get("email", ""),
                        "picks": 0,
                        "items": 0
                    }
                stats[uid]["picks"] += 1
                stats[uid]["items"] += pick.get("quantity", 1)
            
            # Sorteer op meeste picks
            ranked = sorted(stats.values(), key=lambda x: x["picks"], reverse=True)
            return {"pickers": ranked, "total_picks": len(picks), "total_users": len(users)}
        
        result = await cache.get_or_fetch(cache_key, 60, fetch)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ RUN SERVER ============

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
