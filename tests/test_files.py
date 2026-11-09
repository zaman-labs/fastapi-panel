from __future__ import annotations

import io
import re

import pytest
from conftest import SECRET, Base, Panel, User, seed
from fastapi import FastAPI
from sqlalchemy import ForeignKey, String, create_engine
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from fastapi_panel import (
    AdminSite,
    FileField,
    ImageField,
    ImproperlyConfigured,
    LocalStorage,
    ModelAdmin,
    ModelAuth,
    TabularInline,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class Doc(Base):
    __tablename__ = "docs"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    photo: Mapped[str | None] = mapped_column(String(255))
    manual: Mapped[str | None] = mapped_column(String(255))


class Page(Base):
    __tablename__ = "doc_pages"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("docs.id"))
    scan: Mapped[str | None] = mapped_column(String(255))


class Cover(Base):
    __tablename__ = "covers"

    id: Mapped[int] = mapped_column(primary_key=True)
    image: Mapped[str] = mapped_column(String(255))


class PageInline(TabularInline):
    model = Page
    extra = 1
    field_overrides = {"scan": ImageField()}


class DocAdmin(ModelAdmin):
    list_display = ("title", "photo", "manual")
    field_overrides = {"photo": ImageField(max_size=1024), "manual": FileField(allowed_extensions=("pdf",))}
    inlines = (PageInline,)


class CoverAdmin(ModelAdmin):
    field_overrides = {"image": ImageField()}


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        seed(session)
    return engine


def mount(engine, root, **mount_options):
    site = AdminSite(storage=LocalStorage(root))
    site.register(Doc, DocAdmin)
    site.register(Cover, CoverAdmin)
    app = FastAPI()
    auth = ModelAuth(User, superuser_field="is_superuser", permissions_field="permissions")
    site.mount(app, engine=engine, auth=auth, secret_key=SECRET, **mount_options)
    return Panel(TestClient(app), engine)


@pytest.fixture
def panel(engine, tmp_path):
    panel = mount(engine, tmp_path)
    panel.login()
    return panel


def add_doc(
    panel, filename="My Photo!.PNG", content=PNG, field="photo", content_type="image/png", url="/doc/add", **data
):
    payload = {"title": "t", "_save": "1", **data}
    return panel.post(url, payload, files={field: (filename, io.BytesIO(content), content_type)})


def only_doc(engine):
    with Session(engine) as session:
        return session.query(Doc).one()


def test_upload_stores_under_a_safe_random_name(panel, engine, tmp_path):
    assert add_doc(panel).status_code == 303
    name = only_doc(engine).photo
    assert re.fullmatch(r"doc/my-photo-[0-9a-f]{12}\.png", name)
    assert (tmp_path / name).read_bytes() == PNG


@pytest.mark.parametrize("filename", ["../../evil.png", "..\\..\\evil.png", "/etc/passwd.png", "a/b/c.png", "‮.png"])
def test_hostile_filenames_stay_inside_the_folder(panel, engine, tmp_path, filename):
    assert add_doc(panel, filename=filename).status_code == 303
    name = only_doc(engine).photo
    assert name.startswith("doc/") and ".." not in name and name.count("/") == 1
    assert (tmp_path / name).resolve().is_relative_to(tmp_path.resolve())


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("evil.svg", b"<svg onload=alert(1)>", "file types"),
        ("evil.html", b"<script>", "file types"),
        ("evil.php.exe", PNG, "file types"),
        ("fake.png", b"<script>alert(1)</script>", "valid image"),
        ("fake.jpg", PNG, "valid image"),
        ("big.png", PNG + b"\0" * 2000, "too large"),
    ],
)
def test_bad_images_are_rejected(panel, engine, tmp_path, filename, content, message):
    page = add_doc(panel, filename=filename, content=content)
    assert page.status_code == 200 and message in page.text
    assert not list(tmp_path.rglob("*.*"))


def test_a_name_that_is_only_an_extension_is_rejected(panel):
    assert "file types" in add_doc(panel, filename=".png").text


def test_file_field_only_takes_its_extensions(panel):
    assert "file types: pdf" in add_doc(panel, filename="a.exe", field="manual").text
    assert add_doc(panel, filename="a.pdf", content=b"%PDF", field="manual").status_code == 303


def file_links(html):
    return re.findall(r'(/admin/files/[^"]+)"', html)


def test_images_are_served_inline_and_locked_down(panel):
    add_doc(panel)
    response = panel.client.get(file_links(panel.get("/doc").text)[0])
    assert response.status_code == 200 and response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].startswith("inline")
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_other_files_always_download(panel):
    add_doc(panel, filename="a.pdf", content=b"%PDF", field="manual")
    response = panel.client.get(file_links(panel.get("/doc").text)[0])
    assert response.headers["content-disposition"].startswith("attachment")


def test_list_shows_a_thumbnail_and_the_form_shows_the_current_file(panel):
    add_doc(panel)
    assert 'class="thumb"' in panel.get("/doc").text
    form = panel.get("/doc/1/change").text
    assert 'class="thumb-large"' in form and "photo-clear" in form


