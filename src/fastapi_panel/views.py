from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode

from sqlalchemy import and_, func, inspect, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import MANYTOONE
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, RedirectResponse, Response

from .forms import Form, ValidationError
from .introspect import (
    coerce_to_column,
    humanize,
    object_label,
    pk_criterion,
    pk_keys,
    pk_string,
    pluralize,
    split_camel,
)
from .log import SETTINGS_SLUG, AdminLog, SavedFilter, record
from .options import InlineAdmin, ModelAdmin

MAX_INLINE_ROWS = 1000
AUTOCOMPLETE_RESULTS = 20
MAX_RELATED_LISTED = 25
MAX_DELETE_ROWS = 5000
MAX_DELETE_DEPTH = 50
ACTIVITY_ACTIONS = ("add", "change", "delete", "restore")
ACTIVITY_PER_PAGE = 50
MAX_ACTIVITY_USERS = 200
MAX_SAVED_FILTERS = 50
MAX_FILTER_QUERY = 2000
DB_ERROR = "This couldn't be saved because it conflicts with existing data or a database constraint."


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _url_pk(obj: Any) -> str:
    # Quoted twice: ASGI decodes the path once, then pk_criterion decodes each part.
    return quote(pk_string(obj), safe="")


def _root(request: Any) -> str:
    return request.scope["root_path"]


def _list_url(request: Any, admin: ModelAdmin) -> str:
    back = request.query_params.get("_filters", "")
    return f"{_root(request)}/{admin.slug}" + (f"?{back}" if back else "")


def _keep_filters(request: Any) -> str:
    back = request.query_params.get("_filters", "")
    return f"?_filters={quote(back, safe='')}" if back else ""


def _crumbs(request: Any, admin: ModelAdmin, *extra: tuple[str, str | None]) -> list[tuple[str, str | None]]:
    return [("Home", f"{_root(request)}/"), (admin.name_plural, _list_url(request, admin)), *extra]


def _page_numbers(current: int, last: int) -> list[int | None]:
    wanted = {1, last, current, current - 1, current + 1, current - 2, current + 2}
    numbers: list[int | None] = []
    previous = 0
    for number in sorted(n for n in wanted if 1 <= n <= last):
        if number - previous > 1:
            numbers.append(None)
        numbers.append(number)
        previous = number
    return numbers


def _query_url(request: Any, **changes: Any) -> str:
    params = [(k, v) for k, v in request.query_params.multi_items() if k not in changes and k != "page"]
    params += [(k, v) for k, v in changes.items() if v not in (None, "")]
    return "?" + urlencode(params) if params else "?"


def _filtered_statement(request: Any, admin: ModelAdmin) -> Any:
    stmt = admin.get_queryset(request)
    params = request.query_params
    for list_filter in admin.get_list_filters(request):
        value = params.get(list_filter.parameter)
        if value:
            stmt = list_filter.apply(stmt, value, request)
    hierarchy = admin.date_hierarchy_helper()
    if hierarchy:
        stmt = hierarchy.apply(stmt, params)
    query = params.get("q", "").strip()
    if query and admin.search_fields:
        stmt = admin.apply_search(stmt, query)
    return stmt


def _count(session: Any, stmt: Any) -> int:
    return session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))


def _apply_ordering(request: Any, admin: ModelAdmin, stmt: Any, columns: list[Any]) -> tuple[Any, str]:
    requested = request.query_params.get("o", "")
    name = requested.lstrip("-")
    sortable = {c if isinstance(c, str) else c.__name__: admin.sort_expression(c) for c in columns}
    specs = admin.get_ordering(request)
    active = ""
    if sortable.get(name):
        prefix = "-" if requested.startswith("-") else ""
        specs = [prefix + sortable[name]]
        active = requested
    for spec in specs:
        stmt = _order_by(stmt, admin.model, spec)
    # A stable tiebreak, otherwise rows can repeat or vanish between pages.
    pk_columns = [getattr(admin.model, key) for key in pk_keys(admin.model)]
    return stmt.order_by(*[column.desc() for column in pk_columns]), active


def _order_by(stmt: Any, model: type, spec: str) -> Any:
    descending = spec.startswith("-")
    path = spec.lstrip("-")
    current = model
    parts = path.split("__")
    for part in parts[:-1]:
        relation = getattr(current, part)
        stmt = stmt.outerjoin(relation)
        current = relation.property.mapper.class_
    column = getattr(current, parts[-1])
    return stmt.order_by(column.desc() if descending else column.asc())


def changelist(session: Any, request: Any, admin: ModelAdmin) -> Response:
    admin.require(admin.has_view_permission(request))
    if request.method == "POST":
        data = request.state.form
        if "_save_list" in data:
            return _save_list_editable(session, request, admin)
        return _run_action(session, request, admin)
    return _render_changelist(session, request, admin)


def trash(session: Any, request: Any, admin: ModelAdmin) -> Response:
    if not admin.soft_delete_field:
        raise HTTPException(404, "Not found.")
    admin.require(admin.can_see_deleted(request))
    request.state.trash = True
    if request.method == "GET":
        return _render_changelist(session, request, admin)
    data = request.state.form
    if "_restore" not in data:
        return _run_action(session, request, admin)
    admin.require(admin.has_change_permission(request))
    restore_objects(session, request, admin, [admin.get_object(session, request, str(data["_restore"]))])
    session.commit()
    return redirect(request.url.path + (f"?{request.url.query}" if request.url.query else ""))


