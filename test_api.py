"""
Test suite for the Price Monitor API.

Coverage includes:
  - Auth (valid key, missing key, wrong key)
  - Refresh pipeline (new listings, price change detection, deduplication)
  - Product list and detail endpoints
  - Analytics aggregation
  - Scraper normalisation and sample data
  - Notification event log
  - Input validation / edge cases
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.models import Listing, PriceChangeEvent, PriceHistory, Product
from app.routers.refresh import _fingerprint, _upsert_listing
from app.scrapers.base import ScrapedListing
from app.scrapers.grailed import GrailedScraper
from app.scrapers.fashionphile import FashionphileScraper
from app.scrapers.firstdibs import FirstDibsScraper
from notifications.event_log import record_price_change


# ---------------------------------------------------------------------------
# 1. Health check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# 2. Auth — missing bearer token returns 403
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unauthenticated_request_rejected(unauthed_client: AsyncClient):
    response = await unauthed_client.get("/products")
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 3. Auth — wrong token returns 401
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wrong_api_key_rejected(unauthed_client: AsyncClient):
    response = await unauthed_client.get(
        "/products",
        headers={"Authorization": "Bearer definitely-not-a-real-key"},
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# 4. Refresh creates new products and listings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_creates_listings(client: AsyncClient, db_session):
    response = await client.post("/refresh", json={"sources": ["grailed"]})
    assert response.status_code == 200

    data = response.json()
    grailed = next(r for r in data["results"] if r["source"] == "grailed")
    assert grailed["fetched"] > 0
    assert grailed["new_listings"] > 0

    # Verify data actually landed in the DB
    count = await db_session.scalar(select(Listing).where(Listing.source == "grailed").with_only_columns())
    # Just check that at least one listing exists
    result = await db_session.execute(select(Listing).where(Listing.source == "grailed"))
    listings = result.scalars().all()
    assert len(listings) > 0


# ---------------------------------------------------------------------------
# 5. Refresh is idempotent — running twice doesn't duplicate listings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_idempotent(client: AsyncClient, db_session):
    await client.post("/refresh", json={"sources": ["fashionphile"]})
    result_before = await db_session.execute(
        select(Listing).where(Listing.source == "fashionphile")
    )
    count_before = len(result_before.scalars().all())

    await client.post("/refresh", json={"sources": ["fashionphile"]})
    result_after = await db_session.execute(
        select(Listing).where(Listing.source == "fashionphile")
    )
    count_after = len(result_after.scalars().all())

    assert count_before == count_after, "Second refresh should not create duplicate listings"


# ---------------------------------------------------------------------------
# 6. Price change detection — upsert detects a changed price
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_change_detected(db_session):
    scraped_v1 = ScrapedListing(
        source="grailed",
        external_id="test-price-change-99",
        url="https://grailed.com/listings/test-price-change-99",
        brand="Test Brand",
        name="Test Jacket",
        category="Outerwear",
        price=200.00,
        currency="USD",
    )
    await _upsert_listing(scraped_v1, db_session)
    await db_session.commit()

    scraped_v2 = ScrapedListing(**{**scraped_v1.__dict__, "price": 180.00})
    _, is_new, changed = await _upsert_listing(scraped_v2, db_session)
    await db_session.commit()

    assert not is_new
    assert changed

    # PriceChangeEvent should exist
    result = await db_session.execute(
        select(PriceChangeEvent).where(PriceChangeEvent.old_price == 200.00)
    )
    event = result.scalar_one_or_none()
    assert event is not None
    assert event.new_price == 180.00


# ---------------------------------------------------------------------------
# 7. Price history is appended, not overwritten
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_history_appended(db_session):
    scraped = ScrapedListing(
        source="1stdibs",
        external_id="history-test-77",
        url="https://1stdibs.com/77",
        brand="Chanel",
        name="History Test Bag",
        category="Handbags",
        price=1000.00,
        currency="USD",
    )
    await _upsert_listing(scraped, db_session)
    await db_session.commit()

    scraped.price = 950.00
    await _upsert_listing(scraped, db_session)
    await db_session.commit()

    scraped.price = 900.00
    await _upsert_listing(scraped, db_session)
    await db_session.commit()

    listing_result = await db_session.execute(
        select(Listing).where(Listing.external_id == "history-test-77")
    )
    listing = listing_result.scalar_one()

    history_result = await db_session.execute(
        select(PriceHistory).where(PriceHistory.listing_id == listing.id)
    )
    history = history_result.scalars().all()

    assert len(history) == 3, "Should have one row per price observation"
    prices = {h.price for h in history}
    assert prices == {1000.00, 950.00, 900.00}


# ---------------------------------------------------------------------------
# 8. Products list endpoint returns data and respects filters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_products_list_and_filter(client: AsyncClient):
    # Seed data
    await client.post("/refresh", json={"sources": ["grailed"]})

    response = await client.get("/products")
    assert response.status_code == 200
    products = response.json()
    assert isinstance(products, list)
    assert len(products) > 0

    # Filter by source
    filtered = await client.get("/products?source=grailed")
    assert filtered.status_code == 200
    for p in filtered.json():
        sources = [l["source"] for l in p["listings"]]
        assert "grailed" in sources


# ---------------------------------------------------------------------------
# 9. Product detail returns 404 for unknown id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_product_detail_not_found(client: AsyncClient):
    response = await client.get("/products/999999")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# 10. Analytics returns expected structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_analytics_structure(client: AsyncClient):
    await client.post("/refresh", json={})

    response = await client.get("/analytics")
    assert response.status_code == 200

    data = response.json()
    assert "total_products" in data
    assert "total_listings" in data
    assert "by_source" in data
    assert "by_category" in data
    assert isinstance(data["by_source"], list)
    assert isinstance(data["by_category"], list)


# ---------------------------------------------------------------------------
# 11. Fingerprint deduplication across sources
# ---------------------------------------------------------------------------

def test_fingerprint_is_case_insensitive():
    fp1 = _fingerprint("Chanel", "Classic Flap", "Handbags")
    fp2 = _fingerprint("chanel", "classic flap", "handbags")
    fp3 = _fingerprint("  CHANEL  ", "  Classic Flap  ", "  HANDBAGS  ")
    assert fp1 == fp2 == fp3


# ---------------------------------------------------------------------------
# 12. Scraper sample data is well-formed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grailed_sample_data_valid():
    scraper = GrailedScraper()
    listings = scraper._sample_listings()
    assert len(listings) >= 3
    for l in listings:
        assert l.source == "grailed"
        assert l.price > 0
        assert l.brand
        assert l.external_id


@pytest.mark.asyncio
async def test_fashionphile_sample_data_valid():
    scraper = FashionphileScraper()
    listings = scraper._sample_listings()
    for l in listings:
        assert l.source == "fashionphile"
        assert l.price > 0


@pytest.mark.asyncio
async def test_firstdibs_sample_data_valid():
    scraper = FirstDibsScraper()
    listings = scraper._sample_listings()
    for l in listings:
        assert l.source == "1stdibs"
        assert l.price > 0


# ---------------------------------------------------------------------------
# 13. Invalid refresh source returns 422
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_invalid_source_rejected(client: AsyncClient):
    response = await client.post("/refresh", json={"sources": ["ebay"]})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# 14. Price change event log is queryable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_log_endpoint(client: AsyncClient):
    response = await client.get("/products/events/recent")
    assert response.status_code == 200
    assert isinstance(response.json(), list)
