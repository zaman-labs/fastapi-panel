from __future__ import annotations

import asyncio
import re

import httpx
import pytest
from conftest import Item, ItemAdmin, Natural, Note, Owner
from fastapi import FastAPI
from sqlalchemy import create_engine, select

from fastapi_panel import AdminSite, ImproperlyConfigured, ModelAdmin, StaticAuth, hash_password, verify_password
from fastapi_panel.log import AdminLog


def test_changelist_lists_paginates_and_sorts(panel):
    page = panel.get("/item")
    assert page.status_code == 200
    assert "item0" in page.text and "item4" in page.text and "item5" not in page.text

    second = panel.get("/item?page=2")
    assert "item5" in second.text and "item0" not in second.text

    descending = panel.get("/item?o=-name")
    assert descending.text.index("item6") < descending.text.index("item5")


def test_sorting_by_unknown_column_is_ignored(panel):
    page = panel.get("/item?o=password")
    assert page.status_code == 200
    assert page.text.index("item0") < page.text.index("item1")


def test_out_of_range_and_garbage_pages_are_clamped(panel):
    assert "item5" in panel.get("/item?page=999").text
    assert "item0" in panel.get("/item?page=abc").text


def test_search_spans_relations_and_escapes_wildcards(panel):
    assert "item3" in panel.get("/item?q=item3").text
    assert "item0" in panel.get("/item?q=alice").text
    assert "item3" not in panel.get("/item?q=alice").text
    assert "item0" not in panel.get("/item?q=%25").text


@pytest.mark.parametrize(
    ("query", "present", "absent"),
    [
        ("active=1", "item0", "item1"),
        ("active=0", "item1", "item0"),
        ("cheap=yes", "item0", "item5"),
        ("owner=1", "item0", "item3"),
    ],
)
def test_filters(panel, query, present, absent):
    page = panel.get(f"/item?{query}")
    assert present in page.text
    assert f">{absent}<" not in page.text


def test_unknown_and_invalid_filter_values_are_ignored(panel):
    assert "item0" in panel.get("/item?password__startswith=a").text
    assert "item0" in panel.get("/item?owner=notanumber&status=BOGUS&created_at=nope").text


def test_date_hierarchy_ignores_invalid_dates(panel):
    assert panel.get("/item?created_at__year=2020&created_at__month=99").status_code == 200
    year = panel.get("/item?created_at__year=1999")
    assert "No " in year.text or "Nothing matches" in year.text


def test_add_change_and_audit_log(panel, db):
    response = panel.post("/item/add", {"name": "fresh", "price": "9.50", "status": "LIVE", "note-TOTAL": "0"})
    assert response.status_code == 303
    item = db.scalar(select(Item).where(Item.name == "fresh"))
    assert str(item.price) == "9.50" and item.status.name == "LIVE"

    response = panel.post(
        f"/item/{item.id}/change", {"name": "renamed", "price": "9.50", "status": "LIVE", "note-TOTAL": "0"}
    )
    assert response.status_code == 303
    db.expire_all()
    assert db.get(Item, item.id).name == "renamed"

    entries = list(db.scalars(select(AdminLog).where(AdminLog.object_id == str(item.id)).order_by(AdminLog.id)))
    assert [e.action for e in entries] == ["add", "change"]
    assert "Name" in entries[1].message.title()
    assert "renamed" not in entries[1].message

    history = panel.get(f"/item/{item.id}/history")
    assert history.status_code == 200 and "Name" in history.text.title()


def test_validation_errors_keep_input_and_save_nothing(panel, db):
    page = panel.post("/item/add", {"name": "", "price": "abc", "status": "BOGUS", "note-TOTAL": "0"})
    assert page.status_code == 200
    assert "This field is required." in page.text
    assert "Enter a number." in page.text
    assert "Select a valid choice." in page.text
    assert 'value="abc"' in page.text
    assert db.scalar(select(Item).where(Item.price == 0)) is None


def test_unique_and_clean_hook(panel):
    taken = panel.post("/item/add", {"name": "item0", "price": "1", "status": "LIVE", "note-TOTAL": "0"})
    assert "must be unique" in taken.text
    refused = panel.post("/item/add", {"name": "forbidden", "price": "1", "status": "LIVE", "note-TOTAL": "0"})
    assert "Not this name." in refused.text


def test_nonexistent_foreign_key_is_rejected(panel):
    page = panel.post("/item/add", {"name": "fk", "price": "1", "status": "LIVE", "owner": "9999", "note-TOTAL": "0"})
    assert "Select a valid choice." in page.text


def test_nan_and_infinity_are_rejected(panel):
    for value in ("NaN", "Infinity"):
        page = panel.post("/item/add", {"name": "num", "price": value, "status": "LIVE", "note-TOTAL": "0"})
        assert "Enter a number." in page.text


def test_many_to_many_saves(panel, db):
    panel.post("/item/add", {"name": "tagged", "price": "1", "status": "LIVE", "tags": ["1", "2"], "note-TOTAL": "0"})
    item = db.scalar(select(Item).where(Item.name == "tagged"))
    assert {t.name for t in item.tags} == {"red", "blue"}


