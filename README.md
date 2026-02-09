# Dura Fulfilment Dashboard Backend

Backend API die Goedgepickt WMS data beschikbaar maakt voor het Dura Fulfilment dashboard.

## Setup

### 1. Goedgepickt API Key

Vraag bij Goedgepickt een API key aan:
1. Log in op je Goedgepickt account
2. Ga naar Instellingen → API / Integraties
3. Genereer een nieuwe API key
4. Noteer ook je Webshop UUID

### 2. Environment Variables

Maak een `.env` bestand (of stel in via Railway):

```env
GOEDGEPICKT_API_KEY=jouw_api_key
GOEDGEPICKT_WEBSHOP_ID=jouw_webshop_uuid
CORS_ORIGINS=https://gripai-website.vercel.app
```

### 3. Lokaal draaien

```bash
# Installeer dependencies
pip install -r requirements.txt

# Start server
cd src
uvicorn main:app --reload --port 8000
```

### 4. Deploy naar Railway

```bash
# Login bij Railway
railway login

# Nieuw project
railway init

# Deploy
railway up

# Stel environment variables in
railway variables set GOEDGEPICKT_API_KEY=xxx
railway variables set GOEDGEPICKT_WEBSHOP_ID=xxx
railway variables set CORS_ORIGINS=https://gripai-website.vercel.app
```

## API Endpoints

### Dashboard
- `GET /` - Health check
- `GET /health` - Uitgebreide health check
- `GET /api/dashboard` - Alle dashboard data
- `GET /api/dashboard/kpis` - Alleen KPI's (voor snelle refresh)

### Orders
- `GET /api/orders` - Alle orders (met filters)
- `GET /api/orders/today` - Orders van vandaag
- `GET /api/orders/{uuid}` - Specifieke order

### Voorraad
- `GET /api/inventory` - Alle producten
- `GET /api/inventory?low_stock_only=true` - Alleen lage voorraad
- `GET /api/inventory/alerts` - Producten met alerts

### Verzendingen
- `GET /api/shipments` - Alle verzendingen
- `GET /api/shipments/{uuid}/tracking` - Tracking info
- `GET /api/shipments/carriers` - Carrier statistieken

### Warehouse Display
- `GET /api/warehouse/employees` - Medewerker statistieken
- `GET /api/warehouse/live` - Live data (voor polling)

## Dashboard Koppeling

Na deployment, update het dashboard om de API te gebruiken:

```javascript
const API_URL = 'https://jouw-railway-app.railway.app';

async function loadDashboard() {
    const response = await fetch(`${API_URL}/api/dashboard`);
    const data = await response.json();
    // Update dashboard met data
}

// Refresh elke 30 seconden
setInterval(loadDashboard, 30000);
```

## Goedgepickt API Documentatie

- Officiële docs: https://developers.goedgepickt.nl/
- Rate limits: 100 requests/minuut
- Webhooks beschikbaar voor real-time updates

## Architectuur

```
┌─────────────────────┐     ┌─────────────────────┐
│   Dashboard (Web)   │────▶│   Backend (Python)  │
│   Vercel            │     │   Railway           │
└─────────────────────┘     └──────────┬──────────┘
                                       │
                                       ▼
                            ┌─────────────────────┐
                            │   Goedgepickt API   │
                            │   (WMS Data)        │
                            └─────────────────────┘
```