def restore_objects(session: Any, request: Any, admin: ModelAdmin, objects: list[Any]) -> None:
    admin.require(all(admin.has_change_permission(request, obj) for obj in objects))
    for obj in objects:
        admin.restore_model(session, request, obj)
        record(session, request, admin, obj, "restore")
    session.flush()
    word = admin.name if len(objects) == 1 else admin.name_plural
    admin.message_user(request, f"Restored {len(objects)} {word.lower()}.")


def _render_changelist(
    session: Any, request: Any, admin: ModelAdmin, list_forms: dict[str, Form] | None = None
) -> Response:
    params = request.query_params
    in_trash = request.state.trash
    columns = admin.get_list_display(request)
    base = admin.get_queryset(request)
    stmt = _filtered_statement(request, admin)
    total = _count(session, stmt)
    grand_total = total
    if admin.show_full_result_count and params.multi_items():
        grand_total = _count(session, base)

    per_page = admin.list_per_page
    last_page = max(1, math.ceil(total / per_page))
    raw_page = params.get("page", "1")
    page = min(int(raw_page), last_page) if raw_page.isdigit() and int(raw_page) > 0 else 1

    ordered, active_sort = _apply_ordering(request, admin, stmt, columns)
    objects = list(session.scalars(admin.eager_load(ordered, request).limit(per_page).offset((page - 1) * per_page)))

    can_change = admin.has_change_permission(request)
    editable = set(admin.list_editable) if can_change and not in_trash else set()
    link_columns = set(admin.list_display_links) if admin.list_display_links is not None else {columns[0]}
    if in_trash:
        link_columns = set()
    filters_qs = quote(request.url.query, safe="")
    forms = list_forms or {}
    rows = []
    for obj in objects:
        pk = pk_string(obj)
        form = None
        if editable:
            form = forms.get(pk) or admin.get_list_form(session, request, obj, f"row-{pk}-")
        by_name = {f.name: f for f in form.fields} if form else {}
        cells = []
        for column in columns:
            field = by_name.get(column) if isinstance(column, str) and column in editable else None
            cells.append(
                {
                    "value": None if field else admin.cell(obj, column, request),
                    "field": field,
                    "link": column in link_columns,
                    "name": column if isinstance(column, str) else column.__name__,
                }
            )
        url = f"{_root(request)}/{admin.slug}/{_url_pk(obj)}/change"
        rows.append({"pk": pk, "url": f"{url}?_filters={filters_qs}" if filters_qs else url, "cells": cells})

    headers = []
    for column in columns:
        key = column if isinstance(column, str) else column.__name__
        can_sort = admin.sort_expression(column) is not None
        direction = "asc" if active_sort == key else "desc" if active_sort == f"-{key}" else ""
        toggle = f"-{key}" if direction == "asc" else key
        headers.append(
            {
                "label": admin.column_label(column),
                "url": _query_url(request, o=toggle) if can_sort else None,
                "direction": direction,
            }
        )

    filters = []
    for list_filter in admin.get_list_filters(request):
        current = params.get(list_filter.parameter, "")
        options = [
            {"label": "All", "url": _query_url(request, **{list_filter.parameter: None}), "selected": not current}
        ]
        for value, label in list_filter.options(session, request):
            options.append(
                {
                    "label": label,
                    "url": _query_url(request, **{list_filter.parameter: value}),
                    "selected": current == value,
                }
            )
        filters.append({"title": list_filter.title, "options": options, "active": bool(current)})

    hierarchy = admin.date_hierarchy_helper()
    date_links = None
    if hierarchy:
        date_links = hierarchy.links(session, stmt, params, lambda **kw: _query_url(request, **kw))

    actions = admin.get_actions(request)
    list_url = f"{_root(request)}/{admin.slug}"
    current_query = "" if in_trash else _filter_query(request.url.query)
    title = f"Deleted {admin.name_plural.lower()}" if in_trash else admin.name_plural
    crumbs = [("Home", f"{_root(request)}/"), (admin.name_plural, list_url if in_trash else None)]
    if in_trash:
        crumbs.append(("Deleted", None))
    return admin.site.render(
        request,
        admin.list_template,
        {
            "admin": admin,
            "title": title,
            "crumbs": crumbs,
            "trash": in_trash,
            "list_url": list_url,
            "current_query": current_query,
            "saved_filters": [] if in_trash else _saved_filters(session, request, admin, current_query),
            "trash_url": f"{list_url}/deleted" if admin.can_see_deleted(request) and not in_trash else None,
            "can_restore": in_trash and can_change,
            "headers": headers,
            "rows": rows,
            "total": total,
            "grand_total": grand_total,
            "page": page,
            "last_page": last_page,
            "pages": [
                {"number": n, "url": _query_url(request, page=n) if n else None, "current": n == page}
                for n in _page_numbers(page, last_page)
            ],
            "prev_url": _query_url(request, page=page - 1) if page > 1 else None,
            "next_url": _query_url(request, page=page + 1) if page < last_page else None,
            "filters": filters,
            "any_filter_active": any(f["active"] for f in filters),
            "clear_filters_url": f"{list_url}/deleted" if in_trash else list_url,
            "date_links": date_links,
            "query": params.get("q", ""),
            "hidden_params": [(k, v) for k, v in params.multi_items() if k not in ("q", "page")],
            "actions": actions,
            "editable": bool(editable),
            "can_add": admin.has_add_permission(request) and not in_trash,
            "import_url": f"{list_url}/import" if admin.can_import(request) and not in_trash else None,
            "add_url": f"{list_url}/add",
            "post_url": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
            "per_page": per_page,
        },
    )