def test_inline_add_edit_delete(panel, db):
    item = db.scalar(select(Item).where(Item.name == "item0"))
    base = {"name": "item0", "price": "1", "status": "DRAFT"}
    panel.post(f"/item/{item.id}/change", {**base, "note-TOTAL": "2", "note-0-text": "first", "note-1-text": ""})
    note = db.scalar(select(Note).where(Note.item_id == item.id))
    note_id = note.id
    assert note.text == "first"
    assert db.query(Note).count() == 1

    panel.post(
        f"/item/{item.id}/change",
        {**base, "note-TOTAL": "1", "note-0-_pk": str(note_id), "note-0-text": "edited"},
    )
    db.expire_all()
    assert db.get(Note, note_id).text == "edited"

    panel.post(
        f"/item/{item.id}/change",
        {**base, "note-TOTAL": "1", "note-0-_pk": str(note_id), "note-0-text": "edited", "note-0-DELETE": "on"},
    )
    db.expire_all()
    assert db.get(Note, note_id) is None


def test_inline_cannot_touch_another_parents_row(panel, db):
    first = db.scalar(select(Item).where(Item.name == "item0"))
    other = db.scalar(select(Item).where(Item.name == "item1"))
    note = Note(text="private", item_id=other.id)
    db.add(note)
    db.commit()

    panel.post(
        f"/item/{first.id}/change",
        {
            "name": "item0",
            "price": "1",
            "status": "DRAFT",
            "note-TOTAL": "1",
            "note-0-_pk": str(note.id),
            "note-0-text": "hacked",
            "note-0-DELETE": "on",
        },
    )
    db.expire_all()
    kept = db.get(Note, note.id)
    assert kept is not None and kept.text == "private" and kept.item_id == other.id


def test_inline_max_num_is_enforced(panel, db):
    item = db.scalar(select(Item).where(Item.name == "item0"))
    data = {"name": "item0", "price": "1", "status": "DRAFT", "note-TOTAL": "4"}
    data.update({f"note-{i}-text": f"n{i}" for i in range(4)})
    page = panel.post(f"/item/{item.id}/change", data)
    assert "at most 3" in page.text
    assert db.query(Note).count() == 0


def test_save_as_new_creates_a_copy(panel, db):
    item = db.scalar(select(Item).where(Item.name == "item0"))
    response = panel.post(
        f"/item/{item.id}/change",
        {"name": "copy", "price": "1", "status": "DRAFT", "_saveasnew": "1", "note-TOTAL": "0"},
    )
    assert response.status_code == 303
    db.expire_all()
    assert db.scalar(select(Item).where(Item.name == "copy")) is not None
    assert db.get(Item, item.id).name == "item0"


def test_list_editable_saves_and_reports_errors(panel, db):
    item = db.scalar(select(Item).where(Item.name == "item0"))
    response = panel.post("/item", {"_save_list": "1", "_row": str(item.id), f"row-{item.id}-price": "42.00"})
    assert response.status_code == 303
    db.expire_all()
    assert str(db.get(Item, item.id).price) == "42.00"

    page = panel.post("/item", {"_save_list": "1", "_row": str(item.id), f"row-{item.id}-price": "oops"})
    assert page.status_code == 200 and "Enter a number." in page.text
    db.expire_all()
    assert str(db.get(Item, item.id).price) == "42.00"


def test_custom_action(panel, db):
    inactive = db.scalar(select(Item).where(Item.active.is_(False)))
    response = panel.post("/item", {"action": "activate", "_selected": str(inactive.id)})
    assert response.status_code == 303
    db.expire_all()
    assert db.get(Item, inactive.id).active is True


def test_unknown_action_is_refused(panel):
    response = panel.post("/item", {"action": "__init__", "_selected": "1"})
    assert response.status_code == 303
    assert "Choose an action" in panel.get("/item").text


def test_delete_selected_asks_first(panel, db):
    target_id = db.scalar(select(Item.id).where(Item.name == "item0"))
    db.add(Note(text="child note", item_id=target_id))
    db.commit()
    confirm = panel.post("/item", {"action": "delete_selected", "_selected": str(target_id)})
    assert confirm.status_code == 200 and "Yes, delete" in confirm.text and "Note" in confirm.text
    db.expire_all()
    assert db.get(Item, target_id) is not None

    done = panel.post("/item", {"action": "delete_selected", "_selected": str(target_id), "_confirm": "1"})
    assert done.status_code == 303
    db.expire_all()
    assert db.get(Item, target_id) is None
    assert db.query(Note).count() == 0


def test_select_across_respects_filters(panel, db):
    panel.post("/item?active=0", {"action": "activate", "select_across": "1"})
    db.expire_all()
    assert all(item.active for item in db.scalars(select(Item)))


