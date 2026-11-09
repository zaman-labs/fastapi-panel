from __future__ import annotations

import copy
import datetime as dt
import decimal
import enum
import functools
import hashlib
import json
import math
import re
import secrets
import uuid
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import Any

import sqlalchemy as sa
from markupsafe import Markup
from sqlalchemy import inspect, select
from sqlalchemy.orm import MANYTOMANY, MANYTOONE
from starlette.datastructures import UploadFile

from .introspect import coerce_to_column, humanize, object_label
from .richtext import plain_text, sanitize_html
from .security import MIN_PASSWORD_LENGTH

SENSITIVE_NAME = re.compile(r"password|passwd|secret|token|api_?key|private_?key", re.I)
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RELATION_SELECT_LIMIT = 200
# Records a CSV import looks through for a name. More than one exact match is refused as ambiguous anyway.
LABEL_SEARCH_LIMIT = 20
FINGERPRINT_LENGTH = 32
IMAGE_SIGNATURES: dict[str, Callable[[bytes], bool]] = {
    "png": lambda head: head.startswith(b"\x89PNG\r\n\x1a\n"),
    "jpg": lambda head: head.startswith(b"\xff\xd8\xff"),
    "jpeg": lambda head: head.startswith(b"\xff\xd8\xff"),
    "gif": lambda head: head[:6] in (b"GIF87a", b"GIF89a"),
    "webp": lambda head: head[:4] == b"RIFF" and head[8:12] == b"WEBP",
}


def _extension(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).suffix.lstrip(".").lower()


def _size_label(size: int) -> str:
    return f"{size / (1024 * 1024):g} MB" if size >= 1024 * 1024 else f"{size // 1024} KB"


class ValidationError(Exception):
    """Raise from `ModelAdmin.clean` or a validator. `field=None` makes it a form-wide error."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


class Field:
    widget = "input"
    input_type = "text"

    def __init__(
        self,
        *,
        label: str | None = None,
        required: bool | None = None,
        help_text: str = "",
        initial: Any = None,
        validators: Iterable[Callable[[Any], None]] = (),
        attrs: dict[str, Any] | None = None,
        widget: str | None = None,
    ) -> None:
        self.label = label
        self.required = required
        self.help_text = help_text
        self.initial = initial
        self.validators = list(validators)
        self.attrs = attrs or {}
        if widget:
            self.widget = widget
        self.attr: str | None = None
        self.empty_value: Any = None
        self.keeps_default = False
        self.unique = False
        self.generated = False

    def bind(self, name: str, prefix: str = "") -> Field:
        bound = copy.copy(self)
        bound.name = name
        bound.attr = self.attr or name
        bound.html_name = f"{prefix}{name}"
        bound.label = self.label or humanize(name)
        bound.required = bool(self.required)
        bound.errors = []
        bound.value = ""
        bound.choices = list(getattr(self, "choices", []))
        return bound

    def prepare(self, session: Any, request: Any, site: Any) -> None:
        pass

    def read(self, obj: Any) -> Any:
        return getattr(obj, self.attr)

    def write(self, obj: Any, value: Any) -> None:
        setattr(obj, self.attr, value)

    def format(self, value: Any) -> Any:
        return "" if value is None else str(value)

    def extract(self, data: Any) -> Any:
        return data.get(self.html_name, "")

    def to_python(self, raw: str) -> Any:
        return raw

    def describe(self, formatted: Any) -> str:
        """How a value reads in the history, when `log_values` asks to keep it."""
        labels = dict(getattr(self, "choices", []))
        values = formatted if isinstance(formatted, list) else [formatted]
        return ", ".join(str(labels.get(value, value)) for value in values)

    def clean(self, raw: Any) -> Any:
        if isinstance(raw, str):
            raw = raw.strip()
        if raw in ("", None, []):
            if self.required:
                raise ValidationError("This field is required.")
            return self.empty_value
        value = self.to_python(raw)
        for validate in self.validators:
            validate(value)
        return value


class CharField(Field):
    def __init__(self, *, max_length: int | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_length = max_length
        if max_length:
            self.attrs = {"maxlength": max_length, **self.attrs}

    def to_python(self, raw: str) -> str:
        if self.max_length and len(raw) > self.max_length:
            raise ValidationError(f"Use at most {self.max_length} characters.")
        return raw


class TextField(CharField):
    widget = "textarea"


class RichTextField(TextField):
    """Formatted text, stored as HTML. Only a small set of tags survives, whatever gets submitted."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.attrs = {"data-richtext": "", **self.attrs}

    def format(self, value: Any) -> str:
        return sanitize_html(value) if value else ""

    def extract(self, data: Any) -> str:
        return self.format(data.get(self.html_name, ""))

    def clean(self, raw: Any) -> Any:
        # An editor left empty still holds markup such as <br>.
        return super().clean(raw if plain_text(raw).strip() else "")

    def describe(self, formatted: Any) -> str:
        return plain_text(formatted)