def _filter_query(raw: str) -> str:
    return urlencode([(key, value) for key, value in parse_qsl(raw) if key != "page"])


def _saved_filters(session: Any, request: Any, admin: ModelAdmin, current: str) -> list[dict[str, Any]]:
    stmt = (
        select(SavedFilter)
        .where(SavedFilter.user_id == admin.site.auth.user_id(request.state.user), SavedFilter.model == admin.slug)
        .order_by(SavedFilter.name)
    )
    try:
        saved = list(session.scalars(stmt))
    except DBAPIError:
        # The database user can't create the table, and the rest of the list still works without it.
        session.rollback()
        return []
    list_url = f"{_root(request)}/{admin.slug}"
    return [
        {"id": item.id, "name": item.name, "url": f"{list_url}?{item.query}", "active": item.query == current}
        for item in saved
    ]


def save_filter(session: Any, request: Any, admin: ModelAdmin) -> Response:
    admin.require(admin.has_view_permission(request))
    data = request.state.form
    query = _filter_query(str(data.get("query", "")))
    user_id = admin.site.auth.user_id(request.state.user)
    mine = select(SavedFilter).where(SavedFilter.user_id == user_id, SavedFilter.model == admin.slug)
    target = f"{_root(request)}/{admin.slug}" + (f"?{query}" if query else "")
    name = str(data.get("name", "")).strip()[:100]
    try:
        if "_delete" in data:
            found = session.scalars(mine.where(SavedFilter.id == _int_or_zero(data["_delete"]))).first()
            if found is not None:
                session.delete(found)
        elif not name or not query or len(query) > MAX_FILTER_QUERY:
            admin.message_user(request, "Give it a name, and apply a search or filter first.", "warning")
        else:
            found = session.scalars(mine.where(SavedFilter.name == name)).first()
            if found is not None:
                found.query = query
            elif _count(session, mine) >= MAX_SAVED_FILTERS:
                admin.message_user(request, f"You can keep up to {MAX_SAVED_FILTERS} saved filters here.", "warning")
            else:
                session.add(SavedFilter(user_id=user_id[:255], model=admin.slug, name=name, query=query))
                admin.message_user(request, f"Saved “{name}”.")
        session.commit()
    except DBAPIError:
        session.rollback()
        admin.message_user(request, "Saved filters aren't available, their table is missing.", "error")
    return redirect(target if len(target) <= MAX_FILTER_QUERY else f"{_root(request)}/{admin.slug}")


def _int_or_zero(raw: Any) -> int:
    return int(raw) if str(raw).isdigit() else 0


def activity(session: Any, request: Any, site: Any) -> Response:
    params = request.query_params
    # Only entries about models this person may see, since a label can reveal what's in a record.
    visible: dict[str, Any] = {
        slug: admin for slug, admin in site._registry.items() if admin.has_view_permission(request)
    }
    if site.settings and site.auth.has_perm(request.state.user, "settings.change"):
        visible[SETTINGS_SLUG] = SimpleNamespace(name_plural="Settings")
    stmt = select(AdminLog).where(AdminLog.model.in_(visible))
    model, user, action_name = params.get("model", ""), params.get("user", ""), params.get("action", "")
    if model in visible:
        stmt = stmt.where(AdminLog.model == model)
    if user:
        stmt = stmt.where(AdminLog.user_id == user)
    if action_name in ACTIVITY_ACTIONS:
        stmt = stmt.where(AdminLog.action == action_name)

    total = _count(session, stmt)
    last_page = max(1, math.ceil(total / ACTIVITY_PER_PAGE))
    raw_page = params.get("page", "1")
    page = min(int(raw_page), last_page) if raw_page.isdigit() and int(raw_page) > 0 else 1
    entries = session.scalars(
        stmt.order_by(AdminLog.created_at.desc(), AdminLog.id.desc())
        .limit(ACTIVITY_PER_PAGE)
        .offset((page - 1) * ACTIVITY_PER_PAGE)
    )
    users = session.execute(
        select(AdminLog.user_id, AdminLog.user_label)
        .where(AdminLog.model.in_(visible))
        .distinct()
        .order_by(AdminLog.user_label)
        .limit(MAX_ACTIVITY_USERS)
    )
    return site.render(
        request,
        "fastapi_panel/activity.html",
        {
            "title": "Activity",
            "crumbs": [("Home", f"{_root(request)}/"), ("Activity", None)],
            "entries": list(entries),
            "models": visible,
            "users": list(users),
            "actions": ACTIVITY_ACTIONS,
            "selected": {"model": model, "user": user, "action": action_name},
            "total": total,
            "pages": [
                {"number": n, "url": _query_url(request, page=n) if n else None, "current": n == page}
                for n in _page_numbers(page, last_page)
            ],
            "last_page": last_page,
            "any_filter": bool(model or user or action_name),
        },
    )