def test_export_csv_neutralises_formulas(panel, db):
    db.add(Item(name="=HYPERLINK(1)", price=1))
    db.commit()
    item = db.scalar(select(Item).where(Item.name.startswith("=")))
    response = panel.post("/item", {"action": "export_csv", "_selected": str(item.id)})
    assert response.headers["content-type"].startswith("text/csv")
    assert "'=HYPERLINK(1)" in response.text


def test_delete_view_requires_post(panel, db):
    item_id = db.scalar(select(Item.id).where(Item.name == "item1"))
    page = panel.get(f"/item/{item_id}/delete")
    assert "Yes, delete" in page.text
    db.expire_all()
    assert db.get(Item, item_id) is not None
    assert panel.post(f"/item/{item_id}/delete").status_code == 303
    db.expire_all()
    assert db.get(Item, item_id) is None


def test_composite_key_with_odd_characters(panel, app, db):
    url = "/natural/a%252Fb%252Cc%2Ceu/change"
    page = panel.get("/natural")
    assert "odd key" in page.text and url in page.text

    # TestClient decodes paths twice, real servers once. httpx's ASGI transport behaves like a server.
    async def fetch():
        cookies = dict(panel.client.cookies.items())
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver", cookies=cookies) as client:
            form = await client.get(f"/admin{url}")
            assert form.status_code == 200
            assert 'name="code"' not in form.text
            token = re.search(r'name="csrf_token" value="([^"]+)"', form.text).group(1)
            return await client.post(f"/admin{url}", data={"csrf_token": token, "label": "renamed"})

    assert asyncio.run(fetch()).status_code == 303
    db.expire_all()
    assert db.get(Natural, ("a/b,c", "eu")).label == "renamed"


def test_garbage_primary_key_is_404(panel):
    assert panel.get("/item/notanumber/change").status_code == 404
    assert panel.get("/item/999999/change").status_code == 404


def test_unknown_model_is_404(panel):
    assert panel.get("/nope").status_code == 404
    assert panel.get("/nope/1/change/extra").status_code == 404


def test_autocomplete_searches(panel):
    data = panel.get("/owner/autocomplete?q=ali").json()
    assert [r["text"] for r in data["results"]] == ["Alice"]


def test_count_follows_list_filters_and_sees_new_rows(panel, db):
    assert panel.get("/item/count").json() == {"total": 7}
    assert panel.get("/item/count?q=item1&page=3&o=-name").json() == {"total": 1}
    db.add(Item(name="from another service", price=1))
    db.commit()
    assert panel.get("/item/count").json() == {"total": 8}


def test_count_needs_view_permission(anon):
    anon.login("viewer")
    assert anon.get("/item/count").status_code == 200
    assert anon.get("/owner/count").status_code == 403


def test_live_updates_notice_only_when_switched_on(panel, monkeypatch):
    assert "data-live-updates" not in panel.get("/item").text
    monkeypatch.setattr(ItemAdmin, "live_updates", 10)
    page = panel.get("/item").text
    assert 'data-live-updates="10"' in page and 'data-total="7"' in page


@pytest.mark.parametrize("value", [0, -5, 1.5, True])
def test_bad_live_updates_fails_on_boot(value):
    site = AdminSite()

    class Broken(ModelAdmin):
        live_updates = value

    site.register(Item, Broken)
    with pytest.raises(ImproperlyConfigured):
        site.mount(FastAPI(), engine=_memory_engine(), auth=StaticAuth({}), secret_key="x" * 40)


def test_credentials_are_not_exposed_by_default(panel):
    assert "secret_token" not in panel.get("/owner/add").text
    assert "created_at" not in panel.get("/item/add").text


def test_posted_field_outside_the_form_is_ignored(panel, db):
    panel.post("/owner/add", {"name": "Mallory", "secret_token": "injected", "id": "777"})
    owner = db.scalar(select(Owner).where(Owner.name == "Mallory"))
    assert owner.secret_token is None and owner.id != 777


def test_dashboard_shows_groups_and_recent_actions(panel):
    panel.post("/owner/add", {"name": "Recent"})
    page = panel.get("/")
    assert "Items" in page.text and "Recent" in page.text


def test_startup_checks_catch_typos():
    site = AdminSite()

    class Broken(ModelAdmin):
        list_display = ("nope",)

    site.register(Item, Broken)
    with pytest.raises(ImproperlyConfigured):
        site.mount(FastAPI(), engine=_memory_engine(), auth=StaticAuth({}), secret_key="x" * 40)


def test_duplicate_or_reserved_slug_is_refused():
    site = AdminSite()
    site.register(Item)
    with pytest.raises(ImproperlyConfigured):
        site.register(Item)

    class LoginAdmin(ModelAdmin):
        slug = "login"

    with pytest.raises(ImproperlyConfigured):
        site.register(Owner, LoginAdmin)


def test_password_helpers():
    encoded = hash_password("s3cret")
    assert encoded.startswith("scrypt$") and "s3cret" not in encoded
    assert verify_password("s3cret", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("s3cret", "garbage")


def _memory_engine():
    return create_engine("sqlite://")
