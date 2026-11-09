from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import importlib
import inspect as inspect_module
import logging
import mimetypes
import pkgutil
import re
import secrets
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI
from jinja2 import ChoiceLoader, Environment, FileSystemLoader, PackageLoader, select_autoescape
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, imports, two_factor, views
from .db import Database
from .groups import StaffGroupAdmin
from .log import SETTINGS_SLUG, AdminLog, LiveSetting, SavedFilter, StaffGroup, StaffGroupMember, TwoFactorDevice
from .log import Base as PanelBase
from .options import ImproperlyConfigured, ModelAdmin
from .security import MIN_PASSWORD_LENGTH, SECURITY_HEADERS, BodyTooLarge, LimitBody, LoginThrottle, Signer
from .settings import LiveSettings, Setting, settings_view
from .storage import Storage

RESERVED_SLUGS = {
    "login",
    "logout",
    "password",
    "static",
    "files",
    "forgot-password",
    "reset-password",
    "activity",
    "two-factor",
    SETTINGS_SLUG,
}
RESET_LINK_AGE = 3600
# How long someone has to type their code after the password was right.
TWO_FACTOR_PENDING_AGE = 300
logger = logging.getLogger("fastapi_panel")
INLINE_FILE_TYPES = {"png", "jpg", "jpeg", "gif", "webp"}
FILE_CSP = "default-src 'none'; sandbox"
MIN_SECRET_LENGTH = 32
MAX_PASSWORD_LENGTH = 4096
MAX_FORM_FIELDS = 20_000