def _selected_statement(session: Any, request: Any, admin: ModelAdmin, data: Any) -> tuple[Any, bool]:
    if data.get("select_across") == "1":
        return _filtered_statement(request, admin), True
    selected = data.getlist("_selected")
    if not selected:
        return None, False
    selected = selected[: admin.action_max_rows + 1]
    keys = pk_keys(admin.model)
    try:
        if len(keys) == 1:
            column = getattr(admin.model, keys[0])
            values = [coerce_to_column(column, unquote(raw)) for raw in selected]
            return admin.get_queryset(request).where(column.in_(values)), False
        criteria = [pk_criterion(admin.model, raw) for raw in selected]
    except ValueError:
        raise HTTPException(400, "Invalid selection.") from None
    return admin.get_queryset(request).where(or_(*criteria)), False


def _run_action(session: Any, request: Any, admin: ModelAdmin) -> Response:
    data = request.state.form
    back = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    actions = admin.get_actions(request)
    chosen = actions.get(data.get("action", ""))
    if chosen is None:
        admin.message_user(request, "Choose an action first.", "warning")
        return redirect(back)
    stmt, _ = _selected_statement(session, request, admin, data)
    if stmt is None:
        admin.message_user(request, "Select at least one row first.", "warning")
        return redirect(back)
    if _count(session, stmt) > admin.action_max_rows:
        admin.message_user(
            request,
            f"Actions work on up to {admin.action_max_rows:,} rows at a time. Narrow it down with filters.",
            "error",
        )
        return redirect(back)
    objects = list(session.scalars(admin.eager_load(stmt, request)))
    try:
        result = chosen["fn"](session, request, objects)
        session.commit()
    except DBAPIError:
        session.rollback()
        admin.message_user(request, DB_ERROR, "error")
        return redirect(back)
    return result if isinstance(result, Response) else redirect(back)


def _save_list_editable(session: Any, request: Any, admin: ModelAdmin) -> Response:
    admin.require(admin.has_change_permission(request))
    data = request.state.form
    back = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    forms: dict[str, Form] = {}
    for pk in data.getlist("_row"):
        obj = admin.get_object(session, request, pk)
        admin.require(admin.has_change_permission(request, obj))
        form = admin.get_list_form(session, request, obj, f"row-{pk}-")
        admin.validate_form(session, form, data)
        forms[pk] = form
    if not all(form.is_valid for form in forms.values()):
        admin.message_user(request, "Please fix the errors below.", "error")
        return _render_changelist(session, request, admin, forms)
    saved = 0
    try:
        for form in forms.values():
            changed = form.apply(form.obj)
            if changed:
                admin.save_model(session, request, form.obj, True)
                session.flush()
                values = admin.logged_values(form)
                message = ", ".join(changed) + (f". {values}" if values else "")
                record(session, request, admin, form.obj, "change", message)
                saved += 1
        session.commit()
    except DBAPIError:
        session.rollback()
        admin.message_user(request, DB_ERROR, "error")
        return redirect(back)
    admin.message_user(request, f"{saved} {admin.name_plural.lower() if saved != 1 else admin.name.lower()} updated.")
    return redirect(back)


