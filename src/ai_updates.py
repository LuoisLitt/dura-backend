"""
AI Insights Generator voor Dura Fulfilment Dashboard.
Gebruikt Claude Haiku om inzichten te genereren op basis van live Goedgepickt data.
Draait op schema: 08:30, 13:00, 17:30 CET.

P3.1: Uitgebreide pagina-specifieke insights met bottleneck detectie,
burn-rate analyse, carrier verdeling, warehouse stats en trend data.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from typing import Optional

import anthropic

from goedgepickt import get_client

# In-memory cache voor AI insights
_ai_cache = {
    "insights": None,
    "generated_at": None,
    "error": None
}


def _safe_stock(val) -> int:
    """Convert stock value to int safely."""
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            return 0
    return 0


def get_cached_insights() -> dict:
    """Haal gecachede AI insights op."""
    if _ai_cache["insights"] is None:
        return {
            "status": "pending",
            "message": "AI insights worden gegenereerd bij de volgende geplande run (08:30, 13:00, 17:30 CET).",
            "generated_at": None,
            "error": _ai_cache.get("error")
        }
    return {
        "status": "ok",
        "insights": _ai_cache["insights"],
        "generated_at": _ai_cache["generated_at"],
        "error": _ai_cache.get("error")
    }


async def _safe_fetch(coro, fallback=None):
    """Voer coroutine uit met error handling."""
    try:
        return await asyncio.wait_for(coro, timeout=15.0)
    except Exception as e:
        print(f"[AI Insights] Data fetch failed: {type(e).__name__}: {e}")
        return fallback


async def _gather_extended_data(client) -> dict:
    """Verzamel uitgebreide data voor alle pagina's. Parallel waar mogelijk."""
    from zoneinfo import ZoneInfo
    CET = ZoneInfo("Europe/Amsterdam")
    now = datetime.now(tz=CET)
    today_str = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    prev_week_start = (now - timedelta(days=now.weekday() + 7)).strftime("%Y-%m-%d")
    week_ago_str = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    # === FASE 1: Parallel basis-data ophalen ===
    async def fetch_orders_today_count():
        _, info = await client.get_orders(created_after=today_str, limit=1, page=1)
        return info.get("totalItems", 0)

    async def fetch_orders_week_count():
        _, info = await client.get_orders(created_after=week_start, limit=1, page=1)
        return info.get("totalItems", 0)

    async def fetch_prev_week_count():
        _, info = await client.get_orders(created_after=prev_week_start, limit=1, page=1)
        return info.get("totalItems", 0)

    async def fetch_orders_today_sample():
        items, _ = await client.get_orders(created_after=today_str, limit=50, page=1)
        return items

    async def fetch_shipments_today_count():
        _, info = await client.get_shipments(created_after=today_str, limit=1, page=1)
        return info.get("totalItems", 0)

    async def fetch_shipments_week_count():
        _, info = await client.get_shipments(created_after=week_start, limit=1, page=1)
        return info.get("totalItems", 0)

    async def fetch_prev_shipments_count():
        _, info = await client.get_shipments(created_after=prev_week_start, limit=1, page=1)
        return info.get("totalItems", 0)

    async def fetch_shipments_today_sample():
        items, _ = await client.get_shipments(created_after=today_str, limit=50, page=1)
        return items

    async def fetch_low_stock():
        return await client.get_low_stock_products(threshold=25)

    async def fetch_picks_today():
        return await client.get_picks(date_from=now.replace(hour=0, minute=0, second=0))

    async def fetch_orders_week_sample():
        """Sample van week-orders voor burn-rate berekening."""
        items, _ = await client.get_orders(created_after=week_ago_str, limit=50, page=1)
        return items

    (
        orders_today_count,
        orders_week_count,
        prev_week_total,
        orders_today_sample,
        shipments_today_count,
        shipments_week_count,
        prev_shipments_total,
        shipments_today_sample,
        low_stock_products,
        picks_today,
        orders_week_sample,
    ) = await asyncio.gather(
        _safe_fetch(fetch_orders_today_count(), fallback=0),
        _safe_fetch(fetch_orders_week_count(), fallback=0),
        _safe_fetch(fetch_prev_week_count(), fallback=0),
        _safe_fetch(fetch_orders_today_sample(), fallback=[]),
        _safe_fetch(fetch_shipments_today_count(), fallback=0),
        _safe_fetch(fetch_shipments_week_count(), fallback=0),
        _safe_fetch(fetch_prev_shipments_count(), fallback=0),
        _safe_fetch(fetch_shipments_today_sample(), fallback=[]),
        _safe_fetch(fetch_low_stock(), fallback=[]),
        _safe_fetch(fetch_picks_today(), fallback=[]),
        _safe_fetch(fetch_orders_week_sample(), fallback=[]),
    )

    # === FASE 2: Bereken afgeleide data (CPU only) ===

    # Orders status verdeling
    orders_by_status = {}
    for order in (orders_today_sample or []):
        status = order.get("status", "unknown")
        orders_by_status[status] = orders_by_status.get(status, 0) + 1

    # Bottleneck detectie: orders die lang open staan
    bottleneck_orders = []
    attention_orders = []
    webshop_counts = {}
    for order in (orders_today_sample or []):
        # Webshop verdeling
        shop = order.get("webshopName", "Onbekend")
        webshop_counts[shop] = webshop_counts.get(shop, 0) + 1

        # Attention needed
        if order.get("attentionNeeded") in (1, "1", True):
            attention_orders.append({
                "id": order.get("orderId", "?"),
                "status": order.get("status", "?"),
                "shop": shop,
            })

        # Stuck orders: >2 uur oud en niet verzonden/afgerond
        create_date = order.get("createDate")
        status = order.get("status", "")
        if create_date and status not in ("completed", "delivered", "shipped"):
            try:
                created = datetime.fromisoformat(create_date.replace("Z", "+00:00"))
                age_hours = (now.replace(tzinfo=None) - created.replace(tzinfo=None)).total_seconds() / 3600
                if age_hours > 2:
                    bottleneck_orders.append({
                        "id": order.get("orderId", "?"),
                        "status": status,
                        "age_hours": round(age_hours, 1),
                        "shop": shop,
                    })
            except (ValueError, TypeError):
                pass

    # Top webshops
    top_webshops = sorted(webshop_counts.items(), key=lambda x: x[1], reverse=True)[:5]

    # Carrier verdeling uit shipments
    carrier_counts = {}
    for shipment in (shipments_today_sample or []):
        carrier = shipment.get("carrier", shipment.get("carrierName", "Onbekend"))
        if carrier:
            carrier_counts[carrier] = carrier_counts.get(carrier, 0) + 1

    # Voorraad: kritieke producten + burn-rate schatting
    critical_stock = [p for p in (low_stock_products or []) if _safe_stock(p.get("stock", 0)) <= 5]

    # Burn-rate: tel hoe vaak producten voorkomen in orders van afgelopen week
    product_order_count = {}
    for order in (orders_week_sample or []):
        order_lines = order.get("orderLines", order.get("items", []))
        if isinstance(order_lines, list):
            for line in order_lines:
                sku = line.get("sku", "")
                qty = 1
                try:
                    qty = int(line.get("quantity", 1))
                except (ValueError, TypeError):
                    pass
                if sku:
                    product_order_count[sku] = product_order_count.get(sku, 0) + qty

    # Koppel burn-rate aan low stock producten
    burn_rate_products = []
    for p in (low_stock_products or [])[:20]:
        sku = p.get("sku", "")
        stock = _safe_stock(p.get("stock", 0))
        weekly_demand = product_order_count.get(sku, 0)
        daily_demand = weekly_demand / 7 if weekly_demand > 0 else 0
        days_remaining = round(stock / daily_demand, 1) if daily_demand > 0 else None
        if daily_demand > 0:
            burn_rate_products.append({
                "sku": sku,
                "naam": p.get("name", "?"),
                "voorraad": stock,
                "dagelijks_verkoop": round(daily_demand, 1),
                "dagen_resterend": days_remaining,
            })
    burn_rate_products.sort(key=lambda x: x.get("dagen_resterend") or 999)

    # Warehouse: picks per medewerker
    picks_per_user = {}
    picks_per_hour = {}
    for pick in (picks_today or []):
        user = pick.get("userName", pick.get("user", "Onbekend"))
        picks_per_user[user] = picks_per_user.get(user, 0) + 1
        # Piek-uren
        pick_time = pick.get("createdAt", pick.get("createDate", ""))
        if pick_time:
            try:
                pt = datetime.fromisoformat(pick_time.replace("Z", "+00:00"))
                hour_key = pt.hour
                picks_per_hour[hour_key] = picks_per_hour.get(hour_key, 0) + 1
            except (ValueError, TypeError):
                pass

    # Openstaande orders (niet verzonden)
    open_statuses = {"open", "new", "processing", "picked", "picking"}
    open_orders_count = sum(c for s, c in orders_by_status.items() if s.lower() in open_statuses)

    # Trend data: vorige week vergelijking
    prev_week_orders = max(0, (prev_week_total or 0) - (orders_week_count or 0))
    prev_week_shipments = max(0, (prev_shipments_total or 0) - (shipments_week_count or 0))

    return {
        "orders_today": orders_today_count or 0,
        "orders_week": orders_week_count or 0,
        "orders_by_status": orders_by_status,
        "bottleneck_orders": bottleneck_orders[:5],
        "attention_orders": attention_orders[:5],
        "top_webshops": top_webshops,
        "shipments_today": shipments_today_count or 0,
        "shipments_week": shipments_week_count or 0,
        "carrier_counts": carrier_counts,
        "low_stock_count": len(low_stock_products or []),
        "critical_stock": [{"sku": p.get("sku", "?"), "naam": p.get("name", "?"), "voorraad": _safe_stock(p.get("stock", 0))} for p in critical_stock[:5]],
        "burn_rate_products": burn_rate_products[:5],
        "picks_today_total": len(picks_today or []),
        "picks_per_user": dict(sorted(picks_per_user.items(), key=lambda x: x[1], reverse=True)[:5]),
        "picks_per_hour": picks_per_hour,
        "open_orders_count": open_orders_count,
        "prev_week_orders": prev_week_orders,
        "prev_week_shipments": prev_week_shipments,
    }


