"""
Dura Fulfilment Dashboard Backend
API server die Goedgepickt data beschikbaar maakt voor het dashboard.
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime, timedelta
from typing import Optional
import os
from dotenv import load_dotenv

from goedgepickt import get_client, GoedgepicktAPI

# Load environment variables
load_dotenv()

# Initialize FastAPI
app = FastAPI(
    title="Dura Fulfilment Dashboard API",
    description="Backend API voor het Dura Fulfilment management dashboard",
    version="1.0.0"
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
    """Health check endpoint."""
    return {
        "status": "online",
        "service": "Dura Fulfilment Dashboard API",
        "timestamp": datetime.now().isoformat()
    }


@app.get("/health")
async def health_check():
    """Uitgebreide health check met Goedgepickt connectie test."""
    api_key = os.getenv("GOEDGEPICKT_API_KEY")
    webshop_id = os.getenv("GOEDGEPICKT_WEBSHOP_ID")
    
    try:
        client = get_client()
        gp_connected = await client.test_connection()
    except Exception as e:
        gp_connected = False
    
    return {
        "status": "healthy" if gp_connected else "degraded",
        "goedgepickt_connected": gp_connected,
        "api_key_set": bool(api_key),
        "webshop_id_set": bool(webshop_id),
        "api_key_length": len(api_key) if api_key else 0,
        "timestamp": datetime.now().isoformat()
    }


# ============ WEBSHOPS ============

@app.get("/api/webshops")
async def get_webshops():
    """Haal alle webshops op."""
    try:
        client = get_client()
        webshops = await client.get_webshops()
        return {"success": True, "count": len(webshops), "data": webshops}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ DASHBOARD ============

@app.get("/api/dashboard")
async def get_dashboard():
    """
    Haal alle dashboard data op in één call.
    Dit is de hoofd-endpoint voor het dashboard.
    """
    try:
        client = get_client()
        stats = await client.get_dashboard_stats()
        
        return {
            "success": True,
            "data": stats,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dashboard/kpis")
async def get_dashboard_kpis():
    """Haal alleen de KPI's op (snelle endpoint voor periodieke refresh)."""
    try:
        client = get_client()
        
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        orders_today = await client.get_orders(date_from=today)
        
        # Tel per status
        pending = sum(1 for o in orders_today if o.get("status") in ["open", "pending"])
        picking = sum(1 for o in orders_today if o.get("status") == "processing")
        shipped = sum(1 for o in orders_today if o.get("status") in ["shipped", "delivered"])
        
        return {
            "orders_today": len(orders_today),
            "pending": pending,
            "picking": picking,
            "shipped": shipped,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ ORDERS ============

@app.get("/api/orders")
async def get_orders(
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    page: int = 1,
    limit: int = 100
):
    """Haal orders op met optionele filters."""
    try:
        client = get_client()
        
        df = datetime.fromisoformat(date_from) if date_from else None
        dt = datetime.fromisoformat(date_to) if date_to else None
        
        orders = await client.get_orders(
            status=status,
            date_from=df,
            date_to=dt,
            limit=limit,
            page=page
        )
        
        return {
            "success": True,
            "count": len(orders),
            "page": page,
            "data": orders
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orders/latest")
async def get_latest_orders(limit: int = 20):
    """Haal de nieuwste orders op via cursor-based pagination."""
    try:
        client = get_client()
        
        # Stap 1: Haal pagina 200 op om de cursor te krijgen
        result = await client._request("GET", "/orders", params={
            "perPage": 50,
            "page": 200
        })
        cursor = result.get("pageInfo", {}).get("cursor")
        
        if not cursor:
            # Fallback: gewoon laatste beschikbare pagina
            items = result.get("items", [])
            items.sort(key=lambda x: x.get("createDate", ""), reverse=True)
            return {"success": True, "count": len(items), "data": items[:limit]}
        
        # Stap 2: Gebruik cursor om verder te pagineren naar het einde
        # Probeer steeds de max pagina met cursor
        latest_items = []
        for _ in range(50):  # max 50 pogingen
            try:
                result = await client._request("GET", "/orders", params={
                    "perPage": 50,
                    "cursor": cursor
                })
                items = result.get("items", [])
                if not items:
                    break
                latest_items = items  # Bewaar steeds de laatste batch
                new_cursor = result.get("pageInfo", {}).get("cursor")
                if not new_cursor or new_cursor == cursor:
                    break
                cursor = new_cursor
            except:
                break
        
        latest_items.sort(key=lambda x: x.get("createDate", ""), reverse=True)
        return {
            "success": True,
            "count": len(latest_items),
            "data": latest_items[:limit]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/debug/test-sort")
async def debug_test_sort(sort_param: str = "", base: str = ""):
    """Test verschillende sort parameters direct op de Goedgepickt API."""
    try:
        client = get_client()
        import httpx
        
        # Probeer fulfilment API als base=fulfilment
        if base == "fulfilment":
            api_url = "https://account.goedgepickt.nl/api/fulfilment/v1/orders?perPage=3"
        else:
            api_url = f"{client.BASE_URL}/orders?perPage=3"
        
        if sort_param:
            api_url += f"&{sort_param}"
        
        async with httpx.AsyncClient() as http:
            resp = await http.get(api_url, headers=client.headers, timeout=30.0)
            data = resp.json()
        
        items = data.get("items", [])
        page_info = data.get("pageInfo", {})
        compact = [{"date": o.get("createDate", o.get("createdAt",""))[:16], "id": o.get("externalDisplayId", o.get("orderId","")), "status": o.get("status",""), "webshop": o.get("webshopName","")} for o in items]
        return {"url_used": api_url, "status": resp.status_code, "pageInfo": page_info, "items": compact, "raw_keys": list(items[0].keys()) if items else []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/debug/raw-orders")
async def debug_raw_orders(page: int = 1, per_page: int = 5, webshop: Optional[str] = None):
    """Debug: toon ruwe API response van Goedgepickt."""
    try:
        client = get_client()
        params = {"perPage": per_page, "page": page}
        if webshop:
            params["webshopUuid"] = webshop
        else:
            params["webshopUuid"] = client.webshop_id
        result = await client._request("GET", "/orders", params=params)
        page_info = result.get("pageInfo", {})
        items = result.get("items", [])
        # Compacte weergave van orders
        compact_items = []
        for o in items:
            compact_items.append({
                "id": o.get("externalDisplayId"),
                "date": o.get("createDate", "")[:16],
                "status": o.get("status"),
                "name": f"{o.get('billingFirstName','')} {o.get('billingLastName','')}".strip(),
                "webshop": o.get("webshopName", "?")
            })
        return {"pageInfo": page_info, "items": compact_items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/orders/all-webshops")
async def get_orders_all_webshops(limit: int = 20):
    """Haal recente orders op over alle webshops."""
    try:
        client = get_client()
        webshops = await client.get_webshops()
        
        all_orders = []
        for ws in webshops:
            ws_uuid = ws.get("uuid", ws.get("id", ""))
            if ws_uuid:
                try:
                    orders = await client.get_orders(limit=limit, webshop_uuid=ws_uuid)
                    for o in orders:
                        o["_webshopName"] = ws.get("name", "Onbekend")
                    all_orders.extend(orders)
                except:
                    pass
        
        # Sort alles op datum, nieuwste eerst
        all_orders.sort(key=lambda x: x.get("createDate", ""), reverse=True)
        
        return {
            "success": True,
            "count": len(all_orders),
            "webshops": len(webshops),
            "data": all_orders[:limit]
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


@app.get("/api/orders/today")
async def get_orders_today():
    """Haal alle orders van vandaag op."""
    try:
        client = get_client()
        orders = await client.get_orders_today()
        return {
            "success": True,
            "count": len(orders),
            "data": orders
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ VOORRAAD ============

@app.get("/api/inventory")
async def get_inventory(low_stock_only: bool = False):
    """Haal voorraad/producten op."""
    try:
        client = get_client()
        
        if low_stock_only:
            products = await client.get_low_stock_products()
        else:
            products = await client.get_products()
        
        return {
            "success": True,
            "count": len(products),
            "data": products
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/inventory/alerts")
async def get_inventory_alerts():
    """Haal alleen producten met voorraad alerts op."""
    try:
        client = get_client()
        products = await client.get_products()
        
        alerts = []
        for product in products:
            stock = product.get("stockLevel", 0)
            min_stock = product.get("minStockLevel", 10)
            
            if stock <= 10:
                alerts.append({
                    **product,
                    "alert_type": "critical",
                    "alert_message": f"Kritiek laag: {stock} stuks"
                })
            elif stock <= min_stock:
                alerts.append({
                    **product,
                    "alert_type": "warning",
                    "alert_message": f"Onder minimum: {stock}/{min_stock} stuks"
                })
        
        return {
            "success": True,
            "count": len(alerts),
            "critical": sum(1 for a in alerts if a["alert_type"] == "critical"),
            "warning": sum(1 for a in alerts if a["alert_type"] == "warning"),
            "data": alerts
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ VERZENDINGEN ============

@app.get("/api/shipments")
async def get_shipments(
    status: Optional[str] = None,
    date_from: Optional[str] = None,
    limit: int = 100
):
    """Haal verzendingen op."""
    try:
        client = get_client()
        
        df = datetime.fromisoformat(date_from) if date_from else None
        
        shipments = await client.get_shipments(
            status=status,
            date_from=df,
            limit=limit
        )
        
        return {
            "success": True,
            "count": len(shipments),
            "data": shipments
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/shipments/{shipment_uuid}/tracking")
async def get_shipment_tracking(shipment_uuid: str):
    """Haal tracking info voor een verzending op."""
    try:
        client = get_client()
        tracking = await client.get_shipment_tracking(shipment_uuid)
        return {"success": True, "data": tracking}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/shipments/carriers")
async def get_carrier_stats():
    """Haal carrier statistieken op voor deze week."""
    try:
        client = get_client()
        
        week_start = datetime.now() - timedelta(days=datetime.now().weekday())
        week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
        
        shipments = await client.get_shipments(date_from=week_start, limit=500)
        
        # Groepeer per carrier
        carriers = {}
        for shipment in shipments:
            carrier = shipment.get("carrier", "unknown")
            if carrier not in carriers:
                carriers[carrier] = {
                    "name": carrier,
                    "count": 0,
                    "delivered": 0,
                    "on_time": 0
                }
            
            carriers[carrier]["count"] += 1
            if shipment.get("status") == "delivered":
                carriers[carrier]["delivered"] += 1
                if shipment.get("deliveredOnTime", True):
                    carriers[carrier]["on_time"] += 1
        
        # Bereken percentages
        for carrier in carriers.values():
            if carrier["delivered"] > 0:
                carrier["on_time_rate"] = round(carrier["on_time"] / carrier["delivered"] * 100, 1)
            else:
                carrier["on_time_rate"] = 100.0
        
        return {
            "success": True,
            "data": list(carriers.values())
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ MEDEWERKERS / WAREHOUSE ============

@app.get("/api/warehouse/employees")
async def get_employee_stats():
    """Haal medewerker statistieken op voor het warehouse display."""
    try:
        client = get_client()
        
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        users = await client.get_users()
        picks = await client.get_picks(date_from=today)
        
        # Groepeer picks per medewerker
        employee_stats = {}
        for user in users:
            user_id = user.get("uuid")
            employee_stats[user_id] = {
                "uuid": user_id,
                "name": user.get("name", "Onbekend"),
                "role": user.get("role", "Picker"),
                "orders_today": 0,
                "orders_per_hour": 0,
                "avg_pick_time": 0,
                "pick_times": []
            }
        
        for pick in picks:
            user_id = pick.get("userUuid")
            if user_id in employee_stats:
                employee_stats[user_id]["orders_today"] += 1
                if pick.get("duration"):
                    employee_stats[user_id]["pick_times"].append(pick["duration"])
        
        # Bereken gemiddelden
        result = []
        for emp in employee_stats.values():
            if emp["pick_times"]:
                emp["avg_pick_time"] = round(sum(emp["pick_times"]) / len(emp["pick_times"]), 1)
            
            # Orders per uur (aangenomen 8 uur werkdag tot nu)
            hours_worked = max(1, (datetime.now().hour - 8))
            emp["orders_per_hour"] = round(emp["orders_today"] / hours_worked, 1)
            
            del emp["pick_times"]  # Niet nodig in response
            result.append(emp)
        
        # Sorteer op orders (hoogste eerst)
        result.sort(key=lambda x: x["orders_today"], reverse=True)
        
        return {
            "success": True,
            "count": len(result),
            "data": result
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/warehouse/live")
async def get_warehouse_live():
    """
    Live data voor het warehouse display.
    Geoptimaliseerd voor snelle polling (elke 30 sec).
    """
    try:
        client = get_client()
        
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        orders = await client.get_orders(date_from=today)
        
        # Tel orders per status
        completed = sum(1 for o in orders if o.get("status") in ["shipped", "delivered"])
        pending = sum(1 for o in orders if o.get("status") in ["open", "pending", "processing"])
        
        # Orders per uur (laatste uur)
        one_hour_ago = datetime.now() - timedelta(hours=1)
        orders_last_hour = sum(
            1 for o in orders 
            if datetime.fromisoformat(o.get("updatedAt", "2000-01-01T00:00:00")) > one_hour_ago
            and o.get("status") in ["shipped", "delivered"]
        )
        
        return {
            "orders_today": len(orders),
            "completed": completed,
            "pending": pending,
            "orders_per_hour": orders_last_hour,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ RUN SERVER ============

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