class FormSet:
    """All the rows of one inline on the parent's page."""

    def __init__(
        self, inline: InlineAdmin, session: Any, request: Any, parent: Any, *, persisted: bool, view_only: bool = False
    ) -> None:
        self.inline = inline
        self.session = session
        self.request = request
        self.view_only = view_only
        self.errors: list[str] = []
        self.prefix = inline.prefix
        self.fk, self.parent_key = inline.foreign_key()
        self.names = inline.get_fields(request, None)
        self.existing: list[Any] = []
        if persisted:
            column = getattr(inline.model, self.fk)
            self.existing = list(
                session.scalars(inline.get_queryset(request).where(column == getattr(parent, self.parent_key)))
            )
        self.rows = [self._row(i, obj, True) for i, obj in enumerate(self.existing)]
        if not view_only and inline.has_add_permission(request):
            start = len(self.rows)
            room = inline.extra if inline.max_num is None else max(0, min(inline.extra, inline.max_num - start))
            self.rows += [self._row(start + i, None, False) for i in range(room)]
        self.blank = None if view_only else self._row("__prefix__", None, False)
        self.can_delete = not view_only and inline.has_delete_permission(request)
        self.labels = [inline.column_label(n) for n in self.names]

    def _row(self, index: Any, obj: Any, persisted: bool) -> dict[str, Any]:
        prefix = f"{self.prefix}-{index}-"
        locked = self.view_only or (persisted and not self.inline.has_change_permission(self.request, obj))
        form = self.inline.build_form(
            self.session, self.request, obj, persisted=persisted, names=self.names, prefix=prefix, view_only=locked
        )
        return {
            "form": form,
            "obj": obj,
            "persisted": persisted,
            "pk": pk_string(obj) if persisted else "",
            "prefix": prefix,
            "index": index,
            "delete": False,
            "label": object_label(obj) if persisted else "",
            "skip": False,
        }

    def validate(self, data: Any) -> None:
        try:
            total = min(int(data.get(f"{self.prefix}-TOTAL", "0")), MAX_INLINE_ROWS)
        except ValueError:
            total = 0
        # Only rows that already belong to this parent can be edited, whatever pk the form claims.
        owned = {pk_string(obj): obj for obj in self.existing}
        self.rows = []
        for index in range(total):
            prefix = f"{self.prefix}-{index}-"
            pk = data.get(f"{prefix}_pk", "")
            obj = owned.pop(pk, None)
            if pk and obj is None:
                continue
            row = self._row(index, obj, obj is not None)
            row["delete"] = bool(data.get(f"{prefix}DELETE")) and self.can_delete and obj is not None
            form = row["form"]
            if row["delete"]:
                pass
            elif obj is None and not form.has_input(data):
                row["skip"] = True
            elif obj is None and not self.inline.has_add_permission(self.request):
                row["skip"] = True
            elif form.editable:
                self.inline.validate_form(self.session, form, data)
            self.rows.append(row)
        # Rows missing from the post are left alone, never deleted implicitly.
        kept = [r for r in self.rows if not r["delete"] and not r["skip"]] + list(owned.values())
        if self.inline.max_num is not None and len(kept) > self.inline.max_num:
            self.errors.append(f"Please submit at most {self.inline.max_num} {self.inline.name_plural.lower()}.")
        if len(kept) < self.inline.min_num:
            self.errors.append(f"Please submit at least {self.inline.min_num} {self.inline.name_plural.lower()}.")

    @property
    def is_valid(self) -> bool:
        return not self.errors and all(r["form"].is_valid for r in self.rows if not r["delete"] and not r["skip"])

    def save(self, parent: Any) -> list[str]:
        notes = []
        for row in self.rows:
            if row["skip"]:
                continue
            if row["delete"]:
                self.session.delete(row["obj"])
                notes.append(f"Deleted {self.inline.name.lower()} “{row['label']}”")
            elif row["persisted"]:
                changed = row["form"].apply(row["obj"])
                if changed:
                    self.inline.save_model(self.session, self.request, row["obj"], True)
                    values = self.inline.logged_values(row["form"])
                    notes.append(
                        f"Changed {', '.join(changed).lower()} on {self.inline.name.lower()} “{row['label']}”"
                        + (f" ({values})" if values else "")
                    )
            else:
                child = self.inline.model()
                row["form"].apply(child)
                setattr(child, self.fk, getattr(parent, self.parent_key))
                self.inline.save_model(self.session, self.request, child, False)
                notes.append(f"Added {self.inline.name.lower()}")
        return notes


def change_form(session: Any, request: Any, admin: ModelAdmin, pk: str | None = None) -> Response:
    adding = pk is None
    data = request.state.form if request.method == "POST" else None
    save_as_new = bool(data is not None and "_saveasnew" in data and admin.save_as)
    obj = None if adding else admin.get_object(session, request, pk)

    if adding or save_as_new:
        admin.require(admin.has_add_permission(request))
        can_save = True
    else:
        can_save = admin.has_change_permission(request, obj)
        admin.require(can_save or admin.has_view_permission(request, obj))
        if data is not None:
            admin.require(can_save)

    creating = adding or save_as_new
    target = admin.model() if creating else obj
    inline_parent = None if creating else obj
    form = admin.get_form(session, request, target, persisted=not creating, view_only=not can_save)
    formsets = [
        FormSet(inline, session, request, inline_parent, persisted=not creating, view_only=not can_save)
        for inline in admin.get_inlines(request, inline_parent)
    ]

    if data is not None:
        admin.validate_form(session, form, data)
        for formset in formsets:
            formset.validate(data)
        if form.is_valid and all(fs.is_valid for fs in formsets):
            try:
                changed = form.apply(target)
                admin.save_model(session, request, target, not creating)
                session.flush()
                notes = [n for fs in formsets for n in fs.save(target)]
                session.flush()
                message = ""
                if not creating:
                    parts = [f"Changed {', '.join(changed).lower()}"] if changed else []
                    values = admin.logged_values(form)
                    message = ". ".join([*parts, *([values] if values else []), *notes])
                else:
                    message = ". ".join(notes)
                record(session, request, admin, target, "add" if creating else "change", message)
                session.commit()
            except DBAPIError:
                session.rollback()
                form.errors.append(DB_ERROR)
                # A failed insert can leave the new object half-attached, so start the page clean.
                if creating and target in session:
                    session.expunge(target)
            else:
                return _after_save(request, admin, target, data, creating)
        if not form.errors:
            form.errors.append("Please fix the errors below.")

    title = f"Add {admin.name.lower()}" if adding else object_label(obj)
    return admin.site.render(
        request,
        admin.form_template,
        {
            "admin": admin,
            "title": title,
            "crumbs": _crumbs(request, admin, (title, None)),
            "form": form,
            "formsets": formsets,
            "adding": adding,
            "can_save": can_save,
            "can_delete": not adding and admin.has_delete_permission(request, obj),
            "can_add": admin.has_add_permission(request),
            "save_as": admin.save_as and not adding and admin.has_add_permission(request),
            "save_on_top": admin.save_on_top,
            "delete_url": ""
            if adding
            else f"{_root(request)}/{admin.slug}/{_url_pk(obj)}/delete{_keep_filters(request)}",
            "history_url": ""
            if adding or not admin.site.audit_log
            else f"{_root(request)}/{admin.slug}/{_url_pk(obj)}/history",
            "view_on_site": None if adding else admin.view_on_site(obj),
            "object_actions": []
            if adding
            else [
                {
                    "url": f"{_root(request)}/{admin.slug}/{_url_pk(obj)}/action/{name}{_keep_filters(request)}",
                    "description": info["description"],
                    "confirm": info["confirm"],
                }
                for name, info in admin.get_object_actions(request, obj).items()
            ],
            "post_url": request.url.path + (f"?{request.url.query}" if request.url.query else ""),
        },
    )