class EmailField(CharField):
    input_type = "email"

    def to_python(self, raw: str) -> str:
        if not EMAIL.match(raw):
            raise ValidationError("Enter a valid email address.")
        return super().to_python(raw)


class _NumberField(Field):
    input_type = "number"
    error = "Enter a number."

    def __init__(self, *, min_value: Any = None, max_value: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.min_value = min_value
        self.max_value = max_value
        limits = {"min": min_value, "max": max_value}
        self.attrs = {**{k: v for k, v in limits.items() if v is not None}, **self.attrs}

    def convert(self, raw: str) -> Any:
        raise NotImplementedError

    def to_python(self, raw: str) -> Any:
        try:
            value = self.convert(raw)
        except (ValueError, decimal.InvalidOperation):
            raise ValidationError(self.error) from None
        if self.min_value is not None and value < self.min_value:
            raise ValidationError(f"Must be at least {self.min_value}.")
        if self.max_value is not None and value > self.max_value:
            raise ValidationError(f"Must be at most {self.max_value}.")
        return value


class IntegerField(_NumberField):
    error = "Enter a whole number."
    convert = staticmethod(int)


class FloatField(_NumberField):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.attrs.setdefault("step", "any")

    def convert(self, raw: str) -> float:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(raw)
        return value


class DecimalField(_NumberField):
    def __init__(self, *, max_digits: int | None = None, decimal_places: int | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.max_digits = max_digits
        self.decimal_places = decimal_places
        self.attrs.setdefault("step", "any" if decimal_places is None else str(10**-decimal_places))

    def convert(self, raw: str) -> decimal.Decimal:
        value = decimal.Decimal(raw)
        if not value.is_finite():
            raise ValueError(raw)
        _, digits, exponent = value.as_tuple()
        places = max(-exponent, 0)
        if self.decimal_places is not None and places > self.decimal_places:
            raise ValidationError(f"Use at most {self.decimal_places} decimal places.")
        if self.max_digits is not None and max(len(digits), places) > self.max_digits:
            raise ValidationError(f"Use at most {self.max_digits} digits.")
        return value


class BooleanField(Field):
    widget = "checkbox"

    def format(self, value: Any) -> str:
        return "on" if value else ""

    def describe(self, formatted: Any) -> str:
        return "Yes" if formatted else "No"

    def clean(self, raw: Any) -> bool:
        return bool(raw)


class DateField(Field):
    input_type = "date"

    def to_python(self, raw: str) -> dt.date:
        try:
            return dt.date.fromisoformat(raw)
        except ValueError:
            raise ValidationError("Enter a valid date.") from None


class TimeField(Field):
    input_type = "time"

    def to_python(self, raw: str) -> dt.time:
        try:
            return dt.time.fromisoformat(raw)
        except ValueError:
            raise ValidationError("Enter a valid time.") from None


class DateTimeField(Field):
    input_type = "datetime-local"

    def __init__(self, *, aware: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.aware = aware
        self.site: Any = None
        self.attrs.setdefault("step", "1")

    def prepare(self, session: Any, request: Any, site: Any) -> None:
        self.site = site
        converted = site.naive_utc and site.timezone != "UTC"
        if not self.help_text and (self.aware or converted):
            self.help_text = f"In {site.timezone}."

    def format(self, value: Any) -> str:
        if value is None:
            return ""
        return self.site.to_local(value).replace(tzinfo=None).isoformat(timespec="seconds")

    def to_python(self, raw: str) -> dt.datetime:
        try:
            value = dt.datetime.fromisoformat(raw)
        except ValueError:
            raise ValidationError("Enter a valid date and time.") from None
        return self.site.from_local(value.replace(tzinfo=None), self.aware)


class UUIDField(Field):
    def to_python(self, raw: str) -> uuid.UUID:
        try:
            return uuid.UUID(raw)
        except ValueError:
            raise ValidationError("Enter a valid UUID.") from None


class JSONField(Field):
    widget = "textarea"

    def format(self, value: Any) -> str:
        return "" if value is None else json.dumps(value, indent=2, ensure_ascii=False, default=str)

    def to_python(self, raw: str) -> Any:
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ValidationError(f"Enter valid JSON: {exc}") from None


class ChoiceField(Field):
    widget = "select"

    def __init__(self, *, choices: Iterable[tuple[Any, str]] = (), **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.choices = [(str(value), label) for value, label in choices]
        self._values = {str(value): value for value, _ in choices}

    def to_python(self, raw: str) -> Any:
        if raw not in self._values:
            raise ValidationError("Select a valid choice.")
        return self._values[raw]


class EnumField(ChoiceField):
    def __init__(self, enum_class: type[enum.Enum], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.choices = [(m.name, humanize(m.name.lower())) for m in enum_class]
        self._values = {m.name: m for m in enum_class}

    def format(self, value: Any) -> str:
        return "" if value is None else value.name


class FileField(Field):
    """A column holding the stored file's name. Needs a storage: `AdminSite(storage=LocalStorage(...))`."""

    widget = "file"
    allowed = ("pdf", "txt", "csv", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "zip", *IMAGE_SIGNATURES)
    current = ""

    def __init__(
        self,
        *,
        allowed_extensions: Iterable[str] | None = None,
        max_size: int = 5 * 1024 * 1024,
        upload_to: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.allowed_extensions = {e.lower().lstrip(".") for e in allowed_extensions or self.allowed}
        self.max_size = max_size
        self.upload_to = upload_to
        self.site: Any = None
        self.request: Any = None
        accept = ",".join(f".{e}" for e in sorted(self.allowed_extensions))
        self.attrs = {"accept": accept, "data-max-size": max_size, **self.attrs}
        self.help_text = self.help_text or f"Up to {_size_label(max_size)}."

    def prepare(self, session: Any, request: Any, site: Any) -> None:
        self.site = site
        self.request = request

    def read(self, obj: Any) -> Any:
        self.current = getattr(obj, self.attr) or ""
        return self.current

    def format(self, value: Any) -> str:
        if isinstance(value, UploadFile):
            return value.filename or ""
        return value or ""

    def extract(self, data: Any) -> Any:
        upload = data.get(self.html_name)
        if isinstance(upload, UploadFile) and upload.filename:
            return upload
        return "" if data.get(f"{self.html_name}-clear") else self.current

    def clean(self, raw: Any) -> Any:
        if isinstance(raw, UploadFile):
            self.check_upload(raw)
            return raw
        if not raw and self.required:
            raise ValidationError("Choose a file.")
        return raw or self.empty_value

    def check_upload(self, upload: UploadFile) -> None:
        extension = _extension(upload.filename or "")
        if extension not in self.allowed_extensions:
            raise ValidationError(f"Use one of these file types: {', '.join(sorted(self.allowed_extensions))}.")
        if upload.size is not None and upload.size > self.max_size:
            raise ValidationError(f"This file is too large. The limit is {_size_label(self.max_size)}.")
        for validate in self.validators:
            validate(upload)

    def write(self, obj: Any, value: Any) -> None:
        if isinstance(value, UploadFile):
            folder = self.upload_to or type(obj).__name__.lower()
            stem = re.sub(r"[^a-z0-9]+", "-", PurePosixPath(value.filename.replace("\\", "/")).stem.lower()).strip("-")
            # shortcut: replaced and deleted files stay in storage, a save can still roll back and restores need them
            name = f"{folder}/{stem[:40] or 'file'}-{secrets.token_hex(6)}.{_extension(value.filename)}"
            value.file.seek(0)
            self.site.storage.save(value.file, name)
            value = name
        setattr(obj, self.attr, value)

    @property
    def current_name(self) -> str:
        return PurePosixPath(self.current).name

    @property
    def current_url(self) -> str:
        return self.site.file_url(self.request, self.current)

    @property
    def is_image(self) -> bool:
        return _extension(self.current) in IMAGE_SIGNATURES

    def render(self, site: Any, request: Any, value: str) -> Markup:
        url = site.file_url(request, value)
        if _extension(value) in IMAGE_SIGNATURES:
            return Markup('<a href="{}"><img class="thumb" src="{}" alt="" loading="lazy"></a>').format(url, url)
        return Markup('<a href="{}">{}</a>').format(url, PurePosixPath(value).name)


class ImageField(FileField):
    """Only PNG, JPEG, GIF and WEBP, checked against the file's contents. SVG can run scripts, so it isn't allowed."""

    allowed = tuple(IMAGE_SIGNATURES)

    def check_upload(self, upload: UploadFile) -> None:
        super().check_upload(upload)
        head = upload.file.read(16)
        upload.file.seek(0)
        if not IMAGE_SIGNATURES.get(_extension(upload.filename or ""), lambda _: False)(head):
            raise ValidationError("This file isn't a valid image.")


class PasswordField(Field):
    """Sets a user's password through the auth backend. The stored hash never reaches the page."""

    input_type = "password"
    masked = "••••••••"

    def prepare(self, session: Any, request: Any, site: Any) -> None:
        self.session = session
        self.auth = site.auth
        self.can_replace = self.auth.superuser_field is None or self.auth.is_superuser(request.state.user)
        self.required = True
        self.attrs = {**self.attrs, "autocomplete": "new-password"}

    def read(self, obj: Any) -> str:
        # An existing password can log in as that person, so changing it takes a superuser.
        self.required = False
        if not self.can_replace:
            self.widget = "readonly"
            self.display = self.masked
        else:
            self.help_text = self.help_text or "Leave empty to keep the current password. A new one logs this user out."
        return ""

    def format(self, value: Any) -> str:
        return self.masked if value else ""

    def clean(self, raw: Any) -> str | None:
        self.value = ""  # The form re-renders with this, and a password must not come back in the page.
        if not raw:
            if self.required:
                raise ValidationError("This field is required.")
            return None
        if len(raw) < MIN_PASSWORD_LENGTH:
            raise ValidationError(f"Use at least {MIN_PASSWORD_LENGTH} characters.")
        return raw

    def write(self, obj: Any, value: Any) -> None:
        if value:
            self.auth.set_password(self.session, obj, value)


class ReadOnlyField(Field):
    widget = "readonly"
    display: Any = ""


class _RelationBase(Field):
    def __init__(self, target: type, key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.target = target
        self.key = key
        self.mode = "select"
        self.autocomplete_url = ""
        self.add_url = ""
        self.add_label = ""

    @property
    def key_attr(self) -> Any:
        return getattr(self.target, self.key)

    def _configure(self, session: Any, request: Any, site: Any) -> None:
        self._session = session
        self._queryset = site.queryset_for(self.target, request)
        related = site.admin_for_model(self.target)
        self._related = related
        # Shared by every row of one CSV import, so each name is searched once.
        self._label_keys = request.state.label_keys
        keyed_by_pk = inspect(self.target).primary_key == (self.key_attr.property.columns[0],)
        if related is not None and keyed_by_pk:
            root = request.scope["root_path"]
            if related.search_fields:
                self.autocomplete_url = f"{root}/{related.slug}/autocomplete"
            if not request.query_params.get("_popup") and related.has_add_permission(request):
                self.add_url = f"{root}/{related.slug}/add?_popup=1"
                self.add_label = related.name.lower()

        if self.mode == "raw":
            self.widget = "input"
            return
        options = None
        if self.mode != "autocomplete":
            stmt = self._queryset.limit(RELATION_SELECT_LIMIT + 1)
            # One query per related model, however many inline rows use it.
            cache = request.state.relation_options
            options = cache.get(self.target)
            if options is None:
                options = cache[self.target] = [
                    (str(getattr(o, self.key)), object_label(o)) for o in session.scalars(stmt)
                ]
        if options is None or len(options) > RELATION_SELECT_LIMIT:
            self.widget = "autocomplete" if self.autocomplete_url else "input"
            self.choices = []
        else:
            self.choices = options
            self.widget = self.select_widget

    def _labels(self, values: list[str]) -> list[tuple[str, str]]:
        known = dict(self.choices)
        missing = [v for v in values if v and v not in known]
        if missing and self.widget == "autocomplete":
            column = self.key_attr
            try:
                wanted = [coerce_to_column(column, v) for v in missing]
            except ValueError:
                wanted = []  # A tampered value. Validation already rejected it, just show it as typed.
            found = self._session.scalars(self._queryset.where(column.in_(wanted)))
            known.update((str(getattr(o, self.key)), object_label(o)) for o in found)
        return [(v, known.get(v, v)) for v in values if v]

    def search_key(self, label: str) -> str:
        """For CSV import into a search box, which has no list of choices: the key of the one record shown as `label`.

        Searches like the autocomplete does, then keeps exact label matches. No match, or several, returns
        `label` unchanged, so validation reports it.
        """
        if not label.strip():
            return label
        cache_key = (self.target, label.lower())
        if cache_key not in self._label_keys:
            stmt = self._related.apply_search(self._queryset, label).limit(LABEL_SEARCH_LIMIT)
            found = self._session.scalars(stmt)
            matches = [str(getattr(o, self.key)) for o in found if object_label(o).lower() == label.lower()]
            self._label_keys[cache_key] = matches[0] if len(matches) == 1 else label
        return self._label_keys[cache_key]

    def _fetch(self, raw_values: list[str]) -> list[Any]:
        column = self.key_attr
        try:
            values = [coerce_to_column(column, raw) for raw in raw_values]
        except ValueError:
            raise ValidationError("Select a valid choice.") from None
        found = list(self._session.scalars(self._queryset.where(column.in_(values))))
        if len(found) != len(set(values)):
            raise ValidationError("Select a valid choice.")
        return found


class RelationField(_RelationBase):
    """A foreign key. It writes the column rather than the relationship object."""

    select_widget = "select"

    def prepare(self, session: Any, request: Any, site: Any) -> None:
        self._configure(session, request, site)

    @property
    def selected(self) -> list[tuple[str, str]]:
        return self._labels([self.value] if self.value else [])

    def to_python(self, raw: str) -> Any:
        return getattr(self._fetch([raw])[0], self.key)


class ManyToManyField(_RelationBase):
    select_widget = "multiselect"

    def prepare(self, session: Any, request: Any, site: Any) -> None:
        self._configure(session, request, site)
        if self.widget == "input":
            self.widget = "readonly"
            self.display = "Too many options to list. Add search_fields to the related admin."

    @property
    def selected(self) -> list[tuple[str, str]]:
        return self._labels(list(self.value or []))

    def read(self, obj: Any) -> Any:
        return getattr(obj, self.attr)

    def format(self, value: Any) -> list[str]:
        return [str(getattr(o, self.key)) for o in value or []]

    def extract(self, data: Any) -> list[str]:
        return data.getlist(self.html_name)

    def clean(self, raw: Any) -> list[Any]:
        if not raw:
            return []
        found = self._fetch(raw)
        for validate in self.validators:
            validate(found)
        return found


class Form:
    def __init__(self, fields: list[Field], obj: Any = None, persisted: bool = False) -> None:
        self.fields = fields
        self.obj = obj
        self.persisted = persisted
        self.errors: list[str] = []
        self.cleaned: dict[str, Any] = {}
        self.fieldsets: list[dict[str, Any]] = []
        self.delete_requested = False
        self.diffs: list[tuple[Field, Any, Any]] = []
        for field in self.editable:
            source = field.read(obj) if persisted else field.initial
            field.value = field.format(source)
        # The editable values as loaded. The page sends it back, so a save can spot changes made in the meantime.
        self.fingerprint = _fingerprint([f.value for f in self.editable]) if persisted else ""

    @property
    def editable(self) -> list[Field]:
        return [f for f in self.fields if f.widget != "readonly"]

    @property
    def is_valid(self) -> bool:
        return not self.errors and not any(f.errors for f in self.fields)

    def has_input(self, data: Any) -> bool:
        return any(f.extract(data) not in ("", [], None, f.format(f.initial)) for f in self.editable)

    def add_error(self, error: ValidationError) -> None:
        target = next((f for f in self.fields if f.name == error.field), None)
        if target is not None:
            target.errors.append(str(error))
        else:
            self.errors.append(str(error))

    def validate(self, data: Any) -> None:
        for field in self.editable:
            field.value = field.extract(data)
            try:
                self.cleaned[field.name] = field.clean(field.value)
            except ValidationError as exc:
                field.errors.append(str(exc))

    def check_unique(self, session: Any, model: type) -> None:
        for field in self.editable:
            value = self.cleaned.get(field.name)
            if not field.unique or value is None or field.errors:
                continue
            stmt = select(getattr(model, field.attr)).where(getattr(model, field.attr) == value).limit(1)
            with session.no_autoflush:
                if self.persisted:
                    stmt = stmt.where(sa.not_(_same_row(model, self.obj)))
                if session.execute(stmt).first():
                    field.errors.append(f"{field.label} must be unique. This value is already used.")

    def apply(self, obj: Any) -> list[str]:
        """Writes the cleaned values and returns the labels of what changed."""
        changed = []
        for field in self.editable:
            if field.name not in self.cleaned:
                continue
            value = self.cleaned[field.name]
            if value is None and field.keeps_default and not self.persisted:
                continue
            before = field.format(field.read(obj)) if self.persisted else None
            field.write(obj, value)
            after = field.format(value)
            if self.persisted and before != after:
                changed.append(field.label)
                self.diffs.append((field, before, after))
        return changed


def _fingerprint(values: list[Any]) -> str:
    return hashlib.sha256(json.dumps(values, default=str).encode()).hexdigest()[:FINGERPRINT_LENGTH]


def _same_row(model: type, obj: Any) -> Any:
    mapper = inspect(model)
    keys = [mapper.get_property_by_column(c).key for c in mapper.primary_key]
    return sa.and_(*[getattr(model, k) == getattr(obj, k) for k in keys])


def _field_for_column(key: str, column: sa.Column) -> Field | None:
    type_ = getattr(column.type, "impl_instance", column.type)
    if isinstance(type_, sa.Boolean):
        return BooleanField()
    if isinstance(type_, sa.Enum):
        if type_.enum_class:
            return EnumField(type_.enum_class)
        return ChoiceField(choices=[(e, humanize(e)) for e in type_.enums])
    if isinstance(type_, sa.Integer):
        return IntegerField()
    if isinstance(type_, sa.Float):
        return FloatField()
    if isinstance(type_, sa.Numeric):
        return DecimalField(max_digits=type_.precision, decimal_places=type_.scale)
    if isinstance(type_, sa.DateTime):
        return DateTimeField(aware=bool(type_.timezone))
    if isinstance(type_, sa.Date):
        return DateField()
    if isinstance(type_, sa.Time):
        return TimeField()
    if isinstance(type_, sa.Text):
        return TextField()
    if isinstance(type_, sa.String):
        kind = EmailField if "email" in key.lower() else CharField
        return kind(max_length=type_.length)
    if isinstance(type_, sa.Uuid):
        return UUIDField()
    if isinstance(type_, sa.JSON):
        return JSONField()
    return None


def _is_generated(column: sa.Column) -> bool:
    if column.primary_key:
        is_lone_integer = len(column.table.primary_key.columns) == 1 and isinstance(column.type, sa.Integer)
        return (
            column.autoincrement is True
            or column.default is not None
            or column.server_default is not None
            or (column.autoincrement == "auto" and is_lone_integer)
        )
    has_default = column.default is not None or column.server_default is not None
    is_temporal = isinstance(column.type, (sa.DateTime, sa.Date, sa.Time))
    return column.onupdate is not None or column.computed is not None or (is_temporal and has_default)


def _apply_column_meta(field: Field, attr: str, column: sa.Column) -> Field:
    has_default = column.default is not None or column.server_default is not None
    scalar_default = column.default.arg if column.default is not None and column.default.is_scalar else None
    field.attr = attr
    field.unique = bool(column.unique) and not column.primary_key
    field.help_text = field.help_text or column.doc or column.comment or ""
    field.initial = scalar_default
    field.required = not column.nullable and not has_default
    field.empty_value = None
    field.keeps_default = has_default and not column.nullable
    if isinstance(field, (CharField,)) and not column.nullable and not field.keeps_default:
        field.empty_value = ""
    return field


def _target_of_foreign_key(mapper: Any, column: sa.Column) -> tuple[type, str] | None:
    remote = next(iter(column.foreign_keys)).column
    for candidate in mapper.registry.mappers:
        if remote.table is candidate.local_table:
            return candidate.class_, candidate.get_property_by_column(remote).key
    return None


@functools.cache
def fields_for_model(model: type) -> tuple[dict[str, Field], set[str]]:
    """Every field the model could expose, plus the names shown by default.

    Generated columns and anything that looks like a credential exist as fields but
    aren't shown unless a ModelAdmin asks for them by name.
    """
    mapper = inspect(model)
    foreign_keys: dict[Any, Any] = {}
    for rel in mapper.relationships:
        if rel.direction is MANYTOONE and len(rel.local_columns) == 1 and not rel.viewonly:
            foreign_keys[next(iter(rel.local_columns))] = rel

    fields: dict[str, Field] = {}
    hidden: set[str] = set()
    for prop in mapper.column_attrs:
        column = prop.columns[0]
        if not isinstance(column, sa.Column):
            continue
        rel = foreign_keys.get(column)
        target = None
        if rel is not None:
            remote_key = rel.mapper.get_property_by_column(next(iter(rel.remote_side))).key
            target = (rel.mapper.class_, remote_key)
        elif column.foreign_keys and len(column.foreign_keys) == 1:
            target = _target_of_foreign_key(mapper, column)

        name = rel.key if rel is not None else prop.key
        if target is not None:
            field: Field | None = RelationField(*target)
        else:
            field = _field_for_column(prop.key, column)
        if field is None:
            continue
        fields[name] = _apply_column_meta(field, prop.key, column)
        field.generated = _is_generated(column)
        if field.generated or SENSITIVE_NAME.search(prop.key):
            hidden.add(name)

    for rel in mapper.relationships:
        if rel.direction is MANYTOMANY and not rel.viewonly:
            target_pk = rel.mapper.primary_key[0]
            field = ManyToManyField(rel.mapper.class_, rel.mapper.get_property_by_column(target_pk).key)
            field.attr = rel.key
            field.required = False
            fields[rel.key] = field
    return fields, hidden
