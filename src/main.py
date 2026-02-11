"""
Dura Fulfilment Dashboard Backend
API server die Goedgepickt data beschikbaar maakt voor het dashboard.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import hmac
import hashlib
import json
import base64
import csv
import io
import os

import bcrypt

CET = ZoneInfo("Europe/Amsterdam")
import time
import asyncio
from dotenv import load_dotenv

from goedgepickt import get_client, GoedgepicktAPI
from ai_updates import get_cached_insights, generate_insights, setup_scheduler, set_inventory_cache_getter

# Load environment variables
load_dotenv()

# ============ AUTHENTICATION ============

SESSION_SECRET = os.getenv("SESSION_SECRET", "")

def _load_users() -> dict:
    """Laad users uit DURA_USERS env var (JSON) of gebruik fallback demo account."""
    users_json = os.getenv("DURA_USERS", "")
    if users_json:
        try:
            users = json.loads(users_json)
            print(f"[AUTH] Loaded {len(users)} users from DURA_USERS env var")
            return users
        except json.JSONDecodeError as e:
            print(f"[WARN] DURA_USERS invalid JSON: {e} — falling back to demo account")
    else:
        print("[WARN] DURA_USERS not set — only demo account available")
    # Fallback: alleen demo account (geen echte credentials in broncode)
    return {
        "demo@durafulfilment.nl": {
            "name": "Demo",
            "password_hash": "$2b$12$qBDjptPGTOxctYMNF9Nz5urDMsSG0ySMTMDsiGcLFbCE6S8nsRi82",
        },
    }


USERS = _load_users()


class LoginRequest(BaseModel):
    email: str
    password: str


class RateLimiter:
    """In-memory rate limiter per key (bijv. IP-adres)."""

    def __init__(self, max_attempts: int, window_seconds: int):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._attempts: dict[str, list[float]] = {}

    def is_limited(self, key: str) -> bool:
        """Check of key over de limiet is. Ruimt ook verlopen keys op."""
        now = time.time()
        cutoff = now - self.window
        # Cleanup: verwijder keys ouder dan 2x window
        cleanup_cutoff = now - (self.window * 2)
        stale_keys = [k for k, v in self._attempts.items() if v and max(v) < cleanup_cutoff]
        for k in stale_keys:
            del self._attempts[k]
        # Check huidige key
        attempts = [t for t in self._attempts.get(key, []) if t > cutoff]
        self._attempts[key] = attempts
        return len(attempts) >= self.max_attempts

    def record(self, key: str):
        """Registreer een poging."""
        if key not in self._attempts:
            self._attempts[key] = []
        self._attempts[key].append(time.time())


login_limiter = RateLimiter(max_attempts=5, window_seconds=900)  # 5 per 15 min


def create_session_token(email: str, name: str) -> str:
    """Maak een signed session token (HMAC-SHA256, 24 uur geldig)."""
    if not SESSION_SECRET:
        raise ValueError("SESSION_SECRET not configured")
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
CACHE_TTL_SLA = 300         # 5 minuten (SLA metrics veranderen niet snel)
CACHE_TTL_ACTIVE_INV = 2100 # 35 minuten (indexer draait elke 30 min)

# ============ CACHE PRE-WARMING ============

async def warm_cache():
    """Achtergrond taak die cache ververst. Interval 180s om rate limits te voorkomen."""
    while True:
        try:
            client = get_client()

            # Dashboard stats (haalt orders, shipments, revenue, etc. op in 1 call)
            print("[WarmCache] Fetching dashboard stats...", flush=True)
            stats = await client.get_dashboard_stats()
            cache.set("dashboard", stats, CACHE_TTL_DASHBOARD + 60)
            print(f"[WarmCache] Dashboard cached: {stats.get('orders', {}).get('today', '?')} orders, {stats.get('shipments', {}).get('today', '?')} shipments, {stats.get('orders', {}).get('processed', '?')} verwerkt", flush=True)

            # Wacht 5s om rate limit budget te laten herstellen
            await asyncio.sleep(5)

            # Latest orders (apart, licht: 1 API call)
            latest = await client.get_latest_orders(limit=50)
            cache.set("orders_latest:50", latest, CACHE_TTL_ORDERS + 60)

            # Latest shipments (1 API call)
            await asyncio.sleep(2)
            ship_latest = await client.get_latest_shipments(limit=50)
            cache.set("shipments_latest:50", ship_latest, CACHE_TTL_SHIPMENTS + 60)

        except Exception as e:
            print(f"[WarmCache] Error: {e}", flush=True)

        await asyncio.sleep(180)  # 3 minuten (was 45s — veroorzaakte rate limiting)


# ============ INVENTORY INDEXER ============

_inventory_status = {"state": "init", "products": 0, "last_run": None, "error": None, "progress": 0}

async def index_active_inventory():
    """Achtergrond taak die elke 30 min alle producten met stock > 0 indexeert."""
    import sys
    global _inventory_status
    print("[INVENTORY] Task started, waiting 30s before first run...", flush=True)
    sys.stdout.flush()
    _inventory_status["state"] = "waiting"
    await asyncio.sleep(30)  # 30s wachten zodat warm_cache eerste fetch kan doen

    while True:
        try:
            _inventory_status.update({"state": "scanning", "progress": 0})
            print("[INVENTORY] Starting scan...", flush=True)
            sys.stdout.flush()
            client = get_client()

            # Callback voor progress tracking
            def on_progress(pages_done, total_pages, active_count):
                pct = round(pages_done / max(total_pages, 1) * 100)
                _inventory_status.update({"progress": pct, "products": active_count})

            active = await client.get_in_stock_products(on_progress=on_progress)
            cache.set("inventory_active", active, CACHE_TTL_ACTIVE_INV)
            _inventory_status.update({
                "state": "ready",
                "products": len(active),
                "last_run": datetime.now(tz=CET).isoformat(),
                "error": None,
                "progress": 100,
            })
            print(f"[INVENTORY] Cache updated: {len(active)} active products", flush=True)
            sys.stdout.flush()
        except Exception as e:
            _inventory_status.update({
                "state": "error",
                "error": str(e),
                "last_run": datetime.now(tz=CET).isoformat(),
            })
            import traceback
            print(f"[INVENTORY] Index error: {e}", flush=True)
            traceback.print_exc()
            sys.stdout.flush()

        await asyncio.sleep(1800)  # 30 minuten


# ============ LIFESPAN (startup + shutdown) ============

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager: startup en shutdown logica."""
    # --- STARTUP ---
    import sys
    print("[Startup] === DURA BACKEND STARTING ===", flush=True)
    sys.stdout.flush()

    warm_task = asyncio.create_task(warm_cache())
    print("[Startup] Cache pre-warming task gestart", flush=True)

    inventory_task = asyncio.create_task(index_active_inventory())
    print("[Startup] Inventory indexer task aangemaakt", flush=True)

    # Registreer inventory cache getter voor AI insights
    set_inventory_cache_getter(lambda: cache.get("inventory_active"))
    print("[Startup] Inventory cache getter geregistreerd bij AI insights", flush=True)
    sys.stdout.flush()

    try:
        setup_scheduler(app)
    except Exception as e:
        print(f"AI Scheduler setup failed (non-fatal): {e}")

    yield

    # --- SHUTDOWN ---
    print("[Shutdown] Graceful shutdown gestart...")

    # Cancel background tasks
    warm_task.cancel()
    inventory_task.cancel()
    try:
        await warm_task
    except asyncio.CancelledError:
        pass
    try:
        await inventory_task
    except asyncio.CancelledError:
        pass
    print("[Shutdown] Background tasks gestopt")

    # Close httpx client (GoedgepicktAPI)
    try:
        client = get_client()
        if client._http_client and not client._http_client.is_closed:
            await client._http_client.aclose()
            print("[Shutdown] Goedgepickt httpx client gesloten")
    except Exception as e:
        print(f"[Shutdown] httpx client close error (non-fatal): {e}")


