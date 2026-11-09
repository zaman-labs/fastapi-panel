from __future__ import annotations

import asyncio
import os

import pytest
from conftest import SECRET, Base, Item, ItemAdmin, Owner, Panel, User, seed
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

from fastapi_panel import AdminSite, ModelAuth

POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="set TEST_POSTGRES_URL=postgresql+asyncpg://... to run")


class SlowItemAdmin(ItemAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).where(text("(SELECT true FROM pg_sleep(2))"))


async def _fresh_tables(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(lambda sync_conn: seed(Session(sync_conn)))


def test_slow_page_is_cut_off_and_the_rest_still_works():
    from sqlalchemy.ext.asyncio import create_async_engine

    # NullPool: TestClient runs its own event loop, and asyncpg connections can't move between loops.
    engine = create_async_engine(POSTGRES_URL, poolclass=NullPool)
    asyncio.run(_fresh_tables(engine))
    site = AdminSite()
    site.register(Item, SlowItemAdmin)
    site.register(Owner)
    app = FastAPI()
    auth = ModelAuth(User, superuser_field="is_superuser", permissions_field="permissions")
    site.mount(app, engine=engine, auth=auth, secret_key=SECRET, query_timeout=0.5)
    panel = Panel(TestClient(app), engine)
    panel.login()

    slow = panel.get("/item")
    assert slow.status_code == 503 and "took too long" in slow.text
    assert panel.get("/owner").status_code == 200