def _after_save(request: Any, admin: ModelAdmin, obj: Any, data: Any, created: bool) -> Response:
    label = object_label(obj)
    if request.query_params.get("_popup"):
        return admin.site.render(
            request,
            "fastapi_panel/popup_done.html",
            {"title": "Saved", "new_id": str(getattr(obj, pk_keys(admin.model)[0])), "new_label": label},
        )
    verb = "added" if created else "changed"
    admin.message_user(request, f"The {admin.name.lower()} “{label}” was {verb} successfully.")
    if "_continue" in data or "_saveasnew" in data:
        return redirect(f"{_root(request)}/{admin.slug}/{_url_pk(obj)}/change{_keep_filters(request)}")
    if "_addanother" in data:
        return redirect(f"{_root(request)}/{admin.slug}/add{_keep_filters(request)}")
    return redirect(_list_url(request, admin))


def collect_deletion(session: Any, request: Any, admin: ModelAdmin, objects: list[Any]) -> dict[str, Any]:
    """What deleting `objects` would remove, and which rows stop it from happening.

    Follows ORM cascades and every foreign key in the model's metadata, so database cascades,
    many-to-many links and references with no relationship on this side are all covered.
    """
    trash = request.state.trash
    root = _root(request)
    plan: dict[str, Any] = {"roots": [], "summary": [], "blocked": [], "truncated": False}
    can_open = not trash and admin.has_view_permission(request)
    plan["roots"] = [
        {
            "label": object_label(obj),
            "url": f"{root}/{admin.slug}/{_url_pk(obj)}/change" if can_open else None,
            "groups": [],
        }
        for obj in objects[:MAX_RELATED_LISTED]
    ]
    if not objects or admin.soft_deletes(request):
        return plan

    registry = inspect(type(objects[0])).registry
    mapped = {
        m.local_table: m for m in registry.mappers if m.inherits is None or m.inherits.local_table is not m.local_table
    }
    seen = {inspect(obj).identity_key for obj in objects}
    blockers: dict[Any, tuple[type, Any]] = {}
    blocked_links: dict[str, int] = {}
    totals: dict[str, int] = {}
    link_names: set[str] = set()
    budget = MAX_DELETE_ROWS

    def spend(rows: list[Any]) -> list[Any]:
        nonlocal budget
        if len(rows) > budget:
            plan["truncated"] = True
            rows = rows[:budget]
        budget -= len(rows)
        return rows

    def load(stmt: Any) -> list[Any]:
        return spend(list(session.execute(stmt.limit(budget + 1)).all()))

    def group_name(cls: type) -> str:
        child = admin.site.admin_for_model(cls)
        return child.name_plural if child else pluralize(split_camel(cls.__name__))

    def describe(cls: type, rows: list[Any]) -> list[dict[str, Any]]:
        """Labels and links, showing only what this user can reach through the child's own admin."""
        child = admin.site.admin_for_model(cls)
        if child is None:
            return [{"label": object_label(obj), "url": None, "groups": []} for obj in rows]
        visible: set[Any] = set()
        if rows and child.has_view_permission(request):
            stmt = child.get_queryset(request).where(or_(*[pk_criterion(cls, pk_string(obj)) for obj in rows]))
            visible = {inspect(obj).identity_key for obj in session.scalars(stmt).unique()}
        return [
            {
                "label": object_label(obj),
                "url": f"{root}/{child.slug}/{_url_pk(obj)}/change",
                "groups": [],
            }
            if inspect(obj).identity_key in visible
            else {"label": f"Hidden {child.name.lower()}", "url": None, "groups": []}
            for obj in rows
        ]

    def link_row(table_row: Any, table: Any, own: Any) -> dict[str, Any]:
        for other in table.foreign_key_constraints:
            target = mapped.get(other.referred_table)
            if other is own or target is None:
                continue
            criteria = [el.column == table_row._mapping[el.parent] for el in other.elements]
            found = session.scalars(select(target.class_).where(and_(*criteria))).first()
            if found is not None:
                return describe(target.class_, [found])[0]
        return {"label": ", ".join(str(value) for value in table_row), "url": None, "groups": []}

    def add_rows(groups: list[Any], cls: type, rows: list[Any], depth: int, show: bool) -> None:
        fresh = []
        for obj in rows:
            key = inspect(obj).identity_key
            if key not in seen:
                seen.add(key)
                fresh.append(obj)
        fresh = spend(fresh)
        if not fresh:
            return
        name = group_name(cls)
        totals[name] = totals.get(name, 0) + len(fresh)
        shown = fresh[:MAX_RELATED_LISTED]
        described = describe(cls, shown) if show else []
        for index, obj in enumerate(fresh):
            below = walk(obj, depth - 1, show and index < MAX_RELATED_LISTED)
            if show and index < MAX_RELATED_LISTED:
                described[index]["groups"] = below
        if show:
            groups.append({"name": name, "count": len(fresh), "rows": described, "link": False})

    def walk(current: Any, depth: int, show: bool) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = []
        if depth <= 0:
            plan["truncated"] = True
            return groups
        mapper = inspect(current).mapper
        for rel in mapper.relationships:
            if rel.viewonly or not rel.cascade.delete:
                continue
            related = getattr(current, rel.key)
            rows = list(related) if rel.uselist else [related] if related is not None else []
            add_rows(groups, rel.mapper.class_, rows, depth, show)
        for table in mapper.local_table.metadata.tables.values():
            for fk in table.foreign_key_constraints:
                if fk.referred_table not in mapper.tables:
                    continue
                owner = mapped.get(table)
                # A joined-inheritance child's key points at its own parent row.
                if owner is not None and owner.common_parent(mapper) and all(c.primary_key for c in fk.columns):
                    continue
                rel = next(
                    (
                        r
                        for r in mapper.relationships
                        if not r.viewonly
                        and r.direction is not MANYTOONE
                        and {child for _, child in r.synchronize_pairs} == set(fk.columns)
                    ),
                    None,
                )
                if rel is not None and rel.cascade.delete:
                    continue  # Already walked above.
                ondelete = (fk.ondelete or "").upper()
                where = and_(
                    *[el.parent == getattr(current, mapper.get_property_by_column(el.column).key) for el in fk.elements]
                )
                if owner is None:
                    if ondelete in ("SET NULL", "SET DEFAULT"):
                        continue
                    rows = load(select(table).where(where))
                    if not rows:
                        continue
                    name = humanize(table.name)
                    if rel is None and ondelete != "CASCADE":
                        blocked_links[name] = blocked_links.get(name, 0) + len(rows)
                        continue
                    totals[name] = totals.get(name, 0) + len(rows)
                    link_names.add(name)
                    if show:
                        described = [link_row(row, table, fk) for row in rows[:MAX_RELATED_LISTED]]
                        groups.append({"name": name, "count": len(rows), "rows": described, "link": True})
                    continue
                if rel is not None and not rel.passive_deletes:
                    # The ORM clears the key itself, which only a nullable column allows.
                    if all(c.nullable for c in fk.columns):
                        continue
                    removed = False
                elif ondelete in ("SET NULL", "SET DEFAULT"):
                    continue
                else:
                    removed = ondelete == "CASCADE"
                found = [row[0] for row in session.execute(select(owner.class_).where(where).limit(budget + 1))]
                if removed:
                    add_rows(groups, owner.class_, found, depth, show)
                else:
                    for obj in spend(found):
                        blockers[inspect(obj).identity_key] = (owner.class_, obj)
        return groups

    # get_queryset answers for the deleted items page while on it, and children live on the normal lists.
    request.state.trash = False
    try:
        for index, obj in enumerate(objects):
            below = walk(obj, MAX_DELETE_DEPTH, index < MAX_RELATED_LISTED)
            if index < MAX_RELATED_LISTED:
                plan["roots"][index]["groups"] = below
        plan["summary"] = [{"name": name, "count": count, "link": name in link_names} for name, count in totals.items()]
        # A custom delete_model may deal with dependants or keep the row, so only the database can refuse it.
        if type(admin).delete_model is ModelAdmin.delete_model:
            by_class: dict[type, list[Any]] = {}
            for key, (cls, obj) in blockers.items():
                if key not in seen:  # Also being deleted, so it isn't in the way.
                    by_class.setdefault(cls, []).append(obj)
            plan["blocked"] = [
                {
                    "name": group_name(cls),
                    "count": len(rows),
                    "rows": describe(cls, rows[:MAX_RELATED_LISTED]),
                    "link": False,
                }
                for cls, rows in by_class.items()
            ] + [{"name": name, "count": count, "rows": [], "link": True} for name, count in blocked_links.items()]
    finally:
        request.state.trash = trash
    return plan


