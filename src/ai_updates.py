"""
AI Insights Generator voor Dura Fulfilment Dashboard.
Gebruikt Claude Haiku om inzichten te genereren op basis van live Goedgepickt data.
Draait op schema: 08:30, 13:00, 17:30 CET.
"""

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


async def generate_insights():
    """Genereer AI insights op basis van actuele Goedgepickt data."""
    print(f"[AI Insights] Generating insights at {datetime.now().isoformat()}")
    
    api_key = os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        error_msg = "CLAUDE_API_KEY niet geconfigureerd"
        _ai_cache["error"] = error_msg
        print(f"[AI Insights] ERROR: {error_msg}")
        print(f"[AI Insights] Env vars check: CLAUDE_API_KEY={os.getenv('CLAUDE_API_KEY')}, ANTHROPIC_API_KEY={os.getenv('ANTHROPIC_API_KEY')}")
        return
    
    try:
        client = get_client()
        
        # Verzamel data
        from zoneinfo import ZoneInfo
        CET = ZoneInfo("Europe/Amsterdam")
        today_str = datetime.now(tz=CET).strftime("%Y-%m-%d")
        week_start = (datetime.now(tz=CET) - timedelta(days=datetime.now(tz=CET).weekday())).strftime("%Y-%m-%d")
        
        # Orders vandaag
        _, today_info = await client.get_orders(created_after=today_str, limit=1, page=1)
        orders_today = today_info.get("totalItems", 0)
        
        # Orders deze week
        _, week_info = await client.get_orders(created_after=week_start, limit=1, page=1)
        orders_week = week_info.get("totalItems", 0)
        
        # Status verdeling vandaag (sample)
        orders_by_status = {}
        items, _ = await client.get_orders(created_after=today_str, limit=50, page=1)
        for order in items:
            status = order.get("status", "unknown")
            orders_by_status[status] = orders_by_status.get(status, 0) + 1
        
        # Verzendingen
        _, ship_today_info = await client.get_shipments(created_after=today_str, limit=1, page=1)
        shipments_today = ship_today_info.get("totalItems", 0)
        
        # Lage voorraad
        low_stock = await client.get_low_stock_products(threshold=25)
        def _get_stock_int(p):
            s = p.get("stock", 0)
            if isinstance(s, (int, float)):
                return s
            if isinstance(s, str):
                try:
                    return int(s)
                except ValueError:
                    return 0
            return 0
        critical_stock = [p for p in low_stock if _get_stock_int(p) <= 5]
        
        # Bouw context voor Claude
        now = datetime.now()
        hour = now.hour
        time_of_day = "ochtend" if hour < 12 else "middag" if hour < 18 else "avond"
        
        data_summary = f"""
Datum: {now.strftime("%A %d %B %Y")} ({time_of_day})
Tijd: {now.strftime("%H:%M")} CET

ORDERS:
- Vandaag: {orders_today} orders
- Deze week: {orders_week} orders
- Status verdeling vandaag: {json.dumps(orders_by_status, indent=2)}

VERZENDINGEN:
- Vandaag: {shipments_today} verzendingen

VOORRAAD:
- Producten met lage voorraad (≤25): {len(low_stock)}
- Kritiek lage voorraad (≤5): {len(critical_stock)}
- Top 5 kritieke producten: {json.dumps([{"sku": p.get("sku", "?"), "naam": p.get("name", "?"), "voorraad": p.get("stock", 0)} for p in critical_stock[:5]], ensure_ascii=False)}
"""

        # Claude Haiku API call
        anthropic_client = anthropic.Anthropic(api_key=api_key)
        
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": f"""Je bent een AI-assistent voor Dura Fulfilment, een fulfilment bedrijf in Nederland. 
Genereer een kort, actionable inzicht op basis van onderstaande live data.

{data_summary}

Geef exact dit JSON format terug (geen markdown, alleen JSON):
{{
  "title": "Korte pakkende titel (max 8 woorden)",
  "summary": "2-3 zinnen samenvatting van de huidige situatie met concrete cijfers",
  "actions": [
    {{"type": "warning|info|success", "text": "Korte actie of observatie"}},
    {{"type": "warning|info|success", "text": "Korte actie of observatie"}}
  ],
  "page_insights": {{
    "orders": "1 zin specifiek over orders",
    "voorraad": "1 zin specifiek over voorraad", 
    "verzendingen": "1 zin specifiek over verzendingen",
    "rapportages": "1 zin specifiek over trends/rapportage",
    "warehouse": "1 zin specifiek voor warehouse medewerkers"
  }}
}}

Schrijf in het Nederlands. Wees concreet met cijfers. Focus op actionable insights."""
            }]
        )
        
        # Parse response
        response_text = response.content[0].text.strip()
        # Try to extract JSON from response
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]
            response_text = response_text.strip()
        
        insights = json.loads(response_text)
        
        _ai_cache["insights"] = insights
        _ai_cache["generated_at"] = now.isoformat()
        _ai_cache["error"] = None
        
        # Also write to /tmp as backup
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
