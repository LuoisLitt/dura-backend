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
    version="2.0.0"
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
        "version": "2.0.0",
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
    """Dashboard KPIs: orders vandaag, deze week, status verdeling."""
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


# ============ ORDERS ============

@app.get("/api/orders")
async def get_orders(
    status: Optional[str] = None,
    created_after: Optional[str] = None,
    page: int = 1,
    limit: int = 50
):
    """Haal orders op. Gebruik created_after=YYYY-MM-DD voor recente orders."""
    try:
        client = get_client()
        items, page_info = await client.get_orders(
            status=status,
            created_after=created_after,
            limit=limit,
            page=page
        )
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
    """Haal de nieuwste orders op (afgelopen 7 dagen, nieuwste eerst)."""
    try:
        client = get_client()
        orders = await client.get_latest_orders(limit=limit)
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


# ============ VERZENDINGEN ============

@app.get("/api/shipments")
async def get_shipments(
    created_after: Optional[str] = None,
    page: int = 1,
    limit: int = 50
):
    """Haal verzendingen op."""
    try:
        client = get_client()
        items, page_info = await client.get_shipments(
            created_after=created_after,
            limit=limit,
            page=page
        )
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
    """Haal de nieuwste verzendingen op."""
    try:
        client = get_client()
        shipments = await client.get_latest_shipments(limit=limit)
        return {
            "success": True,
            "count": len(shipments),
            "data": shipments
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============ RUN SERVER ============

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
