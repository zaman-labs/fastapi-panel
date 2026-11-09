from __future__ import annotations

import csv
import datetime as dt
import decimal
import enum
import io
import json
from collections.abc import Callable, Iterable
from typing import Any

import pydantic
from markupsafe import Markup
from sqlalchemy import Boolean, DateTime, inspect, or_, select
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.orm import selectinload
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.responses import Response

from .filters import DateHierarchy, Filter, make_filter
from .forms import (
    SENSITIVE_NAME,
    Field,
    FileField,
    Form,
    ManyToManyField,
    PasswordField,
    ReadOnlyField,
    RelationField,
    RichTextField,
    ValidationError,
    fields_for_model,
)
from .introspect import (
    humanize,
    object_label,
    pk_criterion,
    pluralize,
    relationship_paths,
    resolve_attr,
    search_criterion,
    split_camel,
)
from .richtext import plain_text, safe_html

MAX_CELL_LENGTH = 120
MAX_LOGGED_VALUE = 200
GROUP_NOISE = {"models", "model", "db", "database", "schemas", "tables", "entities", "app", "src", "__main__"}


class ImproperlyConfigured(Exception):
    pass


def display(
    description: str | None = None,
    *,
    ordering: str | None = None,
    boolean: bool = False,
    empty_value: str | None = None,
) -> Callable[[Callable], Callable]:
    """Label a computed column. `ordering` is the attribute to sort by, like `customer__name`."""

    def decorate(fn: Callable) -> Callable:
        fn.description = description
        fn.ordering = ordering
        fn.boolean = boolean
        fn.empty_value = empty_value
        return fn

    return decorate


def action(
    description: str | None = None,
    *,
    permissions: Iterable[str] = ("change",),
    confirm: str | None = None,
) -> Callable[[Callable], Callable]:
    """Mark an action. In `actions` it's called as `fn(self, session, request, objects)`, and in
    `object_actions` as `fn(self, session, request, obj)`."""

    def decorate(fn: Callable) -> Callable:
        fn.description = description
        fn.permissions = tuple(permissions)
        fn.confirm = confirm
        return fn

    return decorate


def format_value(value: Any, empty: str = "-", boolean: bool = False) -> Any:
    if isinstance(value, Markup):
        return value
    if boolean or isinstance(value, bool):
        if value is None:
            return Markup('<span class="bool bool-none" title="Unknown">&ndash;</span>')
        if value:
            return Markup('<span class="bool bool-yes" title="Yes">&#10003;</span>')
        return Markup('<span class="bool bool-no" title="No">&#10005;</span>')
    plain = plain_value(value)
    if plain == "":
        return empty
    return plain if len(plain) <= MAX_CELL_LENGTH else plain[: MAX_CELL_LENGTH - 1] + "…"


def plain_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Markup):
        return value.striptags()
    if isinstance(value, dt.datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, enum.Enum):
        return humanize(value.name.lower())
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, (list, tuple, set)) or type(value).__name__ == "InstrumentedList":
        return ", ".join(plain_value(item) for item in value)
    if hasattr(value, "__mapper__"):
        return object_label(value)
    return str(value)


def csv_safe(text: str) -> str:
    # Spreadsheets run cells starting with these as formulas.
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