def delete_view(session: Any, request: Any, admin: ModelAdmin, pk: str) -> Response:
    obj = admin.get_object(session, request, pk)
    admin.require(admin.has_delete_permission(request, obj))
    label = object_label(obj)
    plan = collect_deletion(session, request, admin, [obj])
    delete_url = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if request.method == "POST":
        if plan["blocked"]:
            admin.message_user(request, f"“{label}” can't be deleted because other records depend on it.", "error")
            return redirect(delete_url)
        try:
            record(session, request, admin, obj, "delete")
            admin.delete_model(session, request, obj)
            if obj in session.deleted:
                admin.site.auth.forget_user(session, obj)
            session.commit()
        except DBAPIError:
            session.rollback()
            admin.message_user(
                request, f"“{label}” can't be deleted because other records still depend on it.", "error"
            )
            return redirect(f"{_root(request)}/{admin.slug}/{_url_pk(obj)}/change")
        note = " You can restore it from Deleted." if admin.soft_deletes(request) else ""
        admin.message_user(request, f"The {admin.name.lower()} “{label}” was deleted.{note}")
        return redirect(_list_url(request, admin))
    return admin.site.render(
        request,
        "fastapi_panel/delete.html",
        {
            "admin": admin,
            "title": f"Delete {label}?",
            "crumbs": _crumbs(
                request, admin, (label, f"{_root(request)}/{admin.slug}/{_url_pk(obj)}/change"), ("Delete", None)
            ),
            "plan": plan,
            "soft": admin.soft_deletes(request),
            "cancel_url": f"{_root(request)}/{admin.slug}/{_url_pk(obj)}/change",
            "post_url": delete_url,
            "hidden": [],
        },
    )