# Initialize FastAPI
app = FastAPI(
    title="Dura Fulfilment Dashboard API",
    description="Backend API voor het Dura Fulfilment management dashboard",
    version="3.0.0",
    lifespan=lifespan,
)

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

# ============ AUTHENTICATION MIDDLEWARE ============

DURA_API_KEY = os.getenv("DURA_API_KEY", "")

# P1.3: Alleen deze paths accepteren POST requests
POST_ALLOWED_PATHS = {"/api/auth/login", "/api/ai-insights/refresh"}

@app.middleware("http")
async def verify_auth(request: Request, call_next):
    """Middleware die session token OF API key checkt op alle /api/* endpoints.
    P1.1: Accepteert Bearer session tokens zodat frontend geen API key meer nodig heeft.
    P1.3: Blokkeert POST op endpoints die alleen GET ondersteunen.
    """
    path = request.url.path

    # Skip auth voor health checks, CORS preflight, auth endpoints en inventory status
    if not path.startswith("/api/") or request.method == "OPTIONS" or path.startswith("/api/auth/") or path == "/api/inventory/status":
        return await call_next(request)

    # P1.3: POST method whitelist — alleen specifieke endpoints accepteren POST
    if request.method == "POST" and path not in POST_ALLOWED_PATHS:
        client_ip = request.client.host if request.client else "unknown"
        print(f"[AUTH] POST rejected: {path} from {client_ip}")
        return JSONResponse(status_code=405, content={"success": False, "error": "Method not allowed"})

    # Auth optie 1: Geldig session token (Bearer header)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        payload = verify_session_token(token)
        if payload:
            print(f"[AUTH] Session token auth: {payload.get('email', '?')}")
            return await call_next(request)

    # Auth optie 2: Geldige API key (backward compatibility + server-to-server)
    if DURA_API_KEY:
        api_key = request.headers.get("X-API-Key", "")
        if api_key and hmac.compare_digest(api_key, DURA_API_KEY):
            print("[AUTH] API key auth")
            return await call_next(request)

    return JSONResponse(status_code=401, content={"success": False, "error": "Unauthorized"})