def _build_data_summary(data: dict) -> str:
    """Bouw de data context string voor het Claude prompt."""
    now = datetime.now()
    hour = now.hour
    time_of_day = "ochtend" if hour < 12 else "middag" if hour < 18 else "avond"

    return f"""
Datum: {now.strftime("%A %d %B %Y")} ({time_of_day})
Tijd: {now.strftime("%H:%M")} CET

═══ ORDERS ═══
- Vandaag: {data['orders_today']} orders
- Deze week: {data['orders_week']} orders
- Vorige week: {data['prev_week_orders']} orders
- Status verdeling vandaag: {json.dumps(data['orders_by_status'], indent=2)}
- Bottleneck orders (>2u open, niet verzonden): {json.dumps(data['bottleneck_orders'], ensure_ascii=False)}
- Orders met aandacht nodig: {json.dumps(data['attention_orders'], ensure_ascii=False)}
- Top webshops vandaag: {json.dumps(data['top_webshops'], ensure_ascii=False)}

═══ VOORRAAD ═══
- Producten met lage voorraad (≤25): {data['low_stock_count']}
- Kritiek lage voorraad (≤5): {json.dumps(data['critical_stock'], ensure_ascii=False)}
- Burn-rate analyse (voorraad vs verkoopsnelheid):
{json.dumps(data['burn_rate_products'], ensure_ascii=False, indent=2)}

═══ VERZENDINGEN ═══
- Vandaag: {data['shipments_today']} verzendingen
- Deze week: {data['shipments_week']} verzendingen
- Vorige week: {data['prev_week_shipments']} verzendingen
- Carrier verdeling vandaag: {json.dumps(data['carrier_counts'], ensure_ascii=False)}

═══ WAREHOUSE ═══
- Totaal picks vandaag: {data['picks_today_total']}
- Picks per medewerker: {json.dumps(data['picks_per_user'], ensure_ascii=False)}
- Picks per uur: {json.dumps(data['picks_per_hour'], ensure_ascii=False)}
- Openstaande orders (nog te picken/verzenden): {data['open_orders_count']}
"""


