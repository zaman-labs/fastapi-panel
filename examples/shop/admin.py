from decimal import Decimal

from markupsafe import Markup
from models import Category, Customer, Order, OrderItem, OrderStatus, Product, Staff, Tag
from sqlalchemy import func, select

from fastapi_panel import (
    DecimalField,
    ImageField,
    LocalStorage,
    ModelAdmin,
    RichTextField,
    Setting,
    SimpleFilter,
    TabularInline,
    ValidationError,
    action,
    display,
    register,
    site,
)

site.title = "Acme Shop"
site.header = "Acme Shop"
site.storage = LocalStorage("media")

shop_settings = site.live_settings(
    banner=Setting("", help_text="Shown at the top of the store.", group="Store"),
    maintenance_mode=Setting(False, group="Store"),
    free_shipping_over=Setting(Decimal("50.00"), group="Checkout", field=DecimalField(min_value=Decimal(0))),
)


@site.widget("Orders to ship", url="order?status=PAID", permission="order.view")
def orders_to_ship(session, request):
    return session.scalar(select(func.count()).where(Order.status == OrderStatus.PAID))


@site.widget("Low stock products", url="product?stock_level=low", permission="product.view")
def low_stock(session, request):
    return session.scalar(select(func.count()).where(Product.stock > 0, Product.stock < 10))


@site.widget("Revenue from shipped orders", permission="order.view")
def revenue(session, request):
    total = session.scalar(
        select(func.sum(OrderItem.quantity * OrderItem.unit_price))
        .join(Order)
        .where(Order.status == OrderStatus.SHIPPED)
    )
    return f"${total or 0:,.2f}"


class StockFilter(SimpleFilter):
    title = "Stock"
    parameter = "stock_level"

    def lookups(self, request):
        return [("out", "Out of stock"), ("low", "Low (under 10)")]

    def queryset(self, request, stmt, value):
        if value == "out":
            return stmt.where(Product.stock == 0)
        return stmt.where(Product.stock > 0, Product.stock < 10)


@register(Product)
class ProductAdmin(ModelAdmin):
    list_display = ("name", "image", "category", "price", "stock", "is_published", "created_at")
    list_editable = ("stock", "is_published")
    list_filter = ("is_published", "category", StockFilter, "created_at")
    search_fields = ("name", "=slug", "category__name")
    search_help_text = "Search by name, category, or the exact slug."
    ordering = ("-created_at",)
    date_hierarchy = "created_at"
    list_per_page = 20
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("category",)
    filter_horizontal = ("tags",)
    readonly_fields = ("created_at",)
    save_as = True
    actions = ("publish", "unpublish")
    soft_delete_field = "deleted_at"
    csv_import = True
    log_values = ("price", "stock", "is_published")
    field_overrides = {
        "price": DecimalField(min_value=Decimal(0), max_digits=10, decimal_places=2),
        "image": ImageField(help_text="PNG, JPG, GIF or WEBP, up to 5 MB."),
        "description": RichTextField(),
    }
    fieldsets = (
        (None, {"fields": ("name", "slug", "category", "image")}),
        ("Pricing and stock", {"fields": (("price", "stock"), "is_published")}),
        ("Details", {"fields": ("description", "tags", "created_at"), "classes": ("collapse",)}),
    )

    @action("Publish selected products")
    def publish(self, session, request, objects):
        for product in objects:
            product.is_published = True
        self.message_user(request, f"Published {len(objects)} products.")

    @action("Unpublish selected products", confirm="Remove these products from the store?")
    def unpublish(self, session, request, objects):
        for product in objects:
            product.is_published = False
        self.message_user(request, f"Unpublished {len(objects)} products.")


@register(Category)
class CategoryAdmin(ModelAdmin):
    list_display = ("name", "slug")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}


@register(Tag)
class TagAdmin(ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


class OrderItemInline(TabularInline):
    model = OrderItem
    extra = 1
    autocomplete_fields = ("product",)


@register(Order)
class OrderAdmin(ModelAdmin):
    list_display = ("__str__", "customer", "status_badge", "total", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("customer__name", "customer__email")
    ordering = ("-created_at",)
    live_updates = 5
    autocomplete_fields = ("customer",)
    inlines = (OrderItemInline,)
    readonly_fields = ("created_at", "total")
    save_on_top = True
    object_actions = ("mark_shipped",)

    @display("Status", ordering="status")
    def status_badge(self, order):
        tag = {OrderStatus.CANCELLED: "delete", OrderStatus.SHIPPED: "add"}.get(order.status, "change")
        return Markup('<span class="tag tag-{}">{}</span>').format(tag, order.status.name.lower())

    @display("Total")
    def total(self, order):
        return f"${sum(item.quantity * item.unit_price for item in order.items):,.2f}"

    @action("Mark as shipped", confirm="Mark this order as shipped?")
    def mark_shipped(self, session, request, order):
        order.status = OrderStatus.SHIPPED
        self.message_user(request, f"{order} was marked as shipped.")

    def get_object_actions(self, request, order):
        found = super().get_object_actions(request, order)
        if order.status is not OrderStatus.PAID:
            found.pop("mark_shipped", None)
        return found

    def clean(self, data, obj=None):
        if data.get("status") is OrderStatus.CANCELLED and not data.get("note"):
            raise ValidationError("Say why this order was cancelled.", field="note")


@register(Customer)
class CustomerAdmin(ModelAdmin):
    list_display = ("name", "email", "country")
    list_filter = ("country",)
    search_fields = ("name", "email")


@register(Staff)
class StaffAdmin(ModelAdmin):
    list_display = ("username", "is_active", "is_superuser")
    list_filter = ("is_active", "is_superuser")
    group = "Access"
    verbose_name_plural = "Staff"

    def has_module_permission(self, request):
        return request.state.user.is_superuser

    has_view_permission = has_add_permission = has_change_permission = has_delete_permission = (
        lambda self, request, obj=None: request.state.user.is_superuser
    )