class AdminSite:
    def __init__(
        self,
        *,
        title: str = "Admin",
        header: str | None = None,
        index_title: str = "Dashboard",
        template_dirs: Iterable[str | Path] = (),
        extra_css: Iterable[str] = (),
        audit_log: bool = True,
        timezone: str = "UTC",
        naive_utc: bool = True,
        storage: Storage | None = None,
    ) -> None:
        self.title = title
        self.header = header or title
        self.index_title = index_title
        self.extra_css = list(extra_css)
        self.audit_log = audit_log
        self.timezone = timezone
        self.naive_utc = naive_utc
        self.storage = storage
        self._registry: dict[str, ModelAdmin] = {}
        self._pages: list[dict[str, Any]] = []
        self._widgets: list[dict[str, Any]] = []
        self.settings: LiveSettings | None = None
        self._tables_ready = False
        self.templates = Environment(
            loader=ChoiceLoader(
                [FileSystemLoader([str(d) for d in template_dirs]), PackageLoader("fastapi_panel", "templates")]
            ),
            autoescape=select_autoescape(default=True, default_for_string=True),
        )
        self.templates.filters["log_time"] = self._log_time

    # Times. The database keeps its values, people see and type them in this timezone.

    @property
    def timezone(self) -> str:
        return self._timezone

    @timezone.setter
    def timezone(self, name: str) -> None:
        try:
            self._zone = ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ImproperlyConfigured(f"Unknown timezone {name!r}. Use a name like 'Europe/Paris'.") from None
        self._timezone = name

    def to_local(self, value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            if not self.naive_utc:
                return value
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(self._zone)

    def from_local(self, value: dt.datetime, aware: bool) -> dt.datetime:
        """What to store for a time typed in the panel. `value` is naive, `aware` is the column's kind."""
        if not aware and not self.naive_utc:
            return value
        stored = value.replace(tzinfo=self._zone).astimezone(dt.timezone.utc)
        return stored if aware else stored.replace(tzinfo=None)

    def now(self) -> dt.datetime:
        return dt.datetime.now(self._zone)

    def _log_time(self, value: dt.datetime, spec: str = "%Y-%m-%d %H:%M") -> str:
        # The audit log always stores naive UTC, whatever naive_utc says about your own columns.
        return value.replace(tzinfo=dt.timezone.utc).astimezone(self._zone).strftime(spec)

    # Registration

    def register(self, model: type, admin_class: type[ModelAdmin] = ModelAdmin) -> ModelAdmin:
        admin = admin_class(model, self)
        if admin.slug in self._registry or admin.slug in RESERVED_SLUGS:
            raise ImproperlyConfigured(f"The URL name {admin.slug!r} is taken. Set `slug` on {admin_class.__name__}.")
        self._registry[admin.slug] = admin
        return admin

    def unregister(self, model: type) -> None:
        self._registry = {slug: a for slug, a in self._registry.items() if a.model is not model}

    def live_settings(self, **definitions: Setting) -> LiveSettings:
        """Values people can change in the panel without a deploy. Adds a Settings page."""
        if self.settings is not None:
            raise ImproperlyConfigured("Call live_settings once, with every setting.")
        self.settings = LiveSettings(self, definitions)
        return self.settings

    def page(
        self,
        path: str,
        *,
        title: str,
        group: str = "Pages",
        methods: Iterable[str] = ("GET",),
        permission: str | None = None,
    ) -> Callable[[Callable], Callable]:
        """Add a custom page. The view is called as `fn(session, request)` and returns a Response."""

        def decorate(fn: Callable) -> Callable:
            self._pages.append(
                {
                    "path": path,
                    "title": title,
                    "group": group,
                    "methods": list(methods),
                    "permission": permission,
                    "view": fn,
                }
            )
            return fn

        return decorate

    def widget(
        self, title: str, *, url: str | None = None, permission: str | None = None
    ) -> Callable[[Callable], Callable]:
        """Add a stat card to the dashboard. Called as `fn(session, request)`, it returns a number or short text.

        `url` is a path inside the panel, like `"product?stock=low"`, that the card links to.
        """

        def decorate(fn: Callable) -> Callable:
            self._widgets.append({"title": title, "url": url, "permission": permission, "view": fn})
            return fn

        return decorate

    def admin_for_model(self, model: type) -> ModelAdmin | None:
        return next((admin for admin in self._registry.values() if admin.model is model), None)

    def queryset_for(self, model: type, request: Request) -> Any:
        admin = self.admin_for_model(model)
        stmt = admin.get_queryset(request) if admin else select(model)
        keys = inspect(model).primary_key
        return stmt.order_by(*keys)

    def _admin(self, request: Request) -> ModelAdmin:
        admin = self._registry.get(request.path_params["slug"])
        if admin is None:
            raise HTTPException(404, "Not found.")
        return admin

    # Mounting

    def mount(
        self,
        app: FastAPI | Starlette,
        *,
        engine: Any,
        auth: Any,
        secret_key: str,
        path: str = "/admin",
        session_age: int = 12 * 3600,
        secure_cookies: bool | None = None,
        trusted_origins: Iterable[str] = (),
        max_body_size: int = 10 * 1024 * 1024,
        password_reset: Callable[[Any, str], Any] | None = None,
        public_url: str | None = None,
        two_factor: bool = False,
    ) -> None:
        if len(secret_key) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"secret_key must be at least {MIN_SECRET_LENGTH} characters. "
                'Generate one: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if password_reset and not (public_url or "").startswith(("https://", "http://")):
            # Links can't come from the request: its Host header would let an attacker email links to their own site.
            raise ValueError('password_reset needs public_url, the panel\'s address: "https://example.com/admin"')
        self.password_reset = password_reset if auth.can_change_password else None
        self.public_url = (public_url or "").rstrip("/")
        self.db = Database(engine)
        self.auth = auth
        self.cookie_name = session_cookie_name(secret_key)
        self.signer = Signer(secret_key)
        self.file_signer = Signer(secret_key, salt="files")
        self.reset_signer = Signer(secret_key, salt="password-reset")
        self.session_age = session_age
        self.secure_cookies = secure_cookies
        self.trusted_origins = set(trusted_origins)
        self.max_body_size = max_body_size
        self.throttle = LoginThrottle()
        self.reset_throttle = LoginThrottle(max_attempts=3, window=RESET_LINK_AGE)
        self.two_factor = two_factor
        self.two_factor_throttle = LoginThrottle()
        if self.auth.groups and self.admin_for_model(StaffGroup) is None:
            self.register(StaffGroup, StaffGroupAdmin)
        for admin in self._registry.values():
            admin.check()
        if self.audit_log and not any(page["path"] == "/activity" for page in self._pages):
            activity = self.page("/activity", title="Activity", group="System", permission="activity.view")
            activity(lambda session, request: views.activity(session, request, self))
        settings_path = f"/{SETTINGS_SLUG}"
        if self.settings and not any(page["path"] == settings_path for page in self._pages):
            settings_page = self.page(
                settings_path, title="Settings", group="System", methods=("GET", "POST"), permission="settings.change"
            )
            settings_page(lambda session, request: settings_view(session, request, self))
        panel = Starlette(
            routes=self._routes(),
            exception_handlers={404: self._not_found},
            middleware=[Middleware(LimitBody, limit=max_body_size)],
        )
        app.mount(path, panel)

    def _routes(self) -> list[Any]:
        routes: list[Any] = [
            self._route("/", self._index),
            self._route("/login", self._login, methods=["GET", "POST"], public=True),
            self._route("/logout", self._logout, methods=["POST"]),
            self._route("/two-factor", self._two_factor, methods=["GET", "POST"], public=True),
            self._route("/password", self._change_password, methods=["GET", "POST"]),
            self._route("/files/{token}", self._serve_file),
            self._route("/forgot-password", self._forgot_password, methods=["GET", "POST"], public=True),
            self._route("/reset-password/{token}", self._reset_password, methods=["GET", "POST"], public=True),
            Mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static"),
        ]
        for page in self._pages:
            routes.append(self._route(page["path"], self._page_view(page), methods=page["methods"]))
        for slug, admin in self._registry.items():
            for path, view, methods in admin.get_routes():
                routes.append(self._route(f"/{slug}{path}", view, methods=methods))
        routes += [
            self._route("/{slug}", self._model_view(views.changelist), methods=["GET", "POST"]),
            self._route("/{slug}/add", self._model_view(views.change_form), methods=["GET", "POST"]),
            self._route("/{slug}/autocomplete", self._model_view(views.autocomplete)),
            self._route("/{slug}/deleted", self._model_view(views.trash), methods=["GET", "POST"]),
            self._route("/{slug}/filters", self._model_view(views.save_filter), methods=["POST"]),
            self._route("/{slug}/import", self._model_view(imports.import_view), methods=["GET", "POST"]),
            self._route("/{slug}/{pk}/change", self._object_view(views.change_form), methods=["GET", "POST"]),
            self._route("/{slug}/{pk}/delete", self._object_view(views.delete_view), methods=["GET", "POST"]),
            self._route("/{slug}/{pk}/history", self._object_view(views.history_view)),
            self._route("/{slug}/{pk}/action/{name}", self._object_view(views.object_action), methods=["POST"]),
        ]
        return routes

    def _route(self, path: str, view: Callable, *, methods: Iterable[str] = ("GET",), public: bool = False) -> Route:
        async def endpoint(request: Request) -> Response:
            return await self._handle(request, view, public)

        return Route(path, endpoint, methods=list(methods))

    def _model_view(self, view: Callable) -> Callable:
        return lambda session, request: view(session, request, self._admin(request))

    def _object_view(self, view: Callable) -> Callable:
        return lambda session, request: view(session, request, self._admin(request), request.path_params["pk"])

    def _page_view(self, page: dict[str, Any]) -> Callable:
        def view(session: Any, request: Request) -> Response:
            if page["permission"] and not self.auth.has_perm(request.state.user, page["permission"]):
                raise HTTPException(403, "You don't have permission to do that.")
            return page["view"](session, request)

        return view

    # The one path every request takes: session, CSRF, auth, the view, then headers and the cookie.

    async def _handle(self, request: Request, view: Callable, public: bool) -> Response:
        session_data = self._read_session(request)
        request.state.session = session_data
        before = dict(session_data)
        # A missing, expired or badly signed cookie needs replacing, or forms get a token no later request can match.
        session_data.setdefault("csrf", secrets.token_urlsafe(32))
        request.state.user = None
        request.state.form = None
        request.state.trash = False
        request.state.relation_options = {}
        request.state.label_keys = {}
        try:
            if request.method == "POST":
                await self._guard_post(request)
            await self._ensure_tables()
            await self._load_user(request)
            if request.state.user is None and not public:
                response: Response = self._redirect_to_login(request)
            elif inspect_module.iscoroutinefunction(view):
                response = await view(request)
            else:
                response = await self.db.run(view, request)
        except HTTPException as exc:
            response = self._error_page(request, exc.status_code, exc.detail)
        finally:
            await request.close()
        if _is_popup(request) and request.url.path.endswith("/add"):
            # Only the add form opens in the panel's own dialog. Everything else still refuses to be framed.
            policy = SECURITY_HEADERS["Content-Security-Policy"]
            response.headers["Content-Security-Policy"] = policy.replace(
                "frame-ancestors 'none'", "frame-ancestors 'self'"
            )
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.state.session != before:
            self._write_session(request, response)
        return response

    def _read_session(self, request: Request) -> dict[str, Any]:
        raw = request.cookies.get(self.cookie_name)
        data = self.signer.loads(raw) if raw else None
        if not isinstance(data, dict):
            data = {}
        if data.get("uid") and data.get("exp", 0) < time.time():
            data = {}
        return data

    def _write_session(self, request: Request, response: Response) -> None:
        secure = self.secure_cookies if self.secure_cookies is not None else request.url.scheme == "https"
        response.set_cookie(
            self.cookie_name,
            self.signer.dumps(request.state.session),
            max_age=self.session_age,
            path=request.scope["root_path"] or "/",
            httponly=True,
            samesite="lax",
            secure=secure,
        )

    async def _guard_post(self, request: Request) -> None:
        length = request.headers.get("content-length", "0")
        if length.isdigit() and int(length) > self.max_body_size:
            raise HTTPException(413, "That request is too large.")

        origin = request.headers.get("origin")
        if origin and origin != "null":
            parts = urlsplit(origin)
            if parts.netloc != request.url.netloc and origin not in self.trusted_origins:
                raise HTTPException(403, "This request came from another site and was blocked.")
        elif origin == "null" or request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "This request came from another site and was blocked.")

        try:
            form = await request.form(max_fields=MAX_FORM_FIELDS, max_part_size=self.max_body_size)
        except BodyTooLarge:
            raise HTTPException(413, "That request is too large.") from None
        request.state.form = form
        token = form.get("csrf_token", "")
        if not isinstance(token, str) or not hmac.compare_digest(token, request.state.session["csrf"]):
            raise HTTPException(403, "Your session expired or this form is stale. Reload the page and try again.")

    async def _load_user(self, request: Request) -> None:
        session_data = request.state.session
        uid = session_data.get("uid")
        if not uid:
            return
        user = await self.db.run(self.auth.get_user, uid)
        expected = session_data.get("hash", "")
        if (
            user is None
            or not self.auth.is_allowed(user)
            or not hmac.compare_digest(self.auth.session_hash(user), expected)
            # Sessions from before two_factor was switched on must log in again.
            or (self.two_factor and not session_data.get("mfa"))
        ):
            for key in ("uid", "hash", "exp", "mfa"):
                session_data.pop(key, None)
            return
        request.state.user = user

    async def _ensure_tables(self) -> None:
        if self._tables_ready:
            return

        try:
            await self.db.run(self._create_tables)
        except DBAPIError:
            pass  # Another worker won the race, or the table is managed by migrations.
        self._tables_ready = True

    def _create_tables(self, session: Any) -> None:
        tables = [SavedFilter.__table__, *([AdminLog.__table__] if self.audit_log else [])]
        if self.auth.groups:
            tables += [StaffGroup.__table__, StaffGroupMember.__table__]
        if self.settings:
            tables.append(LiveSetting.__table__)
        if self.two_factor:
            tables.append(TwoFactorDevice.__table__)
        PanelBase.metadata.create_all(session.connection(), tables=tables)
        session.commit()

    def _redirect_to_login(self, request: Request) -> Response:
        root = request.scope["root_path"]
        target = request.url.path if request.method == "GET" else f"{root}/"
        return RedirectResponse(f"{root}/login?next={views.quote(target, safe='')}", status_code=303)

    def _error_page(self, request: Request, status: int, detail: str) -> Response:
        response = self.render(
            request, "fastapi_panel/error.html", {"title": f"Error {status}", "status": status, "detail": detail}
        )
        response.status_code = status
        return response

    async def _not_found(self, request: Request, exc: Exception) -> Response:
        return await self._handle(request, _raise_not_found, public=True)

    # Rendering

    def render(
        self, request: Request, template: str, context: dict[str, Any] | None = None, status_code: int = 200
    ) -> HTMLResponse:
        session_data = request.state.session
        messages = session_data.pop("messages", [])
        user = request.state.user
        root = request.scope["root_path"]
        html = self.templates.get_template(template).render(
            {
                "request": request,
                "site": self,
                "root": root,
                "static": f"{root}/static",
                "version": __version__,
                "csrf_token": session_data["csrf"],
                "messages": messages,
                "user": user,
                "popup": _is_popup(request),
                "user_label": self.auth.user_label(user) if user is not None else "",
                "can_change_password": self.auth.can_change_password,
                "nav": self._navigation(request) if user is not None else [],
                **(context or {}),
            }
        )
        return HTMLResponse(html, status_code=status_code)

    def _navigation(self, request: Request) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        current = request.url.path
        root = request.scope["root_path"]
        for admin in self._registry.values():
            if admin.has_module_permission(request):
                url = f"{root}/{admin.slug}"
                can_add = admin.has_add_permission(request)
                groups.setdefault(admin.group_name, []).append(
                    {
                        "label": admin.name_plural,
                        "url": url,
                        "add_url": f"{url}/add" if can_add else None,
                        "active": current == url or current.startswith(f"{url}/"),
                    }
                )
        for page in self._pages:
            if not page["permission"] or self.auth.has_perm(request.state.user, page["permission"]):
                url = f"{root}{page['path']}"
                groups.setdefault(page["group"], []).append(
                    {"label": page["title"], "url": url, "add_url": None, "active": current == url}
                )
        return [
            {"name": name, "items": sorted(items, key=lambda item: item["label"])}
            for name, items in sorted(groups.items())
        ]

    def message(self, request: Request, text: str, level: str = "success") -> None:
        # Kept short: these travel in the cookie until the next page renders.
        queue = request.state.session.setdefault("messages", [])
        queue.append({"text": text[:300], "level": level})
        del queue[:-5]

    # Built-in pages

    def _index(self, session: Any, request: Request) -> Response:
        recent = []
        if self.audit_log:
            stmt = (
                select(AdminLog)
                .where(AdminLog.user_id == self.auth.user_id(request.state.user))
                .order_by(AdminLog.created_at.desc(), AdminLog.id.desc())
                .limit(10)
            )
            recent = list(session.scalars(stmt))
        user = request.state.user
        widgets = [
            self._widget_card(session, request, widget)
            for widget in self._widgets
            if not widget["permission"] or self.auth.has_perm(user, widget["permission"])
        ]
        return self.render(
            request,
            "fastapi_panel/index.html",
            {"title": self.index_title, "recent": recent, "registry": self._registry, "widgets": widgets},
        )

    def _widget_card(self, session: Any, request: Request, widget: dict[str, Any]) -> dict[str, Any]:
        try:
            value = _format_stat(widget["view"](session, request))
        except Exception:
            # Widgets are your code: one failing card shouldn't take the whole dashboard down.
            logger.exception("Dashboard widget %r failed", widget["title"])
            session.rollback()
            value = "Unavailable"
        url = f"{request.scope['root_path']}/{widget['url'].lstrip('/')}" if widget["url"] else None
        return {"title": widget["title"], "value": value, "url": url}

    async def _login(self, request: Request) -> Response:
        root = request.scope["root_path"]
        raw_next = request.query_params.get("next", "")
        target = raw_next if _is_safe_next(raw_next, root) else f"{root}/"
        if request.state.user is not None:
            return RedirectResponse(target, status_code=303)
        error = ""
        username = ""
        status = 200
        if request.method == "POST":
            form = request.state.form
            username = str(form.get("username", ""))[:255]
            password = str(form.get("password", ""))[:MAX_PASSWORD_LENGTH]
            client = request.client.host if request.client else "unknown"
            key = f"{client}|{username.lower()}"
            if self.throttle.blocked(key):
                error = "Too many failed attempts. Try again in a few minutes."
                status = 429
            else:
                user = await self.db.run(self.auth.find_user, username)
                valid = await run_in_threadpool(self.auth.check_password, user, password)
                if valid and user is not None and self.auth.is_allowed(user):
                    self.throttle.reset(key)
                    if self.two_factor:
                        return self._ask_for_code(request, user, target)
                    self._start_session(request, user)
                    return RedirectResponse(target, status_code=303)
                self.throttle.fail(key)
                error = "Please enter a correct username and password."
                status = 400
        return self.render(
            request,
            "fastapi_panel/login.html",
            {"title": "Log in", "error": error, "username": username, "next": raw_next if target == raw_next else ""},
            status_code=status,
        )

    def _start_session(self, request: Request, user: Any) -> None:
        # A new session, including a new CSRF token, so nothing set before login carries over.
        request.state.session = {
            "csrf": secrets.token_urlsafe(32),
            "uid": self.auth.user_id(user),
            "hash": self.auth.session_hash(user),
            "exp": time.time() + self.session_age,
            "mfa": self.two_factor,
        }

    def _ask_for_code(self, request: Request, user: Any, target: str) -> Response:
        # Not logged in yet: the session only remembers who passed the password, for a few minutes.
        request.state.session = {
            "csrf": secrets.token_urlsafe(32),
            "pending": {
                "uid": self.auth.user_id(user),
                "hash": self.auth.session_hash(user),
                "exp": time.time() + TWO_FACTOR_PENDING_AGE,
                "next": target,
            },
        }
        return RedirectResponse(f"{request.scope['root_path']}/two-factor", status_code=303)

    async def _two_factor(self, request: Request) -> Response:
        root = request.scope["root_path"]
        if not self.two_factor:
            raise HTTPException(404, "Not found.")
        if request.state.user is not None:
            return RedirectResponse(f"{root}/", status_code=303)
        pending = request.state.session.get("pending")
        user = await self._user_from_claim(pending)
        if user is None:
            request.state.session.pop("pending", None)
            self.message(request, "That took too long. Log in again.", "error")
            return RedirectResponse(f"{root}/login", status_code=303)
        user_id = self.auth.user_id(user)
        device = await self.db.run(self._find_device, user_id)
        enrolling = device is None
        if enrolling:
            secret = pending.get("setup_secret") or two_factor.new_secret()
            # A new dict, not an edit in place, so the session notices the change and saves it.
            request.state.session["pending"] = {**pending, "setup_secret": secret}
        else:
            secret = device.secret

        error = ""
        status = 200
        if request.method == "POST":
            code = "".join(str(request.state.form.get("code", "")).split())[: two_factor.CODE_DIGITS + 1]
            key = f"two-factor|{user_id}"
            if self.two_factor_throttle.blocked(key):
                error = "Too many failed attempts. Try again in a few minutes."
                status = 429
            else:
                last_step = 0 if enrolling else device.last_step
                step = two_factor.matching_step(secret, code, last_step) if code.isdigit() else None
                if step is not None and await self.db.run(self._accept_code, user_id, secret, step, enrolling):
                    self.two_factor_throttle.reset(key)
                    target = str(pending.get("next") or f"{root}/")
                    self._start_session(request, user)
                    return RedirectResponse(target if _is_safe_next(target, root) else f"{root}/", status_code=303)
                self.two_factor_throttle.fail(key)
                error = "That code didn't work. Check the app and try the newest one."
                status = 400

        context: dict[str, Any] = {"title": "Two-factor authentication", "enrolling": enrolling, "error": error}
        if enrolling:
            uri = two_factor.setup_uri(secret, self.auth.user_label(user), self.header)
            context |= {"qr_code": two_factor.qr_code_data_url(uri), "setup_key": two_factor.display_secret(secret)}
        return self.render(request, "fastapi_panel/two_factor.html", context, status_code=status)

    def _find_device(self, session: Any, user_id: str) -> TwoFactorDevice | None:
        return session.get(TwoFactorDevice, user_id)

    def _accept_code(self, session: Any, user_id: str, secret: str, step: int, enrolling: bool) -> bool:
        if enrolling:
            session.add(TwoFactorDevice(user_id=user_id, secret=secret, last_step=step))
            try:
                session.commit()
            except IntegrityError:
                # Set up in another tab meanwhile. That secret wins, so this code no longer counts.
                session.rollback()
                return False
            return True
        # Conditional, so two requests racing with the same code can't both log in.
        result = session.execute(
            update(TwoFactorDevice)
            .where(TwoFactorDevice.user_id == user_id, TwoFactorDevice.last_step < step)
            .values(last_step=step)
        )
        session.commit()
        return result.rowcount == 1

    def file_url(self, request: Request, name: str) -> str:
        # Links are minted for someone who may see the record, and only work in that person's own session,
        # so a link that leaks into chat or logs is no use to other staff who can't see the record.
        uid = request.state.session.get("uid")
        token = self.file_signer.dumps({"name": name, "uid": uid, "exp": time.time() + self.session_age})
        return f"{request.scope['root_path']}/files/{token}"

    def _serve_file(self, session: Any, request: Request) -> Response:
        payload = self.file_signer.loads(request.path_params["token"])
        if (
            not isinstance(payload, dict)
            or payload.get("exp", 0) < time.time()
            or payload.get("uid") != request.state.session.get("uid")
            or not self.storage
        ):
            raise HTTPException(404, "This file doesn't exist.")
        name = str(payload.get("name", ""))
        try:
            stream = self.storage.open(name)
        except (FileNotFoundError, IsADirectoryError, ValueError):
            raise HTTPException(404, "This file doesn't exist.") from None

        def chunks() -> Iterator[bytes]:
            with stream:
                yield from iter(lambda: stream.read(64 * 1024), b"")

        filename = re.sub(r"[^\w.-]", "_", PurePosixPath(name).name)
        extension = PurePosixPath(name).suffix.lstrip(".").lower()
        # Anything but a plain image downloads, so an uploaded page can't run on the panel's origin.
        disposition = "inline" if extension in INLINE_FILE_TYPES else "attachment"
        return StreamingResponse(
            chunks(),
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            headers={
                "Content-Disposition": f'{disposition}; filename="{filename}"',
                "Content-Security-Policy": FILE_CSP,
            },
        )

    def _logout(self, session: Any, request: Request) -> Response:
        request.state.session = {"csrf": secrets.token_urlsafe(32)}
        return RedirectResponse(f"{request.scope['root_path']}/login", status_code=303)

    async def _change_password(self, request: Request) -> Response:
        if not self.auth.can_change_password:
            raise HTTPException(404, "Not found.")
        errors: dict[str, str] = {}
        if request.method == "POST":
            form = request.state.form
            current = str(form.get("current", ""))[:MAX_PASSWORD_LENGTH]
            new = str(form.get("new", ""))[:MAX_PASSWORD_LENGTH]
            if not await run_in_threadpool(self.auth.check_password, request.state.user, current):
                errors["current"] = "That's not your current password."
            if len(new) < MIN_PASSWORD_LENGTH:
                errors["new"] = f"Use at least {MIN_PASSWORD_LENGTH} characters."
            elif new != form.get("confirm", ""):
                errors["confirm"] = "The two passwords don't match."
            if not errors:
                hashed_user = await self.db.run(self._store_password, self.auth.user_id(request.state.user), new)
                request.state.session["hash"] = self.auth.session_hash(hashed_user)
                self.message(request, "Your password was changed.")
                return RedirectResponse(f"{request.scope['root_path']}/", status_code=303)
        return self.render(request, "fastapi_panel/password.html", {"title": "Change password", "errors": errors})

    def _store_password(self, session: Any, user_id: str, password: str) -> Any:
        user = self.auth.get_user(session, user_id)
        self.auth.set_password(session, user, password)
        session.commit()
        return user

    async def _forgot_password(self, request: Request) -> Response:
        if not self.password_reset:
            raise HTTPException(404, "Not found.")
        sent = request.method == "POST"
        task = None
        if sent:
            username = str(request.state.form.get("username", ""))[:255]
            client = request.client.host if request.client else "unknown"
            key = f"{client}|{username.lower()}"
            # The answer is the same for every username, so this page can't reveal who has an account.
            if not self.reset_throttle.blocked(key):
                self.reset_throttle.fail(key)
                user = await self.db.run(self.auth.find_user, username)
                if user is not None and self.auth.is_allowed(user):
                    task = BackgroundTask(self._send_reset_link, user)
        response = self.render(request, "fastapi_panel/forgot_password.html", {"title": "Reset password", "sent": sent})
        response.background = task
        return response

    async def _send_reset_link(self, user: Any) -> None:
        token = self.reset_signer.dumps(
            {
                "uid": self.auth.user_id(user),
                "hash": self.auth.session_hash(user),
                "exp": time.time() + RESET_LINK_AGE,
            }
        )
        url = f"{self.public_url}/reset-password/{token}"
        try:
            if inspect_module.iscoroutinefunction(self.password_reset):
                await self.password_reset(user, url)
            else:
                await run_in_threadpool(self.password_reset, user, url)
        except Exception:
            logger.exception("Couldn't send a password reset link")

    async def _reset_password(self, request: Request) -> Response:
        if not self.password_reset:
            raise HTTPException(404, "Not found.")
        user = await self._user_from_reset_token(request.path_params["token"])
        if user is None:
            raise HTTPException(400, "This link has expired or was already used. Request a new one.")
        errors: dict[str, str] = {}
        if request.method == "POST":
            form = request.state.form
            new = str(form.get("new", ""))[:MAX_PASSWORD_LENGTH]
            if len(new) < MIN_PASSWORD_LENGTH:
                errors["new"] = f"Use at least {MIN_PASSWORD_LENGTH} characters."
            elif new != form.get("confirm", ""):
                errors["confirm"] = "The two passwords don't match."
            if not errors:
                # The token holds a hash of the old password, so this also uses the link up.
                await self.db.run(self._store_password, self.auth.user_id(user), new)
                self.message(request, "Your password was changed. Log in with the new one.")
                return RedirectResponse(f"{request.scope['root_path']}/login", status_code=303)
        return self.render(
            request, "fastapi_panel/reset_password.html", {"title": "Choose a new password", "errors": errors}
        )

    async def _user_from_reset_token(self, token: str) -> Any | None:
        return await self._user_from_claim(self.reset_signer.loads(token))

    async def _user_from_claim(self, claim: object) -> Any | None:
        """The user a signed `{uid, hash, exp}` claim names, if it hasn't expired and their password is unchanged."""
        if not isinstance(claim, dict) or claim.get("exp", 0) < time.time():
            return None
        user = await self.db.run(self.auth.get_user, str(claim.get("uid", "")))
        if user is None or not self.auth.is_allowed(user):
            return None
        return user if hmac.compare_digest(self.auth.session_hash(user), str(claim.get("hash", ""))) else None