class FormOptions:
    """What ModelAdmin and inlines have in common: turning a model into a form."""

    model: type
    fields: Iterable[Any] | None = None
    exclude: Iterable[str] = ()
    readonly_fields: Iterable[str] = ()
    autocomplete_fields: Iterable[str] = ()
    raw_id_fields: Iterable[str] = ()
    filter_horizontal: Iterable[str] = ()
    prepopulated_fields: dict[str, Iterable[str]] = {}
    field_overrides: dict[str, Field] = {}
    form_schema: type[pydantic.BaseModel] | None = None
    empty_value_display = "-"
    # Fields whose old and new values go into the history. Everything else only logs the field's name.
    log_values: Iterable[str] = ()
    verbose_name: str | None = None
    verbose_name_plural: str | None = None

    site: Any

    @property
    def name(self) -> str:
        return self.verbose_name or split_camel(self.model.__name__)

    @property
    def name_plural(self) -> str:
        return self.verbose_name_plural or pluralize(self.name)

    def _available_fields(self) -> tuple[dict[str, Field], set[str]]:
        cached, hidden = fields_for_model(self.model)
        fields = dict(cached)
        for name, override in self.field_overrides.items():
            original = fields.get(name)
            if original is not None:
                override.attr = override.attr or original.attr
                override.initial = override.initial if override.initial is not None else original.initial
                if override.required is None:
                    override.required = original.required
                override.empty_value = original.empty_value
                override.keeps_default = original.keeps_default
                override.unique = original.unique
            fields[name] = override
        return fields, hidden

    def get_readonly_fields(self, request: Any, obj: Any = None) -> Iterable[str]:
        return self.readonly_fields

    def get_fields(self, request: Any, obj: Any = None) -> list[Any]:
        if self.fields is not None:
            return list(self.fields)
        available, hidden = self._available_fields()
        names = [n for n in available if n not in hidden and n not in self.exclude]
        names += [n for n in self.get_readonly_fields(request, obj) if n not in names]
        return names

    def build_form(
        self,
        session: Any,
        request: Any,
        obj: Any,
        *,
        persisted: bool,
        names: list[str],
        prefix: str = "",
        view_only: bool = False,
    ) -> Form:
        available, _ = self._available_fields()
        readonly = set(self.get_readonly_fields(request, obj))
        pk_attrs = {inspect(self.model).get_property_by_column(c).key for c in inspect(self.model).primary_key}
        fields = []
        for name in names:
            source = available.get(name)
            locked = (
                view_only
                or source is None
                or name in readonly
                or source.generated
                or (persisted and source.attr in pk_attrs)
            )
            if locked:
                field = ReadOnlyField(label=self.column_label(name)).bind(name, prefix)
                field.display = (
                    self.cell(obj, name, request) if obj is not None and persisted else self.empty_value_display
                )
                if isinstance(source, PasswordField):
                    field.display = PasswordField.masked
                value = self.field_value(obj, name) if obj is not None and persisted else None
                if isinstance(source, RichTextField) and value:
                    field.display = Markup('<div class="rich-content">{}</div>').format(safe_html(value))
            else:
                field = source.bind(name, prefix)
                if isinstance(field, (RelationField, ManyToManyField)):
                    if name in self.raw_id_fields:
                        field.mode = "raw"
                    elif name in self.autocomplete_fields:
                        field.mode = "autocomplete"
                    if name in self.filter_horizontal:
                        field.select_widget = "dual"
                field.prepare(session, request, self.site)
                source_names = self.prepopulated_fields.get(name)
                if source_names:
                    field.attrs = {**field.attrs, "data-prepopulate": ",".join(f"{prefix}{n}" for n in source_names)}
            fields.append(field)
        return Form(fields, obj, persisted)

    def clean(self, data: dict[str, Any], obj: Any = None) -> None:
        """Cross-field validation. Mutate `data` or raise ValidationError(message, field=...)."""

    def validate_form(self, session: Any, form: Form, data: Any) -> None:
        form.validate(data)
        if form.is_valid:
            form.check_unique(session, self.model)
        if form.is_valid:
            try:
                self.clean(form.cleaned, form.obj if form.persisted else None)
            except ValidationError as exc:
                form.add_error(exc)
        if form.is_valid and self.form_schema is not None:
            try:
                values = {k: v.filename if isinstance(v, UploadFile) else v for k, v in form.cleaned.items()}
                self.form_schema.model_validate(values)
            except pydantic.ValidationError as exc:
                for error in exc.errors():
                    name = str(error["loc"][0]) if error["loc"] else None
                    form.add_error(ValidationError(error["msg"], field=name))

    def logged_values(self, form: Form) -> str:
        def show(field: Field, value: Any) -> str:
            text = field.describe(value) or "(empty)"
            return text if len(text) <= MAX_LOGGED_VALUE else text[: MAX_LOGGED_VALUE - 1] + "…"

        return "; ".join(
            f"{field.label}: {show(field, before)} → {show(field, after)}"
            for field, before, after in form.diffs
            if field.name in self.log_values
        )

    def field_value(self, obj: Any, name: Any) -> Any:
        if callable(name):
            return name(obj)
        if name == "__str__":
            return object_label(obj)
        if name in inspect(type(obj)).attrs:
            return getattr(obj, name)
        method = getattr(self, name, None)
        if callable(method):
            return method(obj)
        value = obj
        for part in name.split("__"):
            value = getattr(value, part, None)
            if value is None:
                return None
        return value() if callable(value) else value

    def _display_attribute(self, name: Any, attribute: str) -> Any:
        if callable(name):
            return getattr(name, attribute, None)
        if name == "__str__" or name in inspect(self.model).attrs:
            return None
        target = getattr(self, name, None) or getattr(self.model, name, None)
        return getattr(target, attribute, None)

    def column_label(self, name: Any) -> str:
        if name == "__str__":
            return self.name
        description = self._display_attribute(name, "description")
        if description:
            return description
        return humanize(name if isinstance(name, str) else getattr(name, "__name__", ""))

    def display_value(self, obj: Any, name: Any) -> Any:
        value = self.field_value(obj, name)
        return self.site.to_local(value) if isinstance(value, dt.datetime) else value

    def cell(self, obj: Any, name: Any, request: Any) -> Any:
        value = self.display_value(obj, name)
        field = self._available_fields()[0].get(name) if isinstance(name, str) else None
        if isinstance(field, FileField) and value:
            return field.render(self.site, request, value)
        if isinstance(field, RichTextField) and value:
            value = plain_text(value)
        return format_value(
            value,
            self._display_attribute(name, "empty_value") or self.empty_value_display,
            bool(self._display_attribute(name, "boolean")),
        )


