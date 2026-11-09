# Modular projects and customizing

## Splitting a project into modules

Each part of your app keeps its own `admin.py`, and `autodiscover` finds them:

```
app/
  billing/
    models.py
    admin.py
  catalog/
    models.py
    admin.py
  main.py
```

```python
# app/main.py
from fastapi_panel import autodiscover, site

autodiscover("app")
site.mount(app, engine=engine, auth=auth, secret_key=key)
```

The sidebar groups models by module, so these show up as "Billing" and "Catalog". Set `group = "Finance"` on a `ModelAdmin` to choose the name yourself. Click a group's title to collapse it, and it stays collapsed in that browser.

A reusable package can ship its own admin the same way. Your app calls `autodiscover("that_package")`, and if you want a different configuration, unregister and register again:

```python
site.unregister(Invoice)
site.register(Invoice, MyInvoiceAdmin)
```

## Several sites

`site` is a ready-made default, but you can create as many as you need, each with its own login and models:

```python
from fastapi_panel import AdminSite, register

support = AdminSite(title="Support")


@register(Ticket, site=support)
class TicketAdmin(ModelAdmin): ...


support.mount(app, path="/support", engine=engine, auth=support_auth, secret_key=key)
```

## Site options

```python
AdminSite(
    title="Acme",  # browser tab
    header="Acme Back Office",  # sidebar and login
    index_title="Overview",
    template_dirs=["templates"],
    extra_css=["/static/brand.css"],
    audit_log=True,
    timezone="Asia/Baku",
    storage=LocalStorage("uploads"),
)
```

### Timezone

`timezone` applies to lists, forms, CSV files, the audit log and the date filters. Your database doesn't change: aware columns are stored in UTC, and naive columns are treated as UTC too. If your naive columns hold local time, pass `naive_utc=False` to leave them exactly as they are. The date drill-down above a list groups by the stored value, so a row close to midnight can land on the neighboring day there.

Settings for `mount`:

| Option | Default | What it does |
|---|---|---|
| `path` | `/admin` | Where the panel lives |
| `session_age` | 12 hours | Session lifetime in seconds |
| `secure_cookies` | automatic | Force the `Secure` flag on or off. Automatic follows the request scheme |
| `trusted_origins` | `()` | Extra origins allowed to POST, see [Security](security.md) |
| `max_body_size` | 10 MB | Largest POST body accepted |
| `password_reset` | `None` | A function that delivers reset links, see [Password reset](auth-and-permissions.md#password-reset) |
| `public_url` | `None` | The panel's public address, required with `password_reset` |
| `query_timeout` | `None` | Seconds a panel query may run before PostgreSQL cancels it, so a heavy filter or search can't hold up the database your app shares. The page then says it took too long. PostgreSQL only; it applies to the panel's own transactions and nothing else |

## Live settings

Values that people change in the panel, with no code change or deploy, such as a banner or a shipping threshold:

```python
from decimal import Decimal

from fastapi_panel import DecimalField, Setting, site

settings = site.live_settings(
    banner=Setting("", help_text="Shown at the top of the store.", group="Store"),
    maintenance_mode=Setting(False, group="Store"),
    free_shipping_over=Setting(Decimal("50.00"), group="Checkout", field=DecimalField(min_value=Decimal(0))),
)
```

Then read them where you need them. A change applies on the next read, on every worker:

```python
@app.get("/checkout")
async def checkout():
    threshold = await settings.aget("free_shipping_over")  # a Decimal
```

`settings.get("name")` does the same in synchronous code on a sync engine. Both work once `site.mount` has run, and each read is one small query, so read once per request rather than in a loop.

The default sets the type: `bool`, `int`, `float`, `Decimal`, `str`, `date`, `time`, `datetime`, `list` or `dict`. Pass `field=` for limits or a different input, as above. Only values different from the default are stored, so changing a default in code applies to anyone who hasn't changed that setting. An empty number goes back to its default. If a saved value no longer fits the type, the default is used and a warning is logged.

The **Settings** page under System needs the `settings.change` permission. Each change appears on the Activity page with its old and new values, for anyone who has both `activity.view` and `settings.change`.

A name that looks like a secret, such as `api_key`, is refused at startup, because values are stored unencrypted and shown in the form. Keep secrets in your environment.

## Custom pages

```python
from sqlalchemy import func, select


@site.page("/reports", title="Sales report", group="Reports", permission="reports.view")
def sales_report(session, request):
    total = session.scalar(select(func.sum(Order.amount)))
    return site.render(request, "reports.html", {"title": "Sales report", "total": total})
```

The page appears in the sidebar and sits behind the same login, CSRF protection and headers as everything else. Views take `(session, request)` and return a response. POST pages need `methods=("GET", "POST")` and a hidden `csrf_token` field.

```html
{% extends "fastapi_panel/base.html" %}
{% block content %}
  <h1 class="page-title">{{ title }}</h1>
  <div class="card card-padded">Total sales: {{ total }}</div>
{% endblock %}
```

To add a page to one model, return routes from `get_routes`. They're served under `/<slug>`:

```python
class OrderAdmin(ModelAdmin):
    def get_routes(self):
        return [("/stats", self.stats, ["GET"])]

    def stats(self, session, request):
        self.require(self.has_view_permission(request))
        ...
```

## Dashboard widgets

Stat cards at the top of the dashboard. A widget takes `(session, request)` and returns a number or short text:

```python
@site.widget("Orders to ship", url="order?status=PAID", permission="order.view")
def orders_to_ship(session, request):
    return session.scalar(select(func.count()).where(Order.status == OrderStatus.PAID))
```

`url` is a path inside the panel the card links to, often a filtered list. `permission` hides the card from people without it. Whole numbers get thousands separators. If a widget raises, its card says "Unavailable", the error is logged, and the rest of the dashboard still loads.

## Templates

Every template can be replaced. Files in `template_dirs` win over the built-in ones, so to change the login page create `templates/fastapi_panel/login.html`. You can also extend the original pieces, such as `base.html` and its `content` block.

For one model only, set `list_template` or `form_template` on its `ModelAdmin`.

The default Content-Security-Policy blocks inline scripts and styles, so keep yours in files. Static files from your own app are fine since they share the origin.

## Colors

Everything uses CSS variables. Load a stylesheet with `extra_css` and override them:

```css
:root { --accent: #6d28d9; --accent-hover: #5b21b6; --accent-soft: #ede9fe; }
:root[data-theme="dark"] { --accent: #a78bfa; --on-accent: #1e1b4b; }
```

## Fields

`field_overrides` replaces a generated field:

```python
from fastapi_panel import ChoiceField, ModelAdmin, TextField


class ProductAdmin(ModelAdmin):
    field_overrides = {
        "summary": TextField(help_text="Shown on the product card."),
        "size": ChoiceField(choices=[("s", "Small"), ("m", "Medium"), ("l", "Large")]),
    }
```

To write your own, subclass `Field` and implement `to_python`, raising `ValidationError` for bad input. Implement `format` too if the value needs converting for display.
