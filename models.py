"""
SQLAlchemy ORM models.

Design notes:
- `products` stores the canonical, deduplicated product record.
- `listings` stores one row per marketplace listing — the same product
  can appear on multiple platforms, each with its own price.
- `price_history` is append-only; we never update or delete rows.
  An index on (listing_id, recorded_at) keeps point-in-time queries fast
  even at millions of rows.
- `price_change_events` is the event log for notifications. Writing here
  is atomic with the price update, so events are never lost even if
  webhook delivery fails later.
- `api_keys` and `api_usage` support per-consumer auth and usage tracking.
- `webhook_subscriptions` lets consumers register URLs for price-change alerts.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Product(Base):
    """
    Canonical product — deduplicated across sources.
    Identified by a normalised (brand, name, category) fingerprint.
    """

    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    brand = Column(String(255), nullable=False, index=True)
    name = Column(String(512), nullable=False)
    category = Column(String(128), nullable=False, index=True)
    # Normalised fingerprint used to merge cross-source duplicates
    fingerprint = Column(String(512), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    listings = relationship("Listing", back_populates="product", lazy="selectin")


class Listing(Base):
    """
    One marketplace listing of a product.
    A single Product can have many Listings (one per source).
    """

    __tablename__ = "listings"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
    source = Column(String(64), nullable=False, index=True)  # 'grailed' | 'fashionphile' | '1stdibs'
    external_id = Column(String(256), nullable=False)       # ID on the source platform
    url = Column(Text, nullable=False)
    image_url = Column(Text, nullable=True)
    current_price = Column(Float, nullable=False)
    currency = Column(String(8), default="USD", nullable=False)
    condition = Column(String(64), nullable=True)
    seller = Column(String(256), nullable=True)
    is_sold = Column(Boolean, default=False, nullable=False)
    last_seen_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    product = relationship("Product", back_populates="listings")
    price_history = relationship("PriceHistory", back_populates="listing", lazy="dynamic")

    # Prevent duplicate source+external_id pairs
    __table_args__ = (
        Index("ix_listings_source_external_id", "source", "external_id", unique=True),
    )


class PriceHistory(Base):
    """
    Append-only price log — one row per observed price per listing.

    Scaling strategy:
    - Composite index on (listing_id, recorded_at) makes time-range queries O(log n).
    - At millions of rows, consider partitioning by month (PostgreSQL) or
      archiving rows older than N days to a separate cold-storage table.
    - We never UPDATE or DELETE rows — this table is immutable by convention.
    """

    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False)
    price = Column(Float, nullable=False)
    currency = Column(String(8), default="USD", nullable=False)
    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    listing = relationship("Listing", back_populates="price_history")

    __table_args__ = (
        Index("ix_price_history_listing_recorded", "listing_id", "recorded_at"),
    )


class PriceChangeEvent(Base):
    """
    Event log for price changes detected during a refresh.

    Why an event log instead of direct webhook calls?
    - Writing here is part of the same DB transaction as the price update,
      so events are durable even if the webhook call fails or the process crashes.
    - A background task reads pending events and attempts delivery,
      updating `delivered_at` on success or `retry_count` on failure.
    - This decouples scraping speed from webhook latency.
    """

    __tablename__ = "price_change_events"

    id = Column(Integer, primary_key=True)
    listing_id = Column(Integer, ForeignKey("listings.id"), nullable=False, index=True)
    old_price = Column(Float, nullable=False)
    new_price = Column(Float, nullable=False)
    currency = Column(String(8), default="USD", nullable=False)
    detected_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    delivered_at = Column(DateTime, nullable=True)   # null = not yet delivered
    retry_count = Column(Integer, default=0, nullable=False)

    listing = relationship("Listing")

    __table_args__ = (
        Index("ix_price_change_events_delivered", "delivered_at"),
    )


class WebhookSubscription(Base):
    """A consumer-registered URL to POST price-change payloads to."""

    __tablename__ = "webhook_subscriptions"

    id = Column(Integer, primary_key=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=False)
    url = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    api_key = relationship("ApiKey", back_populates="webhooks")


class ApiKey(Base):
    """Hashed API key for consumer authentication."""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True)
    key_hash = Column(String(256), unique=True, nullable=False)  # bcrypt hash
    label = Column(String(128), nullable=True)                   # human-readable name
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    usage = relationship("ApiUsage", back_populates="api_key", lazy="dynamic")
    webhooks = relationship("WebhookSubscription", back_populates="api_key")


class ApiUsage(Base):
    """One row per API request — used for rate-limit auditing."""

    __tablename__ = "api_usage"

    id = Column(Integer, primary_key=True)
    api_key_id = Column(Integer, ForeignKey("api_keys.id"), nullable=False, index=True)
    endpoint = Column(String(256), nullable=False)
    method = Column(String(8), nullable=False)
    status_code = Column(Integer, nullable=False)
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    api_key = relationship("ApiKey", back_populates="usage")

    __table_args__ = (
        Index("ix_api_usage_key_requested", "api_key_id", "requested_at"),
    )
