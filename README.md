# Product-Price-Monitoring-System
# Product Price Monitor

A full-stack system that collects product listings from Grailed, Fashionphile, and 1stdibs, tracks price changes over time, and notifies interested parties via webhooks.

---

## Quick Start

### 1. Clone and install

```bash
git clone <your-repo-url>
cd price-monitor
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Start the server

```bash
# Development (auto-reload)
uvicorn app.main:app --reload

# Run on a fixed host/port (used in these examples)
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

The server starts at `http://localhost:8000` (default), or at `http://127.0.0.1:8001` when using the second command above. Interactive API docs are at `/docs`.

### Local run (Windows PowerShell examples)

Create an API key and use it to trigger a refresh (PowerShell):

```powershell
$k = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/keys -Body (@{label='my-client'} | ConvertTo-Json) -ContentType 'application/json'
$k.raw_key

$headers = @{ Authorization = "Bearer $($k.raw_key)" }
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/refresh -Headers $headers -Body (@{} | ConvertTo-Json) -ContentType 'application/json'
```

Same using `curl` (Linux/macOS or Windows with curl):

```bash
# create key
curl -s -X POST http://127.0.0.1:8001/keys -H "Content-Type: application/json" -d '{"label":"my-client"}'

# trigger refresh (replace <RAW_KEY> with the returned raw_key)
curl -s -X POST http://127.0.0.1:8001/refresh \
  -H "Authorization: Bearer <RAW_KEY>" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### 3. Create your first API key

```bash
curl -X POST http://localhost:8000/keys
```

Copy the `raw_key` from the response — it is shown only once.

### 4. Trigger a data refresh

```bash
curl -X POST http://localhost:8000/refresh \
  -H "Authorization: Bearer <your-key>" \
  -H "Content-Type: application/json" \
  -d '{}'
```

### 5. Open the UI

Visit `http://localhost:8000/ui/index.html`, paste your API key, and click **Refresh Now**.

### 6. Run tests

```bash
pytest tests/ -v
```

---

## API Reference

All endpoints except `POST /keys` require `Authorization: Bearer <key>`.

### Authentication

#### `POST /keys`
Create a new API key. No auth required.

**Request body** (optional):
```json
{ "label": "my-app" }
```

**Response:**
```json
{
  "id": 1,
  "label": "my-app",
  "raw_key": "a3f8c...",
  "created_at": "2024-01-15T10:00:00"
}
```
> Store `raw_key` immediately — it cannot be retrieved later.

---

### Refresh

#### `POST /refresh`
Trigger a data collection run. Runs scrapers concurrently.

**Request body** (optional — omit to refresh all sources):
```json
{ "sources": ["grailed", "fashionphile", "1stdibs"] }
```

**Response:**
```json
{
  "started_at": "2024-01-15T10:00:00",
  "completed_at": "2024-01-15T10:00:05",
  "results": [
    { "source": "grailed", "fetched": 5, "new_listings": 3, "price_changes": 1, "errors": 0 }
  ]
}
```

---

### Products

#### `GET /products`
Browse products with optional filters.

| Query param | Type | Description |
|---|---|---|
| `source` | string | Filter by marketplace (`grailed`, `fashionphile`, `1stdibs`) |
| `category` | string | Partial match on category name |
| `brand` | string | Partial match on brand name |
| `min_price` | float | Minimum current price |
| `max_price` | float | Maximum current price |
| `skip` | int | Pagination offset (default 0) |
| `limit` | int | Page size, max 100 (default 20) |

**Example:**
```bash
curl "http://localhost:8000/products?source=grailed&max_price=500" \
  -H "Authorization: Bearer <key>"
```

#### `GET /products/{id}`
Full product detail with price history.

| Query param | Type | Description |
|---|---|---|
| `history_limit` | int | Max history rows per listing (default 50, max 500) |

#### `GET /products/events/recent`
Recent price-change events. Use this for polling instead of webhooks.

| Query param | Type | Description |
|---|---|---|
| `limit` | int | Number of events (default 50, max 200) |

---

### Analytics

#### `GET /analytics`
Aggregate stats across all data.

**Response:**
```json
{
  "total_products": 12,
  "total_listings": 15,
  "by_source": [
    { "source": "grailed", "total_listings": 5, "avg_price": 317.0, "min_price": 145.0, "max_price": 580.0 }
  ],
  "by_category": [
    { "category": "Handbags", "total_products": 3, "avg_price": 4416.67 }
  ]
}
```

---

### Webhooks

#### `POST /webhooks`
Register a URL to receive price-change events.

```json
{ "url": "https://your-server.com/price-hook" }
```

When a price change is detected, your URL receives a POST:
```json
{
  "event": "price_change",
  "listing_id": 7,
  "old_price": 420.0,
  "new_price": 380.0,
  "currency": "USD",
  "change_pct": -9.52,
  "detected_at": "2024-01-15T10:00:04"
}
```

#### `GET /webhooks`
List your active webhook subscriptions.

---

## Design Decisions

### How does price history scale?

`price_history` is an **append-only table** — we never update or delete rows. Every price observation adds one row.

At millions of rows, the key is the composite index on `(listing_id, recorded_at)`. Queries like "give me the last 50 prices for listing 7" resolve in O(log n) using that index, scanning only the relevant leaf pages.