class InlineAdmin(FormOptions):
    """Edit related rows on the parent's page. Subclass TabularInline or StackedInline."""

    template = ""
    fk_name: str | None = None
    extra = 1
    min_num = 0
    max_num: int | None = None
    can_delete = True
    ordering: Iterable[str] = ()

    def __init__(self, parent: ModelAdmin) -> None:
        self.parent = parent
        self.site = parent.site

    @property
    def prefix(self) -> str:
        return self.model.__name__.lower()

    def foreign_key(self) -> tuple[str, str]:
        """(attribute on this model, attribute on the parent) linking the two."""
        mapper = inspect(self.model)
        parent_table = inspect(self.parent.model).local_table
        matches = []
        for prop in mapper.column_attrs:
            for fk in prop.columns[0].foreign_keys:
                if fk.column.table is parent_table and self.fk_name in (None, prop.key):
                    matches.append((prop.key, fk.column))
        if len(matches) != 1:
            raise ImproperlyConfigured(
                f"{type(self).__name__}: expected one foreign key from {self.model.__name__} to "
                f"{self.parent.model.__name__}, found {len(matches)}. Set fk_name."
            )
        key, column = matches[0]
        return key, inspect(self.parent.model).get_property_by_column(column).key

    def get_fields(self, request: Any, obj: Any = None) -> list[Any]:
        names = super().get_fields(request, obj)
        fk, _ = self.foreign_key()
        available, _ = self._available_fields()
        skip = {n for n, f in available.items() if f.attr == fk}
        return [n for n in names if n not in skip]

    def get_queryset(self, request: Any) -> Any:
        stmt = select(self.model)
        return stmt.order_by(*[_order_expression(self.model, o) for o in self.ordering]) if self.ordering else stmt

    def has_add_permission(self, request: Any) -> bool:
        return self.parent.has_change_permission(request)

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return self.parent.has_change_permission(request)

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return self.can_delete and self.parent.has_change_permission(request)

    def save_model(self, session: Any, request: Any, obj: Any, change: bool) -> None:
        session.add(obj)


class TabularInline(InlineAdmin):
    template = "tabular"


class StackedInline(InlineAdmin):
    template = "stacked"


def _order_expression(model: type, spec: str) -> Any:
    descending = spec.startswith("-")
    column = resolve_attr(model, spec.lstrip("-"))
    return column.desc() if descending else column.asc()