# Prompt template voor Claude
AI_PROMPT_TEMPLATE = """Je bent een AI-assistent voor Dura Fulfilment, een fulfilment bedrijf in Nederland.
Genereer actionable inzichten per portal-pagina op basis van onderstaande live data.

{data_summary}

Geef exact dit JSON format terug (geen markdown, alleen pure JSON):
{{
  "title": "Korte pakkende titel (max 8 woorden)",
  "summary": "2-3 zinnen samenvatting van de huidige situatie met concrete cijfers",
  "actions": [
    {{"type": "warning|info|success", "text": "Korte actie of observatie"}},
    {{"type": "warning|info|success", "text": "Korte actie of observatie"}}
  ],
  "page_insights": {{
    "orders": {{
      "items": [
        {{"type": "warning|info|success", "text": "Concreet inzicht over orders met cijfers"}},
        {{"type": "warning|info|success", "text": "Tweede inzicht over orders"}}
      ]
    }},
    "voorraad": {{
      "items": [
        {{"type": "warning|info|success", "text": "Concreet inzicht over voorraad met cijfers"}},
        {{"type": "warning|info|success", "text": "Tweede inzicht over voorraad (burn-rate)"}}
      ]
    }},
    "verzendingen": {{
      "items": [
        {{"type": "warning|info|success", "text": "Concreet inzicht over verzendingen"}},
        {{"type": "info", "text": "Carrier-specifiek inzicht"}}
      ]
    }},
    "rapportages": {{
      "items": [
        {{"type": "info", "text": "Trend of samenvatting voor management"}},
        {{"type": "info", "text": "Week-vergelijking"}}
      ]
    }},
    "warehouse": {{
      "items": [
        {{"type": "warning|info|success", "text": "Inzicht voor warehouse medewerkers"}},
        {{"type": "info", "text": "Pick-gerelateerd inzicht"}}
      ]
    }}
  }}
}}

REGELS:
- Schrijf in het Nederlands
- Gebruik concrete cijfers uit de data (aantallen, percentages, SKU's, carriernamen)
- "warning" = actie nodig, "info" = ter informatie, "success" = goed nieuws
- Maximaal 3 items per pagina, minimaal 2
- Focus op ACTIONABLE inzichten, niet alleen feitjes
- Bij bottlenecks: noem hoeveel orders en hoe lang ze al wachten
- Bij burn-rate: noem resterende dagen, niet alleen voorraadaantallen
- Als data ontbreekt of 0 is, geef toch een relevant inzicht
- Elke bullet MOET een concreet cijfer bevatten"""