For further scale:
- **PostgreSQL table partitioning** by month: each month becomes its own physical table. Queries on recent data only touch recent partitions.
- **Archive old rows**: move records older than 6 months to a cold-storage table (`price_history_archive`) that is rarely queried.
- The API already paginates history (`history_limit` parameter), so the response size stays bounded regardless of table size.

### How does price change notification work?

We use an **event log** pattern rather than synchronous webhooks or a message queue.

**Flow:**
1. Scraper detects a price change → writes a row to `price_change_events` in the same DB transaction as the price update. The event is durable even if the process crashes immediately after.
2. After the scrape, `deliver_pending_events()` reads undelivered events and POSTs to all registered webhook URLs.
3. On success: `delivered_at` is stamped. On failure: `retry_count` is incremented. After 5 failures, the event is abandoned (but never deleted — kept for audit).

**Why event log over alternatives?**
- **Direct webhook during scrape**: if the webhook URL is slow or down, it blocks the scraper. Bad.
- **Redis / RabbitMQ queue**: correct but requires running another service. Over-engineered for this scale.
- **Event log in SQLite**: the event is written atomically with the price change (no lost events), delivery is decoupled from scraping, failed deliveries are retried, and everything is inspectable with a SQL query. No extra infrastructure.
- **Polling endpoint** (`GET /products/events/recent`) is also provided for consumers who prefer pull over push.

### How would you extend this to 100+ data sources?

The scraper system uses a **plugin pattern**. Each source is a class inheriting from `BaseScraper` that implements one method: `fetch()`. Adding source #101 means:
1. Create `app/scrapers/source_101.py`
2. Register it in `SCRAPER_MAP` in `routers/refresh.py`
3. No other files change.

For 100+ sources running concurrently, you'd also:
- Move scrapers to **worker processes** (Celery, ARQ, or similar) so scraping doesn't block the API server.
- Add **per-source rate limiting** in `BaseScraper` so aggressive scrapers don't get IP-banned.
- Store scraper configs (URL, headers, schedule) in the database, making the system **configuration-driven** rather than code-driven.

---

## Known Limitations

- **Live scraping**: Grailed, Fashionphile, and 1stdibs all have bot protection. The scrapers fall back to realistic sample data when the live endpoint is unavailable. In production, you'd use official APIs or a headless browser (Playwright).
- **Product deduplication**: matching is done by a normalised `(brand, name, category)` MD5 hash. This works for exact matches but won't catch spelling variations like "Louis Vuitton" vs "LV". A fuzzy matching step (edit distance or embedding similarity) would improve this.
- **No scheduled refresh**: refreshes are triggered manually via the API. Production would use a cron job or a scheduler (APScheduler, Celery Beat) to run on a schedule.
- **Single-process**: the notification delivery runs in the same process as the API. Under high load, move it to a background worker.
- **Auth is key-only**: no expiry, no scopes, no rate limits beyond usage logging. Production would add all three.
- **SQLite concurrency**: SQLite serialises writes. For high write throughput, switch to PostgreSQL (only the `DATABASE_URL` in `database.py` needs changing — SQLAlchemy abstracts the rest).

---

## Project Structure

```
price-monitor/
├── app/
│   ├── main.py              # FastAPI app, lifespan, middleware
│   ├── database.py          # Async SQLAlchemy engine + session factory
│   ├── models.py            # ORM table definitions
│   ├── schemas.py           # Pydantic request/response types
│   ├── auth.py              # API key hashing and verification
│   └── routers/
│       ├── products.py      # GET /products, GET /products/{id}
│       ├── refresh.py       # POST /refresh + upsert pipeline
│       └── analytics.py     # GET /analytics, POST /keys, webhooks
│   └── scrapers/
│       ├── base.py          # BaseScraper with async HTTP + retry
│       ├── grailed.py
│       ├── fashionphile.py
│       └── firstdibs.py
├── notifications/
│   └── event_log.py         # Durable event log + webhook delivery
├── frontend/
│   ├── index.html           # Dashboard
│   ├── products.html        # Browsable product list
│   └── product.html         # Product detail + price history chart
├── tests/
│   ├── conftest.py          # Fixtures: in-memory DB, test client
│   └── test_api.py          # 14 tests covering auth, pipeline, edge cases
├── requirements.txt
├── .gitignore
└── README.md
```

---

## Suggested Git Commit Sequence

For reviewers: commits were made incrementally in this order:

1. `init: project structure, requirements, gitignore`
2. `db: models for products, listings, price_history, events, api_keys`
3. `db: async session factory and init_db`
4. `scrapers: base async class with httpx and tenacity retry`
5. `scrapers: grailed, fashionphile, 1stdibs with sample data fallback`
6. `api: auth — api key generation, hashing, bearer verification`
7. `api: schemas — pydantic request and response types`
8. `api: products router — list, detail, events endpoints`
9. `api: refresh router — concurrent scrape, upsert, price change detection`
10. `api: analytics, keys, webhooks routers`
11. `notifications: event log with retry-safe webhook delivery`
12. `frontend: dashboard, product list, product detail with Chart.js`
13. `tests: conftest fixtures + 14 tests`
14. `docs: README`

