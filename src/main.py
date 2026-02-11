"""
Dura Fulfilment Dashboard Backend
API server die Goedgepickt data beschikbaar maakt voor het dashboard.
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import hmac
import hashlib
import json
import base64
import os

import bcrypt

CET = ZoneInfo("Europe/Amsterdam")
import time
import asyncio
from dotenv import load_dotenv

from goedgepickt import get_client, GoedgepicktAPI
from ai_updates import get_cached_insights, generate_insights, setup_scheduler

# Load environment variables
load_dotenv()

# ============ AUTHENTICATION ============

SESSION_SECRET = os.getenv("SESSION_SECRET", "")

USERS = {
    "michel@durafulfilment.nl": {
        "name": "Michel",
        "password_hash": "$2b$12$43/gz0tOBkjLKOxFBlMwq.c99buavYr280mUY4HrDtUAis/Wb.J.K",
    },
    "jarne@durafulfilment.nl": {
        "name": "Jarne",
        "password_hash": "$2b$12$aJAd.m6yYyiPO50J31dUke0h1g6bCTHpJNOyWgjXH5qCdLPPRXUEW",
    },
    "demo@durafulfilment.nl": {
        "name": "Demo",
        "password_hash": "$2b$12$qBDjptPGTOxctYMNF9Nz5urDMsSG0ySMTMDsiGcLFbCE6S8nsRi82",
    },
}


class LoginRequest(BaseModel):
    email: str
    password: str


def create_session_token(email: str, name: str) -> str:
    """Maak een signed session token (HMAC-SHA256, 24 uur geldig)."""
    payload = {"email": email, "name": name, "exp": int(time.time()) + 86400}
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(SESSION_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def verify_session_token(token: str) -> Optional[dict]:
    """Verifieer en decode een session token. Returns payload dict of None."""
    try:
        payload_b64, sig = token.rsplit(".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


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
CACHE_TTL_DASHBOARD = 180   # 3 minuten (warm_cache ververst elke 45s, TTL=180s = altijd buffer)
CACHE_TTL_ORDERS = 90       # 90 seconden (was 30, verhoogd voor snelheid)
CACHE_TTL_INVENTORY = 300   # 5 minuten (voorraad verandert niet elke 2 min)
CACHE_TTL_SHIPMENTS = 90    # 90 seconden (Goedgepickt shipments API is traag)

# Initialize FastAPI
app = FastAPI(
    title="Dura Fulfilment Dashboard API",
    description="Backend API voor het Dura Fulfilment management dashboard",
    version="3.0.0"
)

# ============ CACHE PRE-WARMING ============

_warm_task = None

async def warm_cache():
    """Achtergrond taak die cache elke 45s ververst zodat users altijd cached data krijgen."""
    while True:
        try:
            client = get_client()
            today_str = datetime.now(tz=CET).strftime("%Y-%m-%d")
            
            # Dashboard stats
            stats = await client.get_dashboard_stats()
            cache.set("dashboard", stats, CACHE_TTL_DASHBOARD + 30)
            
            # Latest orders (voor /api/orders/latest en frontpage)
            latest = await client.get_latest_orders(limit=50)
            cache.set("orders_latest:50", latest, CACHE_TTL_ORDERS + 30)
            
            # Orders vandaag page info (voor paginering)
            _, today_info = await client.get_orders(created_after=today_str, limit=50, page=1)
            last_page = today_info.get("lastPage", 1)
            
            # Pre-warm eerste + laatste pagina van vandaag
            if last_page > 1:
                items_last, info_last = await client.get_orders(created_after=today_str, limit=50, page=last_page)
                cache.set(f"orders:{None}:{today_str}:{last_page}:50", {"items": items_last, "page_info": info_last}, CACHE_TTL_ORDERS + 30)
            
            # Latest shipments
            ship_latest = await client.get_latest_shipments(limit=50)
            cache.set("shipments_latest:50", ship_latest, CACHE_TTL_SHIPMENTS + 30)
            
            # Inventory alerts
            alerts = await client.get_low_stock_products(threshold=25)
            cache.set("inventory_alerts", alerts, CACHE_TTL_INVENTORY + 30)
            
        except Exception as e:
            print(f"Cache warm error: {e}")
        
        await asyncio.sleep(45)

@app.on_event("startup")
async def startup_event():
    global _warm_task
    _warm_task = asyncio.create_task(warm_cache())
    # Start AI insights scheduler
    try:
        setup_scheduler(app)
    except Exception as e:
        print(f"AI Scheduler setup failed (non-fatal): {e}")

# CORS configuratie
_default_origins = "https://klain.nl,https://www.klain.nl"
cors_origins = os.getenv("CORS_ORIGINS", _default_origins).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Accept", "X-API-Key", "Authorization"],
)

# ============ API KEY AUTHENTICATION ============

DURA_API_KEY = os.getenv("DURA_API_KEY", "")

@app.middleware("http")
async def verify_api_key(request: Request, call_next):
    """Middleware die X-API-Key header checkt op alle /api/* endpoints."""
    path = request.url.path

    # Skip auth voor health checks, CORS preflight en auth endpoints
    if not path.startswith("/api/") or request.method == "OPTIONS" or path.startswith("/api/auth/"):
        return await call_next(request)

    # Als geen API key geconfigureerd is, weiger alle /api/* requests
    if not DURA_API_KEY:
        print("[WARN] DURA_API_KEY not set - rejecting API request")
        return JSONResponse(status_code=500, content={"success": False, "error": "Server configuration error"})

    api_key = request.headers.get("X-API-Key", "")
    if not api_key or not hmac.compare_digest(api_key, DURA_API_KEY):
        return JSONResponse(status_code=401, content={"success": False, "error": "Unauthorized"})

    return await call_next(request)


# ============ SESSION AUTH ENDPOINTS ============

@app.post("/api/auth/login")
async def auth_login(body: LoginRequest):
    """Login met email + wachtwoord. Geeft signed session token terug."""
    if not SESSION_SECRET:
        print("[WARN] SESSION_SECRET not set - rejecting login")
        return JSONResponse(status_code=500, content={"success": False, "error": "Server configuration error"})

    email = body.email.strip().lower()
    user = USERS.get(email)
    if not user:
        return JSONResponse(status_code=401, content={"success": False, "error": "Ongeldige inloggegevens"})

    if not bcrypt.checkpw(body.password.encode(), user["password_hash"].encode()):
        return JSONResponse(status_code=401, content={"success": False, "error": "Ongeldige inloggegevens"})

    token = create_session_token(email, user["name"])
    return {"success": True, "token": token, "user": {"email": email, "name": user["name"]}}


@app.get("/api/auth/verify")
async def auth_verify(request: Request):
    """Verifieer of een session token geldig is."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"success": False, "error": "Geen token"})

    token = auth_header[7:]
    payload = verify_session_token(token)
    if not payload:
        return JSONResponse(status_code=401, content={"success": False, "error": "Token ongeldig of verlopen"})

    return {"success": True, "user": {"email": payload["email"], "name": payload["name"]}}


# ============ COMBINED PAGE DATA ============

@app.get("/api/page-data/{page}")
async def get_page_data(page: str):
    """Gecombineerd endpoint: alle data voor een pagina in één call. Veel sneller dan losse calls."""
    try:
        client = get_client()
        today_str = datetime.now(tz=CET).strftime("%Y-%m-%d")
        result = {}
        
        if page == "dashboard":
            # Dashboard heeft nodig: stats + latest orders + inventory alerts
            async def fetch_stats():
                return await cache.get_or_fetch("dashboard", CACHE_TTL_DASHBOARD, 
                    lambda: asyncio.wait_for(client.get_dashboard_stats(), timeout=20.0))
            async def fetch_latest():
                return await cache.get_or_fetch("orders_latest:5", CACHE_TTL_ORDERS, 
                    lambda: asyncio.wait_for(client.get_latest_orders(limit=5), timeout=10.0))
            async def fetch_alerts():
                return await cache.get_or_fetch("inventory_alerts", CACHE_TTL_INVENTORY, 
                    lambda: asyncio.wait_for(client.get_low_stock_products(threshold=25), timeout=10.0))
            
            try:
                stats, latest, alerts = await asyncio.wait_for(
                    asyncio.gather(fetch_stats(), fetch_latest(), fetch_alerts(), return_exceptions=True),
                    timeout=25.0
                )
                # Handle individual failures
                if isinstance(stats, Exception): stats = cache.get("dashboard") or {}
                if isinstance(latest, Exception): latest = cache.get("orders_latest:5") or []
                if isinstance(alerts, Exception): alerts = cache.get("inventory_alerts") or []
            except asyncio.TimeoutError:
                stats = cache.get("dashboard") or {}
                latest = cache.get("orders_latest:5") or []
                alerts = cache.get("inventory_alerts") or []
            
            result = {
                "dashboard": stats,
                "recentOrders": (latest or [])[:5],
                "inventoryAlerts": (alerts or [])[:5]
            }
        
        elif page == "orders":
            # Orders: stats + laatste pagina orders
            async def fetch_stats():
                return await cache.get_or_fetch("dashboard", CACHE_TTL_DASHBOARD, lambda: client.get_dashboard_stats())
            async def fetch_page_info():
                return await cache.get_or_fetch(f"orders_pageinfo:{today_str}", CACHE_TTL_ORDERS, 
                    lambda: client.get_orders(created_after=today_str, limit=50, page=1))
            
            stats, (first_items, page_info) = await asyncio.gather(fetch_stats(), fetch_page_info())
            last_page = page_info.get("lastPage", 1)
            total = page_info.get("totalItems", 0)
            
            # Haal de laatste pagina op (nieuwste orders)
            if last_page > 1:
                cache_key = f"orders:{None}:{today_str}:{last_page}:50"
                async def fetch_last():
                    items, info = await client.get_orders(created_after=today_str, limit=50, page=last_page)
                    return {"items": items, "page_info": info}
                last_data = await cache.get_or_fetch(cache_key, CACHE_TTL_ORDERS, fetch_last)
                orders = last_data["items"]
            else:
                orders = first_items
            
            orders.sort(key=lambda x: x.get("createDate", ""), reverse=True)
            result = {
                "dashboard": stats,
                "orders": orders,
                "total": total,
                "lastPage": last_page
            }
        
        elif page == "shipments":
            async def fetch_stats():
                return await cache.get_or_fetch("dashboard", CACHE_TTL_DASHBOARD, lambda: client.get_dashboard_stats())
            async def fetch_page_info():
                return await cache.get_or_fetch(f"shipments_pageinfo:{today_str}", CACHE_TTL_SHIPMENTS,
                    lambda: client.get_shipments(created_after=today_str, limit=50, page=1))
            
            stats, (first_items, page_info) = await asyncio.gather(fetch_stats(), fetch_page_info())
            last_page = page_info.get("lastPage", 1)
            total = page_info.get("totalItems", 0)
            
            if last_page > 1:
                cache_key = f"shipments_last:{today_str}:{last_page}"
                async def fetch_last():
                    items, info = await client.get_shipments(created_after=today_str, limit=50, page=last_page)
                    return {"items": items, "page_info": info}
                last_data = await cache.get_or_fetch(cache_key, CACHE_TTL_SHIPMENTS, fetch_last)
                shipments = last_data["items"]
            else:
                shipments = first_items
            
            shipments.sort(key=lambda x: x.get("createDate", ""), reverse=True)
            result = {
                "dashboard": stats,
                "shipments": shipments,
                "total": total,
                "lastPage": last_page
            }
        
        elif page == "warehouse":
            async def fetch_stats():
                return await cache.get_or_fetch("dashboard", CACHE_TTL_DASHBOARD, lambda: client.get_dashboard_stats())
            async def fetch_latest():
                return await cache.get_or_fetch("orders_latest:50", CACHE_TTL_ORDERS, lambda: client.get_latest_orders(limit=50))
            async def fetch_alerts():
                return await cache.get_or_fetch("inventory_alerts", CACHE_TTL_INVENTORY, lambda: client.get_low_stock_products(threshold=25))
            
            stats, latest, alerts = await asyncio.gather(fetch_stats(), fetch_latest(), fetch_alerts())
            result = {
                "dashboard": stats,
                "latestOrders": latest,
                "inventoryAlerts": alerts[:4]
            }
        
        else:
            raise HTTPException(status_code=400, detail=f"Unknown page: {page}")
        
        result["cached"] = True
        result["timestamp"] = datetime.now(tz=CET).isoformat()
        return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] get_page_data({page}): {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============ AI INSIGHTS ============

@app.get("/api/ai-insights")
async def get_ai_insights():
    """Haal de laatste AI-gegenereerde insights op."""
    return {"success": True, "data": get_cached_insights()}


@app.post("/api/ai-insights/refresh")
async def refresh_ai_insights():
    """Forceer een nieuwe AI insights generatie."""
    await generate_insights()
    return {"success": True, "data": get_cached_insights()}


# ============ HEALTH CHECK ============

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Dura Fulfilment Dashboard API",
        "version": "3.5.1",
        "timestamp": datetime.now(tz=CET).isoformat()
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
        "timestamp": datetime.now(tz=CET).isoformat()
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
        print(f"[ERROR] get_webshops: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============ DASHBOARD ============

@app.get("/api/dashboard")
async def get_dashboard():
    """Dashboard KPIs: orders vandaag, deze week, status verdeling. Gecached voor 120s."""
    try:
        async def fetch():
            client = get_client()
            return await asyncio.wait_for(client.get_dashboard_stats(), timeout=20.0)
        
        stats = await cache.get_or_fetch("dashboard", CACHE_TTL_DASHBOARD, fetch)
        return {
            "success": True,
            "data": stats,
            "cached": cache.get("dashboard") is not None,
            "timestamp": datetime.now(tz=CET).isoformat()
        }
    except asyncio.TimeoutError:
        # Return stale cache if available
        stale = cache.get("dashboard")
        if stale:
            return {"success": True, "data": stale, "cached": True, "stale": True, "timestamp": datetime.now(tz=CET).isoformat()}
        raise HTTPException(status_code=504, detail="Dashboard timeout")
    except Exception as e:
        print(f"[ERROR] get_dashboard: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
        print(f"[ERROR] get_orders: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
        print(f"[ERROR] get_latest_orders: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/orders/{order_uuid}")
async def get_order(order_uuid: str):
    """Haal een specifieke order op."""
    try:
        client = get_client()
        order = await client.get_order(order_uuid)
        return {"success": True, "data": order}
    except Exception as e:
        print(f"[ERROR] get_order: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
        print(f"[ERROR] get_inventory: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
        print(f"[ERROR] get_inventory_alerts: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
        print(f"[ERROR] get_shipments: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
        print(f"[ERROR] get_latest_shipments: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
        print(f"[ERROR] get_users: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


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
        print(f"[ERROR] get_picks: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/picks/stats")
async def get_pick_stats():
    """Medewerker pick-statistieken van vandaag. Gecached voor 60s."""
    try:
        cache_key = "pick_stats_today"
        async def fetch():
            client = get_client()
            today = datetime.now(tz=CET).replace(hour=0, minute=0, second=0, microsecond=0)
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
        print(f"[ERROR] get_pick_stats: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============ RUN SERVER ============

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