async def generate_insights():
    """Genereer AI insights op basis van actuele Goedgepickt data."""
    print(f"[AI Insights] Generating insights at {datetime.now().isoformat()}")

    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        error_msg = "CLAUDE_API_KEY niet geconfigureerd"
        _ai_cache["error"] = error_msg
        print(f"[AI Insights] ERROR: {error_msg}")
        return

    try:
        client = get_client()

        # Verzamel uitgebreide data (parallel)
        print("[AI Insights] Gathering extended data...")
        data = await _gather_extended_data(client)
        print(f"[AI Insights] Data gathered: {data['orders_today']} orders, {data['shipments_today']} shipments, {data['low_stock_count']} low stock, {data['picks_today_total']} picks")

        # Bouw data summary
        data_summary = _build_data_summary(data)

        # Claude Haiku API call
        anthropic_client = anthropic.Anthropic(api_key=api_key)

        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1200,
            messages=[{
                "role": "user",
                "content": AI_PROMPT_TEMPLATE.format(data_summary=data_summary)
            }]
        )

        # Parse response
        response_text = response.content[0].text.strip()
        # Extract JSON from potential markdown code block
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.strip()

        insights = json.loads(response_text)

        # Valideer page_insights format (objecten met items array verwacht)
        page_insights = insights.get("page_insights", {})
        for key in ["orders", "voorraad", "verzendingen", "rapportages", "warehouse"]:
            val = page_insights.get(key)
            if isinstance(val, str):
                # Backward compat v1: enkele string -> items array
                page_insights[key] = {"items": [{"type": "info", "text": val}]}
            elif isinstance(val, list):
                # Backward compat v2: array van strings -> items array
                items = []
                for item in val:
                    if isinstance(item, str):
                        items.append({"type": "info", "text": item})
                    elif isinstance(item, dict):
                        items.append(item)
                page_insights[key] = {"items": items}
            elif isinstance(val, dict) and "items" in val:
                # Correct format, valideer items
                pass
            else:
                page_insights[key] = {"items": []}

        now = datetime.now()
        _ai_cache["insights"] = insights
        _ai_cache["generated_at"] = now.isoformat()
        _ai_cache["error"] = None

        # Write to /tmp as backup
        try:
            with open("/tmp/ai-insights.json", "w") as f:
                json.dump({
                    "insights": insights,
                    "generated_at": now.isoformat()
                }, f, ensure_ascii=False)
        except Exception:
            pass

        print(f"[AI Insights] Successfully generated insights: {insights.get('title', '?')}")

    except json.JSONDecodeError as e:
        _ai_cache["error"] = f"JSON parse error: {str(e)}"
        print(f"[AI Insights] JSON parse error: {e}")
    except Exception as e:
        _ai_cache["error"] = str(e)
        print(f"[AI Insights] Error generating insights: {e}")


def setup_scheduler(app):
    """Configureer APScheduler voor AI insights generatie."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz
    
    scheduler = AsyncIOScheduler()
    cet = pytz.timezone("Europe/Amsterdam")
    
    # Schedule op 08:30, 13:00, 17:30 CET
    for hour, minute in [(8, 30), (13, 0), (17, 30)]:
        scheduler.add_job(
            generate_insights,
            CronTrigger(hour=hour, minute=minute, timezone=cet),
            id=f"ai_insights_{hour}_{minute}",
            replace_existing=True
        )
    
    # Also run on startup (after 10 seconds delay)
    scheduler.add_job(
        generate_insights,
        "date",
        run_date=datetime.now() + timedelta(seconds=10),
        id="ai_insights_startup"
    )
    
    scheduler.start()
    print("[AI Insights] Scheduler started — runs at 08:30, 13:00, 17:30 CET + startup")
    
    return scheduler