def session_cookie_name(secret_key: str) -> str:
    # Browsers ignore the port when matching cookies, so projects on one host need different names.
    digest = hashlib.sha256(f"cookie-name:{secret_key}".encode()).hexdigest()[:8]
    return f"fastapi_panel_session_{digest}"


def _format_stat(value: Any) -> str:
    if value is None:
        return "\u2014"
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    return str(value)


def _is_popup(request: Request) -> bool:
    return bool(request.query_params.get("_popup"))


def _raise_not_found(session: Any, request: Request) -> Response:
    raise HTTPException(404, "This page doesn't exist.")


def _is_safe_next(target: str, root: str) -> bool:
    if not target.startswith(f"{root}/") or target.startswith("//") or "\\" in target:
        return False
    parts = urlsplit(target)
    return not parts.scheme and not parts.netloc


def autodiscover(*packages: str, module: str = "admin") -> None:
    """Import every `admin` module under the given packages, so their registrations run."""
    for name in packages:
        package = importlib.import_module(name)
        if not hasattr(package, "__path__"):
            continue
        for info in pkgutil.walk_packages(package.__path__, f"{name}."):
            if info.name.rsplit(".", 1)[-1] == module:
                importlib.import_module(info.name)


site = AdminSite()


def register(*models: type, site: AdminSite = site) -> Callable[[type[ModelAdmin]], type[ModelAdmin]]:
    """`@register(Product)` above a ModelAdmin."""

    def decorate(admin_class: type[ModelAdmin]) -> type[ModelAdmin]:
        for model in models:
            site.register(model, admin_class)
        return admin_class

    return decorate