# ============ SESSION AUTH ENDPOINTS ============

@app.post("/api/auth/login")
async def auth_login(body: LoginRequest, request: Request):
    """Login met email + wachtwoord. Geeft signed session token terug."""
    if not SESSION_SECRET:
        print("[WARN] SESSION_SECRET not set - rejecting login")
        return JSONResponse(status_code=500, content={"success": False, "error": "Server configuration error"})

    # Rate limiting: max 5 pogingen per 15 minuten per IP
    client_ip = request.client.host if request.client else "unknown"
    if login_limiter.is_limited(client_ip):
        print(f"[AUTH] Rate limited: {client_ip}")
        return JSONResponse(status_code=429, content={"success": False, "error": "Te veel inlogpogingen. Probeer het over 15 minuten opnieuw."})

    email = body.email.strip().lower()
    user = USERS.get(email)
    if not user:
        login_limiter.record(client_ip)
        return JSONResponse(status_code=401, content={"success": False, "error": "Ongeldige inloggegevens"})

    if not bcrypt.checkpw(body.password.encode(), user["password_hash"].encode()):
        login_limiter.record(client_ip)
        return JSONResponse(status_code=401, content={"success": False, "error": "Ongeldige inloggegevens"})

    login_limiter.record(client_ip)
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
async def refresh_ai_insights(request: Request):
    """Forceer een nieuwe AI insights generatie. Vereist geldige sessie."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"success": False, "error": "Session vereist"})
    payload = verify_session_token(auth_header[7:])
    if not payload:
        return JSONResponse(status_code=401, content={"success": False, "error": "Ongeldige sessie"})

    print(f"[AI] Manual refresh triggered by {payload.get('email', '?')}")
    await generate_insights()
    return {"success": True, "data": get_cached_insights()}


# ============ SLA METRICS ============

def _calculate_sla_metrics(orders: list, sla_hours: int = 24) -> dict:
    """Bereken SLA KPI's uit een lijst orders."""
    now = datetime.now(tz=CET)
    total = len(orders)
    if total == 0:
        return {
            "on_time_shipping": {"percentage": 0, "on_time": 0, "late": 0, "sla_threshold_hours": sla_hours},
            "order_accuracy": {"percentage": 0, "accurate": 0, "with_issues": 0},
            "order_cycle_time": {"avg_minutes": 0, "median_minutes": 0, "p95_minutes": 0},
            "perfect_order_rate": {"percentage": 0, "perfect": 0, "imperfect": 0},
        }

    on_time = 0
    late = 0
    accurate = 0
    with_issues = 0
    perfect = 0
    cycle_times: list[float] = []

    for order in orders:
        create_date = order.get("createDate")
        finish_date = order.get("finishDate")
        status = order.get("status", "unknown")
        attention = order.get("attentionNeeded") in (1, "1", True)

        # Order Accuracy
        if attention:
            with_issues += 1
        else:
            accurate += 1

        # Cycle time + On-Time Shipping
        is_on_time = False
        if create_date and finish_date:
            try:
                created = datetime.fromisoformat(create_date.replace("Z", "+00:00"))
                finished = datetime.fromisoformat(finish_date.replace("Z", "+00:00"))
                diff_minutes = (finished - created).total_seconds() / 60
                if 0 < diff_minutes < 10080:  # Max 7 dagen
                    cycle_times.append(diff_minutes)
                    if diff_minutes <= sla_hours * 60:
                        on_time += 1
                        is_on_time = True
                    else:
                        late += 1
            except (ValueError, TypeError):
                pass
        elif create_date and status in ("completed", "shipped", "delivered"):
            # Afgerond maar geen finishDate — tel als on-time (data gap)
            on_time += 1
            is_on_time = True

        # Perfect Order: on-time + accuraat + status afgerond
        if is_on_time and not attention and status in ("completed", "shipped", "delivered"):
            perfect += 1

    # Cycle time statistieken
    cycle_times.sort()
    avg_ct = round(sum(cycle_times) / len(cycle_times), 1) if cycle_times else 0
    median_ct = round(cycle_times[len(cycle_times) // 2], 1) if cycle_times else 0
    p95_idx = int(len(cycle_times) * 0.95)
    p95_ct = round(cycle_times[min(p95_idx, len(cycle_times) - 1)], 1) if cycle_times else 0

    shipped_total = on_time + late
    return {
        "on_time_shipping": {
            "percentage": round((on_time / shipped_total * 100), 1) if shipped_total > 0 else 0,
            "on_time": on_time,
            "late": late,
            "sla_threshold_hours": sla_hours,
        },
        "order_accuracy": {
            "percentage": round((accurate / total * 100), 1) if total > 0 else 0,
            "accurate": accurate,
            "with_issues": with_issues,
        },
        "order_cycle_time": {
            "avg_minutes": avg_ct,
            "median_minutes": median_ct,
            "p95_minutes": p95_ct,
        },
        "perfect_order_rate": {
            "percentage": round((perfect / total * 100), 1) if total > 0 else 0,
            "perfect": perfect,
            "imperfect": total - perfect,
        },
    }


def _calc_trend(current: float, previous: float) -> str:
    """Bereken trend: up, down, of stable (threshold 1%)."""
    if previous == 0:
        return "stable"
    diff = current - previous
    if diff > 1.0:
        return "up"
    elif diff < -1.0:
        return "down"
    return "stable"


@app.get("/api/metrics/sla")
async def get_sla_metrics(period: str = "week"):
    """SLA KPI's: On-Time Shipping, Order Accuracy, Cycle Time, Perfect Order Rate."""
    try:
        if period not in ("today", "week", "month"):
            raise HTTPException(status_code=400, detail="Period must be today, week, or month")

        cache_key = f"sla_metrics:{period}"

        async def fetch():
            client = get_client()
            now = datetime.now(tz=CET)

            # Bepaal periodes
            if period == "today":
                start = now.strftime("%Y-%m-%d")
                prev_start = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                prev_end = start
            elif period == "week":
                start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
                prev_start = (now - timedelta(days=now.weekday() + 7)).strftime("%Y-%m-%d")
                prev_end = start
            else:  # month
                start = now.replace(day=1).strftime("%Y-%m-%d")
                prev_month = (now.replace(day=1) - timedelta(days=1)).replace(day=1)
                prev_start = prev_month.strftime("%Y-%m-%d")
                prev_end = start

            # Haal orders op voor huidige en vorige periode (parallel)
            async def fetch_period_orders(after: str, max_pages: int = 50):
                items_first, pg_info = await client.get_orders(created_after=after, limit=50, page=1)
                if not items_first:
                    return []
                all_orders = list(items_first)
                last_page = pg_info.get("lastPage", 1)
                if last_page > 1:
                    tasks = [client.get_orders(created_after=after, limit=50, page=p)
                             for p in range(2, min(last_page + 1, max_pages + 1))]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in results:
                        if isinstance(r, tuple) and len(r) == 2:
                            all_orders.extend(r[0])
                return all_orders

            async def fetch_today_count():
                _, info = await client.get_orders(created_after=now.strftime("%Y-%m-%d"), limit=1, page=1)
                return info.get("totalItems", 0)

            async def fetch_yesterday_count():
                _, info = await client.get_orders(created_after=(now - timedelta(days=1)).strftime("%Y-%m-%d"), limit=1, page=1)
                today_count_est = (await client.get_orders(created_after=now.strftime("%Y-%m-%d"), limit=1, page=1))[1].get("totalItems", 0)
                return info.get("totalItems", 0) - today_count_est

            current_orders, prev_orders, today_count = await asyncio.gather(
                fetch_period_orders(start),
                fetch_period_orders(prev_start),
                fetch_today_count(),
            )

            # Filter vorige periode orders (verwijder orders die in huidige periode vallen)
            prev_orders = [o for o in prev_orders if o.get("createDate", "") < prev_end]

            # Bereken metrics
            current_metrics = _calculate_sla_metrics(current_orders)
            prev_metrics = _calculate_sla_metrics(prev_orders)

            # Voeg trends toe
            for key in ("on_time_shipping", "order_accuracy", "perfect_order_rate"):
                curr_pct = current_metrics[key]["percentage"]
                prev_pct = prev_metrics[key]["percentage"]
                current_metrics[key]["trend"] = _calc_trend(curr_pct, prev_pct)
                current_metrics[key]["prev_percentage"] = prev_pct

            # Cycle time trend (lager is beter, dus omgekeerd)
            curr_avg = current_metrics["order_cycle_time"]["avg_minutes"]
            prev_avg = prev_metrics["order_cycle_time"]["avg_minutes"]
            ct_trend = _calc_trend(prev_avg, curr_avg)  # Omgekeerd: daling is "up" (verbetering)
            current_metrics["order_cycle_time"]["trend"] = ct_trend
            current_metrics["order_cycle_time"]["prev_avg_minutes"] = prev_avg

            # Today vs yesterday
            yesterday_count = max(0, len([o for o in prev_orders
                                          if o.get("createDate", "").startswith((now - timedelta(days=1)).strftime("%Y-%m-%d"))]))
            if yesterday_count == 0:
                # Fallback: schat op basis van vorige periode gemiddelde
                days_in_prev = max(1, (datetime.strptime(prev_end, "%Y-%m-%d") - datetime.strptime(prev_start, "%Y-%m-%d")).days)
                yesterday_count = len(prev_orders) // days_in_prev if days_in_prev > 0 else 0

            change_pct = round(((today_count - yesterday_count) / yesterday_count * 100), 1) if yesterday_count > 0 else 0

            return {
                "period": period,
                "period_start": start,
                "period_end": now.strftime("%Y-%m-%d"),
                "total_orders": len(current_orders),
                "metrics": current_metrics,
                "today_vs_yesterday": {
                    "orders_today": today_count,
                    "orders_yesterday": yesterday_count,
                    "trend": "up" if today_count > yesterday_count else "down" if today_count < yesterday_count else "stable",
                    "change_percent": change_pct,
                },
            }

        result = await cache.get_or_fetch(cache_key, CACHE_TTL_SLA, fetch)
        return {
            "success": True,
            "data": result,
            "cached": cache.get(cache_key) is not None,
            "timestamp": datetime.now(tz=CET).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] get_sla_metrics: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============ REPORT EXPORT ============

@app.get("/api/reports/export")
async def export_report(
    request: Request,
    format: str = "csv",
    period: str = "week",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
):
    """Exporteer een rapport als CSV. Vereist geldige sessie."""
    try:
        if format not in ("csv",):
            raise HTTPException(status_code=400, detail="Format must be csv")
        if period not in ("week", "month", "custom"):
            raise HTTPException(status_code=400, detail="Period must be week, month, or custom")

        # Session auth check (extra naast middleware, voor logging)
        auth_header = request.headers.get("Authorization", "")
        user_email = "unknown"
        if auth_header.startswith("Bearer "):
            payload = verify_session_token(auth_header[7:])
            if payload:
                user_email = payload.get("email", "unknown")

        client = get_client()
        now = datetime.now(tz=CET)

        # Bepaal periode
        if period == "custom" and start_date and end_date:
            p_start = start_date
            p_end = end_date
        elif period == "month":
            p_start = now.replace(day=1).strftime("%Y-%m-%d")
            p_end = now.strftime("%Y-%m-%d")
        else:  # week
            p_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
            p_end = now.strftime("%Y-%m-%d")

        print(f"[REPORT] Export {format} {period} ({p_start} - {p_end}) by {user_email}")

        # Haal data parallel op (batched voor betrouwbaarheid)
        orders, shipments = await asyncio.gather(
            client._fetch_today_orders_all_pages(p_start, max_pages=100),
            client._fetch_all_shipments(p_start, max_pages=100),
        )

        # Filter orders op einddatum als custom
        if period == "custom" and end_date:
            orders = [o for o in orders if o.get("createDate", "")[:10] <= end_date]

        # Bereken SLA metrics
        sla = _calculate_sla_metrics(orders)

        # Bereken revenue
        total_revenue = 0.0
        for o in orders:
            try:
                total_revenue += float(o.get("totalPaid", "0") or "0")
            except (ValueError, TypeError):
                pass

        # Orders per dag
        orders_by_day: dict[str, dict] = {}
        for o in orders:
            day = o.get("createDate", "")[:10]
            if not day:
                continue
            if day not in orders_by_day:
                orders_by_day[day] = {"count": 0, "revenue": 0.0}
            orders_by_day[day]["count"] += 1
            try:
                orders_by_day[day]["revenue"] += float(o.get("totalPaid", "0") or "0")
            except (ValueError, TypeError):
                pass

        # Webshop verdeling
        webshop_counts: dict[str, int] = {}
        for o in orders:
            shop = o.get("webshopName", "Onbekend")
            webshop_counts[shop] = webshop_counts.get(shop, 0) + 1
        top_webshops = sorted(webshop_counts.items(), key=lambda x: x[1], reverse=True)[:10]

        # Carrier verdeling
        carrier_counts: dict[str, int] = {}
        for s in shipments:
            carrier = s.get("carrier", s.get("carrierName", "Onbekend"))
            carrier_counts[carrier] = carrier_counts.get(carrier, 0) + 1
        top_carriers = sorted(carrier_counts.items(), key=lambda x: x[1], reverse=True)

        # Genereer CSV
        output = io.StringIO()
        output.write("\ufeff")  # BOM voor UTF-8 herkenning in Excel
        writer = csv.writer(output, delimiter=";")  # Puntkomma voor NL Excel

        # Sectie 1: Samenvatting
        writer.writerow(["DURA FULFILMENT RAPPORT"])
        writer.writerow(["Periode", f"{p_start} t/m {p_end}"])
        writer.writerow(["Gegenereerd", now.strftime("%Y-%m-%d %H:%M")])
        writer.writerow([])
        writer.writerow(["SAMENVATTING"])
        writer.writerow(["Totaal orders", len(orders)])
        writer.writerow(["Totaal verzendingen", len(shipments)])
        writer.writerow(["Omzet", f"{total_revenue:.2f}"])
        writer.writerow([])
        writer.writerow(["SLA METRICS"])
        writer.writerow(["On-Time Shipping %", f"{sla['on_time_shipping']['percentage']}%"])
        writer.writerow(["Order Accuracy %", f"{sla['order_accuracy']['percentage']}%"])
        writer.writerow(["Gem. Doorlooptijd (min)", sla["order_cycle_time"]["avg_minutes"]])
        writer.writerow(["Mediaan Doorlooptijd (min)", sla["order_cycle_time"]["median_minutes"]])
        writer.writerow(["P95 Doorlooptijd (min)", sla["order_cycle_time"]["p95_minutes"]])
        writer.writerow(["Perfect Order Rate %", f"{sla['perfect_order_rate']['percentage']}%"])
        writer.writerow([])

        # Sectie 2: Orders per dag
        writer.writerow(["ORDERS PER DAG"])
        writer.writerow(["Datum", "Aantal Orders", "Omzet"])
        for day in sorted(orders_by_day.keys()):
            d = orders_by_day[day]
            writer.writerow([day, d["count"], f"{d['revenue']:.2f}"])
        writer.writerow([])

        # Sectie 3: Top webshops
        writer.writerow(["TOP WEBSHOPS"])
        writer.writerow(["Webshop", "Aantal Orders", "% van totaal"])
        for name, count in top_webshops:
            pct = round(count / len(orders) * 100, 1) if orders else 0
            writer.writerow([name, count, f"{pct}%"])
        writer.writerow([])

        # Sectie 4: Carrier verdeling
        writer.writerow(["CARRIER VERDELING"])
        writer.writerow(["Carrier", "Aantal Verzendingen", "% van totaal"])
        for name, count in top_carriers:
            pct = round(count / len(shipments) * 100, 1) if shipments else 0
            writer.writerow([name, count, f"{pct}%"])

        # Return als streaming response
        output.seek(0)
        filename = f"dura-rapport-{period}-{p_end}.csv"
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[ERROR] export_report: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# ============ INVENTORY STATUS (debug) ============

@app.get("/api/inventory/status")
async def get_inventory_status():
    """Debug endpoint: toont de status van de inventory indexer."""
    cached = cache.get("inventory_active")
    return {
        "indexer": _inventory_status,
        "cache_has_data": cached is not None,
        "cache_count": len(cached) if cached else 0,
        "timestamp": datetime.now(tz=CET).isoformat(),
    }


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
async def get_inventory(
    low_stock_only: bool = False,
    active_only: bool = True,
    search: Optional[str] = None,
    page: int = 1,
    per_page: int = 50,
):
    """Haal voorraad/producten op. active_only=true serveert vanuit de gecachete index."""
    try:
        if low_stock_only:
            cache_key = f"inventory:low_stock:{page}"
            async def fetch():
                client = get_client()
                products = await client.get_low_stock_products()
                return {"items": products, "total": len(products), "lastPage": 1}
            result = await cache.get_or_fetch(cache_key, CACHE_TTL_INVENTORY, fetch)
            return {
                "success": True,
                "count": len(result["items"]),
                "total": result["total"],
                "page": 1,
                "lastPage": result["lastPage"],
                "data": result["items"]
            }

        if active_only:
            # Serveer vanuit gecachete active products index
            active = cache.get("inventory_active")
            if active is None:
                # Index nog niet klaar — val terug op pass-through (1 pagina)
                print("[INVENTORY] Cache not ready, falling back to pass-through", flush=True)
                client = get_client()
                items, page_info = await client.get_products(limit=50, page=page)
                return {
                    "success": True,
                    "count": len(items),
                    "total": page_info.get("totalItems", len(items)),
                    "page": page,
                    "lastPage": page_info.get("lastPage", 1),
                    "data": items,
                    "indexing": True,
                }

            # Backend zoeken op SKU + naam
            filtered = active
            if search:
                q = search.lower()
                filtered = [p for p in active if q in p.get("sku", "").lower() or q in p.get("name", "").lower()]

            # Backend paginering
            total = len(filtered)
            per_page = max(1, min(per_page, 200))
            last_page = max(1, (total + per_page - 1) // per_page)
            page = max(1, min(page, last_page))
            start = (page - 1) * per_page
            end = start + per_page
            page_items = filtered[start:end]

            return {
                "success": True,
                "count": len(page_items),
                "total": total,
                "page": page,
                "lastPage": last_page,
                "data": page_items,
                "active_only": True,
            }

        # Fallback: pass-through naar Goedgepickt (active_only=false)
        cache_key = f"inventory:passthrough:{page}"
        async def fetch():
            client = get_client()
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
