from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from sqlalchemy import Engine, event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from .options import ImproperlyConfigured

T = TypeVar("T")
QUERY_CANCELED = "57014"  # PostgreSQL's SQLSTATE for a statement cut off by statement_timeout


class Database:
    """Runs plain synchronous code against a sync or async engine.

    Everything in the panel is written once, against a regular Session. Async engines
    go through `run_sync`, which is why lazy loading just works in templates and hooks.
    """

    def __init__(self, bind: Any, query_timeout: float | None = None) -> None:
        if isinstance(bind, AsyncEngine):
            bind = async_sessionmaker(bind, expire_on_commit=False)
        elif isinstance(bind, Engine):
            bind = sessionmaker(bind, expire_on_commit=False)
        if not isinstance(bind, (sessionmaker, async_sessionmaker)):
            raise TypeError("engine must be an Engine, AsyncEngine or a sessionmaker")
        self.maker = bind
        self.is_async = isinstance(bind, async_sessionmaker)
        self.timeout_ms = _timeout_ms(bind, query_timeout)

    async def run(self, fn: Callable[..., T], *args: Any) -> T:
        if self.is_async:
            session: AsyncSession
            async with self.maker() as session:
                self._limit(session.sync_session)
                return await session.run_sync(fn, *args)
        return await run_in_threadpool(self._run_sync, fn, *args)

    def _run_sync(self, fn: Callable[..., T], *args: Any) -> T:
        session: Session
        with self.maker() as session:
            self._limit(session)
            return fn(session, *args)

    def _limit(self, session: Session) -> None:
        if self.timeout_ms:
            event.listen(session, "after_begin", self._set_timeout)

    def _set_timeout(self, session: Session, transaction: Any, connection: Any) -> None:
        # SET LOCAL ends with the transaction, so the setting never leaks into the app's pooled connections.
        # SET takes no bound parameters; timeout_ms is an int checked in _timeout_ms.
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {self.timeout_ms}")


def _timeout_ms(maker: Any, query_timeout: float | None) -> int | None:
    if query_timeout is None:
        return None
    if isinstance(query_timeout, bool) or not isinstance(query_timeout, (int, float)) or query_timeout <= 0:
        raise ImproperlyConfigured(f"query_timeout must be a number of seconds above 0, not {query_timeout!r}")
    bind = maker.kw.get("bind")
    if bind is None or bind.dialect.name != "postgresql":
        raise ImproperlyConfigured("query_timeout needs a PostgreSQL engine")
    return max(1, round(query_timeout * 1000))


def is_query_timeout(exc: DBAPIError) -> bool:
    code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return code == QUERY_CANCELED