def delete_selected(session: Any, request: Any, admin: ModelAdmin, objects: list[Any]) -> Response | None:
    data = request.state.form
    allowed = [obj for obj in objects if admin.has_delete_permission(request, obj)]
    admin.require(len(allowed) == len(objects))
    plan = collect_deletion(session, request, admin, objects)
    back = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if data.get("_confirm") == "1":
        if plan["blocked"]:
            admin.message_user(request, "Nothing was deleted because other records depend on these.", "error")
            return redirect(back)
        for obj in objects:
            record(session, request, admin, obj, "delete")
            admin.delete_model(session, request, obj)
            if obj in session.deleted:
                admin.site.auth.forget_user(session, obj)
        session.flush()
        word = admin.name if len(objects) == 1 else admin.name_plural
        admin.message_user(request, f"Deleted {len(objects)} {word.lower()}.")
        return None
    hidden = [("action", "delete_selected"), ("_confirm", "1")]
    if data.get("select_across") == "1":
        hidden.append(("select_across", "1"))
    else:
        hidden += [("_selected", pk_string(obj)) for obj in objects]
    word = admin.name if len(objects) == 1 else admin.name_plural
    return admin.site.render(
        request,
        "fastapi_panel/delete.html",
        {
            "admin": admin,
            "title": f"Delete {len(objects)} {word.lower()}?",
            "crumbs": _crumbs(request, admin, ("Delete selected", None)),
            "plan": plan,
            "extra_count": max(0, len(objects) - MAX_RELATED_LISTED),
            "soft": admin.soft_deletes(request),
            "cancel_url": back,
            "post_url": back,
            "hidden": hidden,
        },
    )


def object_action(session: Any, request: Any, admin: ModelAdmin, pk: str) -> Response:
    obj = admin.get_object(session, request, pk)
    # Looked up again here, so a button hidden from someone can't be run by posting to its address.
    chosen = admin.get_object_actions(request, obj).get(request.path_params["name"])
    if chosen is None:
        raise HTTPException(404, "Not found.")
    back = redirect(f"{_root(request)}/{admin.slug}/{_url_pk(obj)}/change{_keep_filters(request)}")
    try:
        result = chosen["fn"](session, request, obj)
        record(session, request, admin, obj, "change", f"Ran “{chosen['description']}”")
        session.commit()
    except ValidationError as exc:
        session.rollback()
        admin.message_user(request, str(exc), "error")
        return back
    except DBAPIError:
        session.rollback()
        admin.message_user(request, DB_ERROR, "error")
        return back
    return result if isinstance(result, Response) else back


def history_view(session: Any, request: Any, admin: ModelAdmin, pk: str) -> Response:
    if not admin.site.audit_log:
        raise HTTPException(404, "Not found.")
    obj = admin.get_object(session, request, pk)
    admin.require(admin.has_view_permission(request, obj))
    stmt = (
        select(AdminLog)
        .where(AdminLog.model == admin.slug, AdminLog.object_id == pk_string(obj))
        .order_by(AdminLog.created_at.desc(), AdminLog.id.desc())
        .limit(200)
    )
    label = object_label(obj)
    return admin.site.render(
        request,
        "fastapi_panel/history.html",
        {
            "admin": admin,
            "title": f"History of {label}",
            "crumbs": _crumbs(
                request, admin, (label, f"{_root(request)}/{admin.slug}/{_url_pk(obj)}/change"), ("History", None)
            ),
            "entries": list(session.scalars(stmt)),
        },
    )


def row_count(session: Any, request: Any, admin: ModelAdmin) -> Response:
    """The list page's total under the same filters, polled by `live_updates`."""
    admin.require(admin.has_view_permission(request))
    return JSONResponse({"total": _count(session, _filtered_statement(request, admin))})


def autocomplete(session: Any, request: Any, admin: ModelAdmin) -> Response:
    admin.require(admin.has_view_permission(request))
    stmt = admin.get_queryset(request)
    query = request.query_params.get("q", "").strip()
    if query and admin.search_fields:
        stmt = admin.apply_search(stmt, query)
    for spec in admin.get_ordering(request):
        stmt = _order_by(stmt, admin.model, spec)
    key = pk_keys(admin.model)[0]
    results = [
        {"id": str(getattr(obj, key)), "text": object_label(obj)}
        for obj in session.scalars(stmt.limit(AUTOCOMPLETE_RESULTS))
    ]
    return JSONResponse({"results": results})