class ModelAdmin(FormOptions):
    # Changelist
    list_display: Iterable[Any] = ("__str__",)
    list_display_links: Iterable[Any] | None = None
    list_editable: Iterable[str] = ()
    list_filter: Iterable[Any] = ()
    list_per_page = 50
    search_fields: Iterable[str] = ()
    search_help_text = ""
    ordering: Iterable[str] = ()
    date_hierarchy: str | None = None
    show_full_result_count = True
    # Seconds between checks for new rows while the list is open. None turns it off.
    live_updates: int | None = None

    # Actions
    actions: Iterable[Any] = ()
    builtin_actions: Iterable[str] = ("edit_selected", "delete_selected", "export_csv")
    action_max_rows = 10_000
    # Buttons on one record's page. Each runs an @action method that takes a single object.
    object_actions: Iterable[str] = ()
    export_fields: Iterable[Any] | None = None
    csv_import = False
    import_fields: Iterable[str] | None = None
    import_max_rows = 5_000

    # Form page
    fieldsets: Iterable[tuple[str | None, dict[str, Any]]] | None = None
    inlines: Iterable[type[InlineAdmin]] = ()
    save_as = False
    save_on_top = False

    # A column that marks a row as deleted: a nullable datetime or a boolean. Deletes then only set it.
    soft_delete_field: str | None = None

    # Navigation
    group: str | None = None
    slug: str
    list_template = "fastapi_panel/list.html"
    form_template = "fastapi_panel/form.html"

    def __init__(self, model: type, site: Any) -> None:
        self.model = model
        self.site = site
        self.slug = getattr(type(self), "slug", model.__name__.lower())
        self.group_name = self.group or self._group_from_module()

    def _group_from_module(self) -> str:
        parts = [p for p in self.model.__module__.split(".") if p not in GROUP_NOISE]
        return humanize(parts[-1]) if parts else "Models"

    # Permissions. Override any of these. Row-level rules can look at `obj`.

    def _granted(self, request: Any, action_name: str) -> bool:
        return self.site.auth.has_perm(request.state.user, f"{self.slug}.{action_name}")

    def has_module_permission(self, request: Any) -> bool:
        return any(
            check(request) for check in (self.has_view_permission, self.has_add_permission, self.has_change_permission)
        )

    def has_view_permission(self, request: Any, obj: Any = None) -> bool:
        return self._granted(request, "view") or self.has_change_permission(request, obj)

    def has_add_permission(self, request: Any) -> bool:
        return self._granted(request, "add")

    def has_change_permission(self, request: Any, obj: Any = None) -> bool:
        return self._granted(request, "change")

    def has_delete_permission(self, request: Any, obj: Any = None) -> bool:
        return self._granted(request, "delete")

    def require(self, allowed: bool) -> None:
        if not allowed:
            raise HTTPException(403, "You don't have permission to do that.")

    # Querying

    def get_queryset(self, request: Any) -> Any:
        """Rows this request may reach. Every view goes through it, so it's the place for row-level scoping."""
        stmt = select(self.model)
        if not self.soft_delete_field:
            return stmt
        column = getattr(self.model, self.soft_delete_field)
        if isinstance(column.type, Boolean):
            return stmt.where(column.is_(True) if request.state.trash else column.is_not(True))
        return stmt.where(column.is_not(None) if request.state.trash else column.is_(None))

    def get_object(self, session: Any, request: Any, pk: str) -> Any:
        try:
            criterion = pk_criterion(self.model, pk)
        except ValueError:
            raise HTTPException(404, "Not found.") from None
        obj = session.scalars(self.get_queryset(request).where(criterion)).first()
        if obj is None:
            raise HTTPException(404, f"This {self.name.lower()} doesn't exist or you can't see it.")
        return obj

    def get_ordering(self, request: Any) -> list[str]:
        return list(self.ordering)

    def get_list_display(self, request: Any) -> list[Any]:
        return list(self.list_display)

    def get_list_filters(self, request: Any) -> list[Filter]:
        return [make_filter(self.model, spec, self.site) for spec in self.list_filter]

    def date_hierarchy_helper(self) -> DateHierarchy | None:
        return DateHierarchy(self.model, self.date_hierarchy) if self.date_hierarchy else None

    def apply_search(self, stmt: Any, query: str) -> Any:
        for term in query.split()[:10]:
            stmt = stmt.where(or_(*[search_criterion(self.model, spec, term) for spec in self.search_fields]))
        return stmt

    def eager_load(self, stmt: Any, request: Any) -> Any:
        names = [n for n in self.get_list_display(request) if isinstance(n, str)]
        for path in relationship_paths(self.model, names):
            current, option = self.model, None
            for part in path.split("__"):
                attr = getattr(current, part)
                option = option.selectinload(attr) if option is not None else selectinload(attr)
                current = attr.property.mapper.class_
            stmt = stmt.options(option)
        return stmt

    def sort_expression(self, name: Any) -> str | None:
        """The attribute path a list_display column sorts by, or None if it can't sort."""
        ordering = self._display_attribute(name, "ordering")
        if ordering:
            return ordering
        if isinstance(name, str) and name in inspect(self.model).column_attrs:
            return name
        return None

    # Forms

    def get_fields(self, request: Any, obj: Any = None) -> list[Any]:
        return [name for name in super().get_fields(request, obj) if name != self.soft_delete_field]

    def _password_name(self) -> str | None:
        """The password column, when this admin edits the users the panel logs in with."""
        auth = getattr(self.site, "auth", None)
        if auth is None or getattr(auth, "model", None) is not self.model or not auth.can_change_password:
            return None
        name = auth.password_field
        return None if name in self.exclude or name in self.field_overrides else name

    def _available_fields(self) -> tuple[dict[str, Field], set[str]]:
        fields, hidden = super()._available_fields()
        name = self._password_name()
        if name:
            fields[name] = PasswordField(label="Password")
        return fields, hidden

    def get_fieldsets(self, request: Any, obj: Any = None) -> list[tuple[str | None, dict[str, Any]]]:
        if self.fieldsets is not None:
            found = list(self.fieldsets)
        else:
            found = [(None, {"fields": self.get_fields(request, obj)})]
        name = self._password_name()
        if name is None:
            return found
        listed = {
            part
            for _, options in found
            for row in options["fields"]
            for part in (row if isinstance(row, (tuple, list)) else [row])
        }
        # The add page passes a new, unsaved object.
        existing = obj is not None and inspect(obj).persistent
        may_set = self._may_replace_password(request, obj) if existing else self.has_add_permission(request)
        if name not in listed and may_set:
            found.append(("Password", {"fields": (name,)}))
        return found

    def _may_replace_password(self, request: Any, obj: Any) -> bool:
        auth = self.site.auth
        allowed = auth.superuser_field is None or auth.is_superuser(request.state.user)
        return allowed and self.has_change_permission(request, obj)

    def get_inlines(self, request: Any, obj: Any = None) -> list[InlineAdmin]:
        return [inline(self) for inline in self.inlines]

    def get_form(self, session: Any, request: Any, obj: Any, *, persisted: bool, view_only: bool = False) -> Form:
        fieldsets = self.get_fieldsets(request, obj)
        names: list[str] = []
        for _, options in fieldsets:
            for row in options["fields"]:
                names.extend(row if isinstance(row, (tuple, list)) else [row])
        form = self.build_form(session, request, obj, persisted=persisted, names=names, view_only=view_only)
        by_name = {field.name: field for field in form.fields}
        for title, options in fieldsets:
            rows = [
                [by_name[n] for n in (row if isinstance(row, (tuple, list)) else [row])] for row in options["fields"]
            ]
            form.fieldsets.append(
                {
                    "title": title,
                    "description": options.get("description", ""),
                    "collapsed": "collapse" in options.get("classes", ()),
                    "rows": rows,
                }
            )
        return form

    def get_list_form(self, session: Any, request: Any, obj: Any, prefix: str) -> Form:
        return self.build_form(session, request, obj, persisted=True, names=list(self.list_editable), prefix=prefix)

    def get_import_fields(self, request: Any) -> list[str]:
        if self.import_fields is not None:
            return list(self.import_fields)
        return self._writable_fields(request, skip=(FileField,))

    def get_bulk_edit_fields(self, request: Any) -> list[str]:
        """The fields "Edit selected" offers. Passwords and files are left out, they belong to one record."""
        return self._writable_fields(request, skip=(FileField, PasswordField))

    def _writable_fields(self, request: Any, skip: tuple[type, ...]) -> list[str]:
        readonly = set(self.get_readonly_fields(request))
        names: list[str] = []
        for row in self.get_fields(request):
            names.extend(row if isinstance(row, (tuple, list)) else [row])
        available, _ = self._available_fields()
        return [name for name in names if name not in readonly and not isinstance(available.get(name), skip)]

    def can_import(self, request: Any) -> bool:
        return self.csv_import and (self.has_add_permission(request) or self.has_change_permission(request))

    def view_on_site(self, obj: Any) -> str | None:
        """Return the public URL of `obj` to get a "View on site" button."""
        return None

    # Saving

    def save_model(self, session: Any, request: Any, obj: Any, change: bool) -> None:
        session.add(obj)

    def delete_model(self, session: Any, request: Any, obj: Any) -> None:
        if not self.soft_deletes(request):
            session.delete(obj)
            return
        column = getattr(self.model, self.soft_delete_field)
        if isinstance(column.type, Boolean):
            setattr(obj, self.soft_delete_field, True)
        else:
            now = dt.datetime.now(dt.timezone.utc)
            setattr(obj, self.soft_delete_field, now if column.type.timezone else now.replace(tzinfo=None))

    def restore_model(self, session: Any, request: Any, obj: Any) -> None:
        column = getattr(self.model, self.soft_delete_field)
        setattr(obj, self.soft_delete_field, False if isinstance(column.type, Boolean) else None)

    def soft_deletes(self, request: Any) -> bool:
        """Whether deleting right now keeps the row. On the deleted items page, deleting is for good."""
        return bool(self.soft_delete_field) and not request.state.trash

    def can_see_deleted(self, request: Any) -> bool:
        if not self.soft_delete_field:
            return False
        return self.has_change_permission(request) or self.has_delete_permission(request)

    def message_user(self, request: Any, text: str, level: str = "success") -> None:
        self.site.message(request, text, level)

    # Actions

    def get_actions(self, request: Any) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        specs = (
            ("restore_selected", "delete_selected") if request.state.trash else (*self.actions, *self.builtin_actions)
        )
        for spec in specs:
            name = spec if isinstance(spec, str) else spec.__name__
            fn = getattr(self, spec) if isinstance(spec, str) else (lambda *a, _f=spec: _f(self, *a))
            source = getattr(type(self), spec) if isinstance(spec, str) else spec
            needed = getattr(source, "permissions", ("change",))
            if not all(getattr(self, f"has_{perm}_permission")(request) for perm in needed):
                continue
            found[name] = {
                "fn": fn,
                "description": getattr(source, "description", None) or humanize(name),
                "confirm": getattr(source, "confirm", None),
            }
        if request.state.trash and "delete_selected" in found:
            found["delete_selected"]["description"] = "Delete selected permanently"
        return found

    def get_object_actions(self, request: Any, obj: Any) -> dict[str, dict[str, Any]]:
        """The buttons for this record. Override to offer an action only on some records."""
        found: dict[str, dict[str, Any]] = {}
        for name in self.object_actions:
            source = getattr(type(self), name)
            needed = getattr(source, "permissions", ("change",))
            if all(getattr(self, f"has_{perm}_permission")(request, obj) for perm in needed):
                found[name] = {
                    "fn": getattr(self, name),
                    "description": getattr(source, "description", None) or humanize(name),
                    "confirm": getattr(source, "confirm", None),
                }
        return found

    @action("Delete selected", permissions=("delete",))
    def delete_selected(self, session: Any, request: Any, objects: list[Any]) -> Response | None:
        from . import views

        return views.delete_selected(session, request, self, objects)

    @action("Edit selected", permissions=("change",))
    def edit_selected(self, session: Any, request: Any, objects: list[Any]) -> Response | None:
        from . import views

        return views.edit_selected(session, request, self, objects)

    @action("Restore selected", permissions=("change",))
    def restore_selected(self, session: Any, request: Any, objects: list[Any]) -> None:
        from . import views

        views.restore_objects(session, request, self, objects)

    @action("Export selected to CSV", permissions=("view",))
    def export_csv(self, session: Any, request: Any, objects: list[Any]) -> Response:
        columns = list(self.export_fields or self.get_list_display(request))
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([self.column_label(c) for c in columns])
        for obj in objects:
            writer.writerow([csv_safe(plain_value(self.display_value(obj, c))) for c in columns])
        filename = f"{self.slug}.csv"
        return Response(
            buffer.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # Extra pages under /<slug>/. Return (path, view, methods) tuples; views take (session, request).

    def get_routes(self) -> list[tuple[str, Callable[[Any, Any], Response], list[str]]]:
        return []

    # Startup checks, so a typo fails on boot rather than on the first page view.

    def check(self) -> None:
        def fail(option: str, value: Any) -> None:
            raise ImproperlyConfigured(
                f"{type(self).__name__}.{option}: {value!r} is not valid for {self.model.__name__}"
            )

        try:
            attrs = inspect(self.model).attrs
        except NoInspectionAvailable:
            raise ImproperlyConfigured(f"{self.model!r} is not a SQLAlchemy mapped class") from None

        if self.live_updates is not None and (type(self.live_updates) is not int or self.live_updates < 1):
            fail("live_updates", self.live_updates)

        def known(name: Any) -> bool:
            return (
                callable(name)
                or name == "__str__"
                or name in attrs
                or hasattr(self, name)
                or _has_path(self.model, name)
            )

        for name in self.list_display:
            if not known(name):
                fail("list_display", name)
        for name in self.list_editable:
            if name not in self.list_display:
                fail("list_editable", name)
        for spec in self.list_filter:
            try:
                make_filter(self.model, spec, self.site)
            except AttributeError:
                fail("list_filter", spec)
        for spec in self.search_fields:
            if not _has_path(self.model, spec.lstrip("^=")):
                fail("search_fields", spec)
        for spec in self.ordering:
            if not _has_path(self.model, spec.lstrip("-")):
                fail("ordering", spec)
        if self.date_hierarchy and self.date_hierarchy not in attrs:
            fail("date_hierarchy", self.date_hierarchy)
        if self.soft_delete_field:
            column = inspect(self.model).column_attrs.get(self.soft_delete_field)
            kind = column.columns[0] if column else None
            usable = kind is not None and (
                isinstance(kind.type, Boolean) or (isinstance(kind.type, DateTime) and kind.nullable)
            )
            if not usable:
                fail("soft_delete_field", self.soft_delete_field)
        available, _ = self._available_fields()
        for spec in (*self.actions, *self.builtin_actions):
            if isinstance(spec, str) and not callable(getattr(self, spec, None)):
                fail("actions", spec)
        for name in self.object_actions:
            if not callable(getattr(self, name, None)):
                fail("object_actions", name)
        for inline_class in self.inlines:
            inline_class(self).foreign_key()
        for options in (self, *[inline_class(self) for inline_class in self.inlines]):
            for name in options.log_values:
                field = options._available_fields()[0].get(name)
                if field is None or SENSITIVE_NAME.search(name) or SENSITIVE_NAME.search(field.attr or ""):
                    fail("log_values", name)
            if not self.site.storage and any(isinstance(f, FileField) for f in options._available_fields()[0].values()):
                raise ImproperlyConfigured(
                    f"{type(options).__name__} has a file field, so the site needs a storage: "
                    'AdminSite(storage=LocalStorage("media")).'
                )
        declared = self.fields or []
        for name in declared:
            for part in name if isinstance(name, (tuple, list)) else [name]:
                if part not in available and not known(part):
                    fail("fields", part)


def _has_path(model: type, path: str) -> bool:
    try:
        resolve_attr(model, path)
    except AttributeError:
        return False
    return True
