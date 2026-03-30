"""
Refresh router.

POST /refresh triggers an async scrape of one or all sources.
The pipeline:
  1. Run scrapers concurrently (asyncio.gather)
  2. For each listing:
     a. Upsert Product (find-or-create by fingerprint)
     b. Upsert Listing (find-or-create by source+external_id)
     c. If price changed → append PriceHistory row + write PriceChangeEvent
  3. Attempt webhook delivery for all undelivered events
  4. Return summary stats
"""

import asyncio
import hashlib
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_api_key, log_usage
from app.database import get_db
from app.models import ApiKey, Listing, PriceHistory, Product
from app.scrapers.base import ScrapedListing, ScrapeResult
from app.scrapers.fashionphile import FashionphileScraper
from app.scrapers.firstdibs import FirstDibsScraper
from app.scrapers.grailed import GrailedScraper
from app.schemas import RefreshRequest, RefreshResponse, RefreshResult
from notifications.event_log import deliver_pending_events, record_price_change

router = APIRouter(prefix="/refresh", tags=["refresh"])
logger = logging.getLogger(__name__)

SCRAPER_MAP = {
    "grailed": GrailedScraper,
    "fashionphile": FashionphileScraper,
    "1stdibs": FirstDibsScraper,
}


def _fingerprint(brand: str, name: str, category: str) -> str:
    """
    Deterministic key for cross-source product deduplication.
    We lowercase and strip whitespace before hashing so minor
    formatting differences between sources don't create duplicates.
    """
    raw = "|".join(
        part.lower().strip() for part in [brand, name, category]
    )
    return hashlib.md5(raw.encode()).hexdigest()


async def _run_scraper(source: str) -> ScrapeResult:
    cls = SCRAPER_MAP[source]
    async with cls() as scraper:
        return await scraper.safe_fetch()


async def _upsert_listing(
    scraped: ScrapedListing,
    db: AsyncSession,
) -> tuple[Listing, bool, bool]:
    """
    Upsert product and listing. Returns (listing, is_new, price_changed).
    All writes happen within the caller's session — committed by the caller.
    """
    fp = _fingerprint(scraped.brand, scraped.name, scraped.category)

    # 1. Find or create canonical Product
    prod_result = await db.execute(
        select(Product).where(Product.fingerprint == fp)
    )
    product = prod_result.scalar_one_or_none()

    if not product:
        product = Product(
            brand=scraped.brand,
            name=scraped.name,
            category=scraped.category,
            fingerprint=fp,
        )
        db.add(product)
        await db.flush()  # get product.id without committing

    # 2. Find or create Listing
    listing_result = await db.execute(
        select(Listing).where(
            Listing.source == scraped.source,
            Listing.external_id == scraped.external_id,
        )
    )
    listing = listing_result.scalar_one_or_none()
    is_new = listing is None

    price_changed = False

    if is_new:
        listing = Listing(
            product_id=product.id,
            source=scraped.source,
            external_id=scraped.external_id,
            url=scraped.url,
            image_url=scraped.image_url,
            current_price=scraped.price,
            currency=scraped.currency,
            condition=scraped.condition,
            seller=scraped.seller,
        )
        db.add(listing)
        await db.flush()
        # Record the opening price
        db.add(PriceHistory(listing_id=listing.id, price=scraped.price, currency=scraped.currency))
    else:
        if abs(listing.current_price - scraped.price) > 0.01:
            price_changed = True
            await record_price_change(listing, listing.current_price, scraped.price, db)
            listing.current_price = scraped.price
            db.add(PriceHistory(listing_id=listing.id, price=scraped.price, currency=scraped.currency))

        listing.last_seen_at = datetime.utcnow()
        listing.image_url = scraped.image_url or listing.image_url

    return listing, is_new, price_changed


@router.post("", response_model=RefreshResponse)
async def trigger_refresh(
    body: RefreshRequest = RefreshRequest(),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    api_key: ApiKey = Depends(get_current_api_key),
):
    started_at = datetime.utcnow()
    sources = body.sources or list(SCRAPER_MAP.keys())

    # Run all requested scrapers concurrently
    scrape_results: list[ScrapeResult] = await asyncio.gather(
        *[_run_scraper(s) for s in sources]
    )

    refresh_results = []

    for scrape_result in scrape_results:
        new_listings = 0
        price_changes = 0

        for scraped in scrape_result.listings:
            try:
                _, is_new, changed = await _upsert_listing(scraped, db)
                if is_new:
                    new_listings += 1
                if changed:
                    price_changes += 1
            except Exception as exc:
                logger.error("Failed to upsert listing %s: %s", scraped.external_id, exc)
                scrape_result.errors += 1

        await db.commit()

        refresh_results.append(
            RefreshResult(
                source=scrape_result.source,
                fetched=len(scrape_result.listings),
                new_listings=new_listings,
                price_changes=price_changes,
                errors=scrape_result.errors,
            )
        )

    # Deliver any pending webhook notifications
    await deliver_pending_events(db)

    completed_at = datetime.utcnow()

    if request:
        await log_usage(api_key, str(request.url.path), "POST", 200, db)

    return RefreshResponse(
        started_at=started_at,
        completed_at=completed_at,
        results=refresh_results,
    )
