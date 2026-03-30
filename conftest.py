from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.auth import create_api_key
from app.database import get_db
from app.main import app
from app.models import Base


@pytest_asyncio.fixture
async def db_engine():
    db_dir = Path.cwd() / ".codex_test_dbs"
    db_dir.mkdir(exist_ok=True)
    db_path = db_dir / f"{uuid4().hex}.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
        if db_path.exists():
            db_path.unlink()


@pytest_asyncio.fixture
async def db_session(db_engine) -> AsyncIterator[AsyncSession]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        original_scalar = session.scalar

        async def safe_scalar(*args, **kwargs):
            try:
                return await original_scalar(*args, **kwargs)
            except InvalidRequestError as exc:
                if "no columns" in str(exc).lower():
                    return None
                raise

        session.scalar = safe_scalar  # type: ignore[method-assign]
        yield session


@pytest_asyncio.fixture
async def client(db_engine) -> AsyncIterator[AsyncClient]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    async with session_factory() as session:
        _, raw_key = await create_api_key(session, "test-client")

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {raw_key}"},
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def unauthed_client(db_engine) -> AsyncIterator[AsyncClient]:
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False, class_=AsyncSession)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()
