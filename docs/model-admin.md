# ModelAdmin reference

Subclass `ModelAdmin` and set what you need. Everything has a default.

## The list page

| Option | Default | What it does |
|---|---|---|
| `list_display` | `("__str__",)` | Columns: a column, relationship, `related__field`, an admin method, a model method or a function |
| `list_display_links` | first column | Columns that link to the edit page |
| `list_editable` | `()` | Columns edited directly in the list. They must also be in `list_display` |
| `list_filter` | `()` | Sidebar filters, see [Filters](#filters) |
| `list_per_page` | `50` | Rows per page |
| `search_fields` | `()` | Searched columns, may cross relations: `category__name` |
| `search_help_text` | `""` | Hint under the search box |
| `ordering` | primary key, newest first | Default sort: `("-created_at", "name")` |
| `date_hierarchy` | `None` | A date column for year, month and day drill-down |
| `show_full_result_count` | `True` | Run a second count for "3 of 1,000". Turn off on huge tables |
| `live_updates` | `None` | Seconds between checks for new rows. The list then shows "3 new orders. Refresh", which catches rows other services write too. Each open list runs one count with its filters per check, paused while the tab is hidden |

Search prefixes: `"^name"` starts with, `"=email"` exact, no prefix means contains. Each word has to match at least one field.

Relationships in `list_display` are loaded up front, so a list of 50 rows doesn't run 50 extra queries. If a computed column reaches further, add the loading yourself in `get_queryset`.

### Computed columns

```python
from fastapi_panel import ModelAdmin, display
from markupsafe import Markup


class OrderAdmin(ModelAdmin):
    list_display = ("id", "customer", "total", "is_paid", "badge")

    @display("Total", ordering="amount")
    def total(self, order):
        return f"${order.amount:,.2f}"

    @display("Paid", boolean=True)
    def is_paid(self, order):
        return order.paid_at is not None

    @display("Status")
    def badge(self, order):
        return Markup('<span class="tag tag-add">{}</span>').format(order.status)
```

Values are escaped. Return `Markup` only for HTML you built yourself, and use `Markup(...).format(...)` so that the inserted values still get escaped. Never put user input in the template string.

## Forms

| Option | Default | What it does |
|---|---|---|
| `fields` | every editable column | Which fields to show and in what order. A tuple puts fields on one row |
| `exclude` | `()` | Remove fields from the default set |
| `fieldsets` | `None` | Sections, see below |
| `readonly_fields` | `()` | Shown but not editable. May include admin methods |
| `prepopulated_fields` | `{}` | `{"slug": ("title",)}` fills a slug as you type |
| `autocomplete_fields` | `()` | Search box for a relation. The related admin needs `search_fields` |
| `raw_id_fields` | `()` | Plain ID input for a relation |
| `filter_horizontal` | `()` | Two-column picker for a many-to-many |
| `field_overrides` | `{}` | Replace a generated field: `{"price": DecimalField(min_value=0)}` |
| `form_schema` | `None` | A Pydantic model to validate with |
| `inlines` | `()` | Related rows on the same page |
| `save_as` | `False` | Adds "Save as new" |
| `save_on_top` | `False` | Repeats the buttons above the form |

```python
fieldsets = (
    (None, {"fields": ("name", "slug")}),
    ("Pricing", {"fields": (("price", "stock"),), "description": "Prices include tax."}),
    ("Advanced", {"fields": ("metadata",), "classes": ("collapse",)}),
)
```

Fields are generated from column types: text, numbers, decimals, booleans, dates, times, enums, UUIDs and JSON. Foreign keys become a dropdown, and above 200 options they switch to autocomplete or an ID input so large tables stay fast.

Next to a relation field there's a **+** button when the user may add to the related model. It opens the add form in a dialog and selects the new record when it's saved. The related model needs to be registered with a single primary key.

These columns are left out of the default form. List one in `fields` to bring it back:

- Autoincrement primary keys and columns with database-generated values
- Timestamps with a default or `onupdate`
- Anything whose name looks like a credential: `password`, `secret`, `token`, `api_key`, `private_key`

### Validation

Required, type, length, uniqueness and "does this foreign key exist" checks are automatic. For your own rules, use `clean`:

```python
from fastapi_panel import ValidationError


class OrderAdmin(ModelAdmin):
    def clean(self, data, obj=None):
        if data["status"] is Status.CANCELLED and not data.get("note"):
            raise ValidationError("Say why it was cancelled.", field="note")
```

Without `field` the error shows at the top of the form. `data` holds clean values and you can change it. `obj` is the existing row, or `None` when adding.

Field validators work too: `CharField(validators=[check_sku])`. To validate with an existing schema, set `form_schema = ProductCreate`, and its errors appear next to the matching fields.

## Rich text

```python
from fastapi_panel import ModelAdmin, RichTextField


class ArticleAdmin(ModelAdmin):
    field_overrides = {"body": RichTextField()}
```

The column holds HTML, edited with a small toolbar: bold, italic, underline, headings, lists, quotes and links. There's nothing to install and no external script. Without JavaScript it falls back to a plain text box.

Whatever is submitted gets rebuilt from a short allowlist of tags, and only `http`, `https` and `mailto` links survive, so a script can't get in through this field. **Sanitize when you print it on your own site too**, in case a row was written some other way: `sanitize_html(article.body)`.

Lists show the text without tags. CSV import goes through the same sanitizing. Images and tables aren't supported.

## Saved filters

Once someone has searched or filtered a list, **Save this view** keeps the search, filters and sorting under a name. Saved views appear above the list as links, and belong to that user and that model. There's nothing to configure.

## History

Each record's history lists who changed which fields. To also keep old and new values, name the fields:

```python
class OrderAdmin(ModelAdmin):
    log_values = ("status", "total")
```

```
Changed status. Status: Paid → Shipped
```

This is opt-in because those values stay in the log for good, so think before listing personal data. Fields named like credentials are refused at startup. Long values are cut at 200 characters. Inlines take `log_values` too.

Everyone's changes are on the **Activity** page, filterable by user, model and action. See [who can open it](auth-and-permissions.md#activity).

## Files and images

A file lives in a string column that holds its stored name. Set a storage on the site, then use `FileField` or `ImageField` for the column:

```python
from fastapi_panel import FileField, ImageField, LocalStorage, ModelAdmin, site

site.storage = LocalStorage("/var/lib/myapp/uploads")  # outside anything your web server serves


class ProductAdmin(ModelAdmin):
    list_display = ("name", "photo")
    field_overrides = {
        "photo": ImageField(max_size=2 * 1024 * 1024),
        "manual": FileField(allowed_extensions=("pdf",), upload_to="manuals"),
    }
```

| Option | Default | What it does |
|---|---|---|
| `max_size` | 5 MB | Largest file, in bytes. `mount(max_body_size=...)`, 10 MB by default, must fit the whole form |
| `allowed_extensions` | documents and images | `ImageField` takes PNG, JPEG, GIF and WEBP only |
| `upload_to` | the model's name | The folder inside the storage |

Lists show a thumbnail or a link, and forms show the current file with a preview of the new one. Both work in inlines too. File columns aren't part of CSV imports.

Replaced and deleted files stay in storage, because a save can still roll back and a restored record needs its file. Clean them up with your own job if you need to, for example when a file holds personal data.

To store files elsewhere, implement two methods. Files are still served through the panel, behind login:

```python
from fastapi_panel import Storage


class S3Storage(Storage):
    def save(self, file, name): ...  # `file` is a binary file object
    def open(self, name): ...  # return a binary file object, raise FileNotFoundError if it's missing
```

## Soft delete and restore

If your model marks rows as deleted, name the column. It must be a boolean or a nullable datetime:

```python
class ArticleAdmin(ModelAdmin):
    soft_delete_field = "deleted_at"
```

Deleting now only sets the column, and the row disappears from lists, forms, dropdowns and autocomplete. A **Deleted** button on the list opens a page with the same search and filters, where people with change permission can restore rows and people with delete permission can remove them for good.

This relies on `get_queryset`, so call `super().get_queryset(request)` when you override it. Your own `delete_model` replaces the behavior, and `restore_model` is there to override as well. Inline rows are always really deleted, and unique rules still count deleted rows, because the database does.

## CSV import

```python
class ProductAdmin(ModelAdmin):
    csv_import = True
    import_fields = ("name", "slug", "category", "price")  # optional, the form's fields by default
    import_max_rows = 5_000
```

An **Import** button appears for people who can add or change. They upload a file and see what each row would do before anything is saved. If any row has a problem, nothing is imported.

- Columns match by field name or label, so a file from `export_csv` works. Other columns are listed and ignored. Commas and semicolons both work, and the file must be UTF-8.
- A column with the primary key, like `ID`, updates that row, and only the columns in the file change. Without it, rows are added.
- Relations and choices can use their label: `Home` for a category, `Shipped` for a status. Separate many-to-many values with commas.
- Every row goes through the same validation as the form, including `clean`, and through `get_queryset` and the permission checks. `save_model` only runs on the real import, not the preview.

## Inlines

```python
from fastapi_panel import ModelAdmin, TabularInline


class OrderItemInline(TabularInline):
    model = OrderItem
    extra = 1
    max_num = 50
    autocomplete_fields = ("product",)


class OrderAdmin(ModelAdmin):
    inlines = (OrderItemInline,)
```

Use `StackedInline` for one form per row. Inlines take the form options above, and also `extra`, `min_num`, `max_num`, `can_delete`, `ordering` and `fk_name`. You need `fk_name` when the model has two foreign keys to the parent.

## Actions

```python
from fastapi_panel import ModelAdmin, action


class ProductAdmin(ModelAdmin):
    actions = ("publish",)

    @action("Publish selected", permissions=("change",), confirm="Publish these products?")
    def publish(self, session, request, objects):
        for product in objects:
            product.is_published = True
        self.message_user(request, f"Published {len(objects)} products.")
```

The panel commits for you. Return a `Response` to send something else back, such as a file. `delete_selected` and `export_csv` are included. Change that with `builtin_actions = ()`, choose the exported columns with `export_fields`, and adjust the limit with `action_max_rows` (10,000 by default).

### Buttons on one record

`object_actions` adds buttons next to **History** on a record's page, for things like a refund:

```python
from fastapi_panel import ModelAdmin, ValidationError, action


class OrderAdmin(ModelAdmin):
    object_actions = ("refund",)

    @action("Refund", permissions=("change",), confirm="Refund this order?")
    def refund(self, session, request, order):
        if order.refunded:
            raise ValidationError("This order was already refunded.")
        order.refunded = True
        self.message_user(request, "Refunded.")

    def get_object_actions(self, request, order):
        found = super().get_object_actions(request, order)
        if order.status != "paid":
            found.pop("refund", None)
        return found
```

These use the same `@action` as bulk actions, but receive one object. The panel commits for you and writes the action to the record's history. Raising `ValidationError` undoes the changes and shows the message. Returning a `Response`, such as a file, sends that back instead of returning to the record. Clicking a button with unsaved edits on the page asks before leaving, since those edits would be lost.

`get_object_actions` is optional and decides which buttons a record gets. The same check runs when a button is posted, and `permissions` is checked for that record. A custom permission needs a `has_<name>_permission(self, request, obj=None)` method.

### What deleting shows

Before anything is deleted, a confirmation page shows a collapsible tree of everything that goes with it, with a count per model and a link to each record the user can open:

- Rows removed by `cascade="..."` on your relationships or by `ON DELETE CASCADE` in the database are listed, at every level.
- Many-to-many rows are listed as links. Only the link is removed, not the record on the other side.
- Rows kept by `ON DELETE SET NULL`, or by a relationship that clears a nullable foreign key, aren't listed.
- Rows that stop the delete, such as a required foreign key with no cascade, appear in a red section that replaces the delete button.

Records the user can't reach through that model's own admin are shown as "Hidden" without their label. Models with no admin show their label with no link. The check stops after 5,000 related rows and says so on the page, and the database can still refuse the delete, which shows an error message.

A custom `delete_model` isn't blocked up front, since it may handle dependent rows itself or keep the row. Soft deletes list nothing, because no related rows change. `delete_selected` loads the rows and calls `delete_model` on each, so your hooks and cascades run.

## Filters

`list_filter` accepts column names, relationship names and classes. The right filter is picked from the type: yes/no for booleans, a list for enums, date ranges for dates, related objects for relationships, and distinct values for anything else.

```python
from fastapi_panel import SimpleFilter


class StockFilter(SimpleFilter):
    title = "Stock"
    parameter = "stock_level"

    def lookups(self, request):
        return [("out", "Out of stock"), ("low", "Running low")]

    def queryset(self, request, stmt, value):
        if value == "out":
            return stmt.where(Product.stock == 0)
        return stmt.where(Product.stock.between(1, 9))
```

Only registered filters read the query string, so a URL can't filter on a column you didn't expose.

## Hooks

Hooks are normal synchronous functions, even on an async engine. They run in the request's transaction, so raising an exception rolls back the save.

```python
class ProductAdmin(ModelAdmin):
    def get_queryset(self, request):
        return super().get_queryset(request).where(Product.tenant_id == request.state.user.tenant_id)

    def save_model(self, session, request, obj, change):
        obj.updated_by = request.state.user.id
        super().save_model(session, request, obj, change)

    def delete_model(self, session, request, obj):
        obj.deleted = True  # soft delete

    def view_on_site(self, obj):
        return f"/products/{obj.slug}"
```

Also available: `get_fields`, `get_fieldsets`, `get_readonly_fields`, `get_list_display`, `get_ordering`, `get_inlines`, `get_actions`, `get_list_filters`, `get_import_fields`, `restore_model`, `has_view_permission`, `has_add_permission`, `has_change_permission`, `has_delete_permission`, `has_module_permission`, `get_object_actions` and `get_routes`.

Hooks run on the event loop when you use an async engine, so database work through `session` is fine but slow blocking calls, like an HTTP request, aren't. Queue those for a background worker.

## Naming and navigation

| Option | Default |
|---|---|
| `verbose_name` | The class name, spaced out |
| `verbose_name_plural` | `verbose_name` + s |
| `group` | From the module: `shop.models` becomes "Shop" |
| `slug` | The class name in lowercase, used in URLs |