def test_files_need_a_login_and_a_valid_link(engine, tmp_path):
    panel = mount(engine, tmp_path)
    panel.login()
    add_doc(panel)
    url = file_links(panel.get("/doc").text)[0]
    assert panel.client.get(url[:-2] + "xx").status_code == 404
    assert panel.client.get("/admin/files/%ff%fe.bad").status_code == 404
    panel.post("/logout")
    assert panel.client.get(url, follow_redirects=False).status_code == 303


def test_links_only_work_for_whoever_they_were_made_for(engine, tmp_path):
    panel = mount(engine, tmp_path)
    panel.login()
    add_doc(panel)
    url = file_links(panel.get("/doc").text)[0]
    other = mount(engine, tmp_path)
    other.login("viewer")
    assert other.get("/doc").status_code == 403
    response = other.client.get(url)
    assert response.status_code == 404 and response.content != PNG
    assert panel.client.get(url).content == PNG


def test_links_cant_reach_outside_the_folder(engine, tmp_path):
    (tmp_path.parent / "secret.txt").write_text("secret")
    site = AdminSite(storage=LocalStorage(tmp_path))
    site.register(Doc, DocAdmin)
    app = FastAPI()
    site.mount(app, engine=engine, auth=ModelAuth(User, superuser_field="is_superuser"), secret_key=SECRET)
    panel = Panel(TestClient(app), engine)
    panel.login()
    token = site.file_signer.dumps({"name": "../secret.txt", "uid": "1", "exp": 9_999_999_999})
    assert panel.get(f"/files/{token}").status_code == 404


def test_session_tokens_dont_work_as_file_links(engine, tmp_path):
    site = AdminSite(storage=LocalStorage(tmp_path))
    site.register(Doc, DocAdmin)
    app = FastAPI()
    site.mount(app, engine=engine, auth=ModelAuth(User, superuser_field="is_superuser"), secret_key=SECRET)
    panel = Panel(TestClient(app), engine)
    panel.login()
    token = site.signer.dumps({"name": "x.png", "exp": 9_999_999_999})
    assert panel.get(f"/files/{token}").status_code == 404


def test_saving_without_a_new_file_keeps_the_old_one(panel, engine):
    add_doc(panel)
    before = only_doc(engine).photo
    panel.post("/doc/1/change", {"title": "renamed", "_save": "1"})
    assert only_doc(engine).photo == before and only_doc(engine).title == "renamed"


def test_remove_clears_the_column(panel, engine):
    add_doc(panel)
    panel.post("/doc/1/change", {"title": "t", "photo-clear": "on", "_save": "1"})
    assert only_doc(engine).photo is None


def test_replacing_changes_the_name(panel, engine):
    add_doc(panel)
    before = only_doc(engine).photo
    add_doc(panel, filename="other.png", url="/doc/1/change")
    assert only_doc(engine).photo != before


def test_required_file(panel, engine):
    page = panel.post("/cover/add", {"_save": "1"})
    assert "Choose a file" in page.text
    assert (
        panel.post("/cover/add", {"_save": "1"}, files={"image": ("c.png", io.BytesIO(PNG), "image/png")}).status_code
        == 303
    )
    assert "Choose a file" not in panel.post("/cover/1/change", {"_save": "1"}).text


def test_invalid_form_stores_no_file(panel, tmp_path):
    page = add_doc(panel, title="")
    assert page.status_code == 200
    assert not list(tmp_path.rglob("*.*"))


def test_inline_uploads(panel, engine):
    add_doc(panel)
    response = panel.post(
        "/doc/1/change",
        {"title": "t", "page-TOTAL": "1", "page-0-_pk": "", "_save": "1"},
        files={"page-0-scan": ("s.png", io.BytesIO(PNG), "image/png")},
    )
    assert response.status_code == 303
    with Session(engine) as session:
        assert session.query(Page).one().scan.startswith("page/")


def test_viewers_see_links_but_no_input(engine, tmp_path):
    panel = mount(engine, tmp_path)
    panel.login()
    add_doc(panel)
    panel.post("/logout")
    with Session(engine) as session:
        password = session.query(User).filter_by(username="root").one().password
        session.add(User(username="reader", password=password, permissions=["doc.view"]))
        session.commit()
    panel.login("reader")
    page = panel.get("/doc/1/change").text
    assert 'type="file"' not in page and "/admin/files/" in page


def test_file_field_without_storage_fails_at_startup(engine):
    site = AdminSite()
    site.register(Doc, DocAdmin)
    with pytest.raises(ImproperlyConfigured, match="storage"):
        site.mount(FastAPI(), engine=engine, auth=ModelAuth(User), secret_key=SECRET)


def test_oversized_requests_are_refused_even_without_content_length(engine, tmp_path):
    panel = mount(engine, tmp_path, max_body_size=1000)
    panel.login()

    def stream():
        yield b"a" * 800
        yield b"b" * 800

    response = panel.client.post(
        "/admin/doc/add", content=stream(), headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert response.status_code == 413
