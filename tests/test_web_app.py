"""Flask app flows: upload gates, job API, artifact downloads, cancel.

`sanity_check` and the worker's `submit` are patched so no real pipeline
subprocess or network call ever runs.
"""

import io
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("flask")

import a2t_web.app as app_module
from a2t_web.app import create_app, human_ts
from a2t_web.config import WebSettings
from a2t_web.db import Database
from a2t_web.jobs import CANCELLED, DONE, FAILED, QUEUED, RUNNING
from a2t_web.sanity import SanityError

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\n%%EOF\n"

REPORT_WITH_DOCX = """# Conversion report — com.docx
Proposal date: **2026-03-01**

## Acts amended
| Act | CELEX (version) | Scope | Identified via |
|---|---|---|---|
| Directive 2003/87/EC | `02003L0087-20240301` | whole act | citation |

## Directive 2003/87/EC
- Base text: `02003L0087-20240301` — Directive 2003/87/EC
- Instructions: 4
- Resolution: 4 resolved
- Changes: 3 inserted, 1 deleted
- Document: `02003L0087-20240301_tracked.docx` — 3 inserted, 1 deleted
"""


def make_settings(tmp_path: Path, **overrides) -> WebSettings:
    values = {
        "data_dir": tmp_path,
        "cache_dir": tmp_path / "cache",
        "max_workers": 1,
        "max_upload_bytes": 1024 * 1024,
        "job_timeout": 3600,
        "job_ttl_hours": 24,
        "poll_seconds": 2,
        "db_path": tmp_path / "a2t.db",
    }
    values.update(overrides)
    return WebSettings(**values)


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    created = []

    def _make(**overrides):
        application = create_app(make_settings(tmp_path, **overrides))
        monkeypatch.setattr(app_module, "sanity_check", lambda _pdf: None)
        monkeypatch.setattr(
            application.extensions["a2t_worker"], "submit", lambda job: None
        )
        created.append(application)
        return application

    yield _make

    for application in created:
        application.extensions["a2t_worker"].shutdown()


@pytest.fixture
def client(make_app):
    return make_app().test_client()


def upload(client, filename: str = "com.pdf", data: bytes = PDF_BYTES) -> str:
    rv = client.post(
        "/jobs",
        data={"pdf": (io.BytesIO(data), filename)},
    )
    assert rv.status_code == 303
    return rv.headers["Location"].rsplit("/", 1)[-1]


def complete_job(client, job_id: str) -> None:
    registry = client.application.extensions["a2t_jobs"]
    outdir = registry.job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text("# report\n")
    registry.transition(job_id, status=DONE)


# ------------------------------------------------------------------- pages


def test_index_page_has_upload_form(client):
    rv = client.get("/")
    assert rv.status_code == 200
    assert b"<form" in rv.data
    assert b'name="pdf"' in rv.data


def test_status_page_shows_job(client):
    job_id = upload(client)
    rv = client.get(f"/jobs/{job_id}")
    assert rv.status_code == 200
    assert b"com.pdf" in rv.data
    assert f"/jobs/{job_id}".encode() in rv.data


def test_status_page_unknown_404(client):
    assert client.get("/jobs/nope").status_code == 404


def test_status_page_no_refresh_when_done(client):
    job_id = upload(client)
    complete_job(client, job_id)
    rv = client.get(f"/jobs/{job_id}")
    assert b'http-equiv="refresh"' not in rv.data


def test_status_page_no_refresh_when_failed(client):
    job_id = upload(client)
    registry_for(client).transition(job_id, status=FAILED, error="boom")
    rv = client.get(f"/jobs/{job_id}")
    assert b'http-equiv="refresh"' not in rv.data


def test_status_page_refresh_when_queued(client):
    job_id = upload(client)
    rv = client.get(f"/jobs/{job_id}")
    assert b'http-equiv="refresh"' in rv.data


def test_status_page_refresh_when_running(client):
    job_id = upload(client)
    registry_for(client).transition(job_id, status=RUNNING)
    rv = client.get(f"/jobs/{job_id}")
    assert b'http-equiv="refresh"' in rv.data


# ---------------------------------------------------------------- timestamps


def test_human_ts_falsy_returns_empty():
    assert human_ts(None) == ""
    assert human_ts(0) == ""
    assert human_ts(False) == ""


def test_human_ts_formats_epoch():
    ts = 1750000000.0
    assert human_ts(ts) == datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    assert human_ts(ts) != str(ts)


def test_status_page_renders_human_timestamp_not_float(client):
    job_id = upload(client)
    registry_for(client).get(job_id).created_at = 1750000000.0
    rv = client.get(f"/jobs/{job_id}")
    assert b"1750000000" not in rv.data
    expected = datetime.fromtimestamp(1750000000.0).strftime("%Y-%m-%d %H:%M:%S")
    assert expected.encode() in rv.data


# ---------------------------------------------------------------- upload


def test_upload_creates_queued_job_and_stores_pdf(client):
    job_id = upload(client)
    registry = client.application.extensions["a2t_jobs"]
    job = registry.get(job_id)
    assert job is not None
    assert job.status == QUEUED
    assert job.pdf_name == "com.pdf"
    assert job.pdf_path.read_bytes() == PDF_BYTES


def test_upload_to_pdf_path_under_data_dir(client, tmp_path):
    job_id = upload(client)
    registry = client.application.extensions["a2t_jobs"]
    job = registry.get(job_id)
    assert job.pdf_path == tmp_path / "uploads" / f"{job_id}.pdf"


def test_upload_rejects_non_pdf_extension(client):
    rv = client.post("/jobs", data={"pdf": (io.BytesIO(b"hello"), "notes.txt")})
    assert rv.status_code == 400
    assert b"PDF" in rv.data
    assert not rv.headers.get("Location")


def test_upload_rejects_bad_magic_and_cleans_up(client, tmp_path):
    rv = client.post("/jobs", data={"pdf": (io.BytesIO(b"not a pdf at all"), "com.pdf")})
    assert rv.status_code == 400
    assert b"not look like a PDF" in rv.data
    assert not list((tmp_path / "uploads").iterdir())


def test_upload_requires_a_file(client):
    rv = client.post("/jobs", data={})
    assert rv.status_code == 400
    assert b"PDF" in rv.data


def test_upload_sanity_error_no_job(client, tmp_path, monkeypatch):
    def reject(_pdf):
        raise SanityError("This does not look like an EU legislative act: nope")

    monkeypatch.setattr(app_module, "sanity_check", reject)
    rv = client.post("/jobs", data={"pdf": (io.BytesIO(PDF_BYTES), "com.pdf")})
    assert rv.status_code == 400
    assert b"nope" in rv.data
    assert not list((tmp_path / "uploads").iterdir())


def test_upload_unexpected_sanity_error_leaves_no_file(client, tmp_path, monkeypatch):
    def crash(_pdf):
        raise RuntimeError("pdftotext exploded")

    monkeypatch.setattr(app_module, "sanity_check", crash)
    rv = client.post("/jobs", data={"pdf": (io.BytesIO(PDF_BYTES), "com.pdf")})
    assert rv.status_code == 500
    assert not list((tmp_path / "uploads").iterdir())


def test_upload_oversized_413(make_app):
    app_ = make_app(max_upload_bytes=100)
    rv = app_.test_client().post(
        "/jobs",
        data={"pdf": (io.BytesIO(b"x" * 500), "com.pdf")},
    )
    assert rv.status_code == 413


# ---------------------------------------------------------------- API status


def test_api_unknown_job_404(client):
    rv = client.get("/api/jobs/does-not-exist")
    assert rv.status_code == 404
    assert rv.get_json()["error"]


def test_api_snapshot_queued(client):
    job_id = upload(client)
    data = client.get(f"/api/jobs/{job_id}").get_json()
    assert data["id"] == job_id
    assert data["status"] == QUEUED
    assert data["pdf_name"] == "com.pdf"
    assert data["error"] is None
    assert data["queue_position"] == 0
    assert data["artifacts"] == []
    assert isinstance(data["created_at"], float)
    assert data["started_at"] is None
    assert data["finished_at"] is None


def test_api_snapshot_running(client):
    job_id = upload(client)
    registry = client.application.extensions["a2t_jobs"]
    registry.transition(job_id, status=RUNNING)
    data = client.get(f"/api/jobs/{job_id}").get_json()
    assert data["status"] == RUNNING
    assert data["started_at"] is not None
    assert data["artifacts"] == []


def test_api_snapshot_done_lists_artifacts(client):
    job_id = upload(client)
    registry = client.application.extensions["a2t_jobs"]
    outdir = registry.job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text("# report\n")
    (outdir / "aaa_tracked.docx").write_bytes(b"docx")
    registry.transition(job_id, status=DONE)
    data = client.get(f"/api/jobs/{job_id}").get_json()
    assert data["status"] == DONE
    names = [a["name"] for a in data["artifacts"]]
    assert "report.md" in names
    assert "aaa_tracked.docx" in names
    assert all(a["download_url"] for a in data["artifacts"])


def test_api_snapshot_done_annotates_title(client):
    job_id = upload(client)
    registry = registry_for(client)
    outdir = registry.job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text(REPORT_WITH_DOCX)
    (outdir / "02003L0087-20240301_tracked.docx").write_bytes(b"docx")
    registry.transition(job_id, status=DONE)
    data = client.get(f"/api/jobs/{job_id}").get_json()
    by_name = {a["name"]: a for a in data["artifacts"]}
    assert (
        by_name["02003L0087-20240301_tracked.docx"]["title"]
        == "Directive 2003/87/EC"
    )
    assert by_name["report.md"]["title"] is None


def test_api_snapshot_done_title_none_without_report_match(client):
    job_id = upload(client)
    complete_job(client, job_id)
    outdir = registry_for(client).job_dir(job_id)
    (outdir / "02003L0087-20240301_tracked.docx").write_bytes(b"docx")
    data = client.get(f"/api/jobs/{job_id}").get_json()
    by_name = {a["name"]: a for a in data["artifacts"]}
    assert by_name["02003L0087-20240301_tracked.docx"]["title"] is None


def test_api_artifacts_empty_when_not_done(client):
    job_id = upload(client)
    registry = client.application.extensions["a2t_jobs"]
    outdir = registry.job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text("# report\n")
    data = client.get(f"/api/jobs/{job_id}").get_json()
    assert data["status"] == QUEUED
    assert data["artifacts"] == []


def test_api_snapshot_failed(client):
    job_id = upload(client)
    registry = client.application.extensions["a2t_jobs"]
    registry.transition(job_id, status=FAILED, error="boom")
    data = client.get(f"/api/jobs/{job_id}").get_json()
    assert data["status"] == FAILED
    assert data["error"] == "boom"
    assert data["artifacts"] == []


# ---------------------------------------------------------------- downloads


def test_download_artifact_attachment(client):
    job_id = upload(client)
    complete_job(client, job_id)
    rv = client.get(f"/api/jobs/{job_id}/artifacts/report.md")
    assert rv.status_code == 200
    assert rv.data == b"# report\n"
    assert "attachment" in rv.headers["Content-Disposition"]


def test_download_docx_uses_friendly_name_and_keeps_disk_file(client):
    job_id = upload(client)
    registry = registry_for(client)
    outdir = registry.job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text(REPORT_WITH_DOCX)
    (outdir / "02003L0087-20240301_tracked.docx").write_bytes(b"docx")
    registry.transition(job_id, status=DONE)
    rv = client.get(f"/api/jobs/{job_id}/artifacts/02003L0087-20240301_tracked.docx")
    assert rv.status_code == 200
    assert rv.data == b"docx"
    disposition = rv.headers["Content-Disposition"]
    assert "Directive 2003-87-EC" in disposition
    assert "(02003L0087-20240301)_tracked.docx" in disposition
    assert (outdir / "02003L0087-20240301_tracked.docx").read_bytes() == b"docx"


def test_download_unknown_title_keeps_disk_name(client):
    job_id = upload(client)
    complete_job(client, job_id)
    rv = client.get(f"/api/jobs/{job_id}/artifacts/report.md")
    assert rv.status_code == 200
    assert "report.md" in rv.headers["Content-Disposition"]


def test_download_before_done_404(client):
    job_id = upload(client)
    outdir = registry_for(client).job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text("# report\n")
    rv = client.get(f"/api/jobs/{job_id}/artifacts/report.md")
    assert rv.status_code == 404


def test_download_unknown_artifact_404(client):
    job_id = upload(client)
    complete_job(client, job_id)
    rv = client.get(f"/api/jobs/{job_id}/artifacts/nope.md")
    assert rv.status_code == 404


def test_download_blocks_path_traversal(client, tmp_path):
    job_id = upload(client)
    complete_job(client, job_id)
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    attacks = [
        "..%2F..%2Fsecret.txt",
        "..%2Fsecret.txt",
        "../secret.txt",
        "..%2F..%2Fetc%2Fpasswd",
        "%2e%2e%2f%2e%2e%2fsecret.txt",
    ]
    for attack in attacks:
        rv = client.get(f"/api/jobs/{job_id}/artifacts/{attack}")
        assert rv.status_code == 404, attack
    assert secret.read_text() == "top secret"


# ------------------------------------------------------------------ cancel


def test_cancel_queued_job(client):
    job_id = upload(client)
    rv = client.post(f"/api/jobs/{job_id}/cancel")
    assert rv.status_code == 200
    assert rv.get_json()["status"] == CANCELLED


def test_cancel_done_job_409(client):
    job_id = upload(client)
    registry = client.application.extensions["a2t_jobs"]
    registry.transition(job_id, status=DONE)
    rv = client.post(f"/api/jobs/{job_id}/cancel")
    assert rv.status_code == 409
    assert rv.get_json()["error"]


def test_cancel_done_job_keeps_artifacts_downloadable(client):
    job_id = upload(client)
    complete_job(client, job_id)
    rv = client.post(f"/api/jobs/{job_id}/cancel")
    assert rv.status_code == 409
    data = client.get(f"/api/jobs/{job_id}").get_json()
    assert data["status"] == DONE
    rv = client.get(f"/api/jobs/{job_id}/artifacts/report.md")
    assert rv.status_code == 200
    assert rv.data == b"# report\n"


def test_cancel_route_does_not_overwrite_done(client, monkeypatch):
    """Even if worker.cancel claims success after the job finished, the route
    must not flip a finished job to CANCELLED."""
    job_id = upload(client)
    complete_job(client, job_id)
    application = client.application
    monkeypatch.setattr(
        application.extensions["a2t_worker"], "cancel", lambda _job_id: True
    )
    rv = client.post(f"/api/jobs/{job_id}/cancel")
    assert rv.status_code == 409
    data = client.get(f"/api/jobs/{job_id}").get_json()
    assert data["status"] == DONE
    rv = client.get(f"/api/jobs/{job_id}/artifacts/report.md")
    assert rv.status_code == 200
    assert rv.data == b"# report\n"


def test_cancel_unknown_job_404(client):
    rv = client.post("/api/jobs/nope/cancel")
    assert rv.status_code == 404
    assert rv.get_json()["error"]


# --------------------------------------------------------------------- log


def test_log_endpoint(client):
    job_id = upload(client)
    outdir = registry_for(client).job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "a2t.log").write_text("line1\nline2\n")
    rv = client.get(f"/api/jobs/{job_id}/log")
    assert rv.status_code == 200
    assert rv.mimetype == "text/plain"
    assert rv.data == b"line1\nline2\n"


def test_log_endpoint_unknown_404(client):
    rv = client.get("/api/jobs/nope/log")
    assert rv.status_code == 404
    assert rv.get_json()["error"]


# ----------------------------------------------------------------- report


def test_report_endpoint_returns_markdown_when_done(client):
    job_id = upload(client)
    registry = registry_for(client)
    outdir = registry.job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text(REPORT_WITH_DOCX)
    registry.transition(job_id, status=DONE)
    rv = client.get(f"/api/jobs/{job_id}/report")
    assert rv.status_code == 200
    assert rv.mimetype == "text/markdown"
    assert rv.data == REPORT_WITH_DOCX.encode()


def test_report_endpoint_not_done_404(client):
    job_id = upload(client)
    registry = registry_for(client)
    outdir = registry.job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_text("# report\n")
    for status in (QUEUED, RUNNING, FAILED, CANCELLED):
        registry.transition(job_id, status=status)
        rv = client.get(f"/api/jobs/{job_id}/report")
        assert rv.status_code == 404
        assert "done" in rv.get_json()["error"]


def test_report_endpoint_unknown_job_404(client):
    rv = client.get("/api/jobs/nope/report")
    assert rv.status_code == 404
    assert rv.get_json()["error"]


def test_report_endpoint_missing_report_404(client):
    job_id = upload(client)
    registry = registry_for(client)
    outdir = registry.job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "aaa_tracked.docx").write_bytes(b"docx")
    registry.transition(job_id, status=DONE)
    rv = client.get(f"/api/jobs/{job_id}/report")
    assert rv.status_code == 404
    assert rv.get_json()["error"] == "Report not found"


def test_report_endpoint_invalid_utf8_report_404(client):
    job_id = upload(client)
    registry = registry_for(client)
    outdir = registry.job_dir(job_id)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "report.md").write_bytes(b"\xff\xfe not utf-8")
    registry.transition(job_id, status=DONE)
    rv = client.get(f"/api/jobs/{job_id}/report")
    assert rv.status_code == 404
    assert rv.get_json()["error"] == "Report not found"


def test_status_page_contains_report_container(client):
    job_id = upload(client)
    rv = client.get(f"/jobs/{job_id}")
    assert rv.status_code == 200
    assert b'id="report-section"' in rv.data
    assert b'id="report-content"' in rv.data


def registry_for(client):
    return client.application.extensions["a2t_jobs"]


# ---------------------------------------------------------------------- auth


def make_auth_client(make_app, admin_user="admin", admin_pw="admin-pw",
                     user="user", user_pw="user-pw"):
    app_ = make_app(
        seed_admin_username=admin_user, seed_admin_password=admin_pw,
        seed_user_username=user, seed_user_password=user_pw,
    )
    return app_.test_client()


def login(client, username: str, password: str, next_url: str | None = None):
    data = {"username": username, "password": password}
    if next_url is not None:
        data["next"] = next_url
    rv = client.post("/login", data=data)
    assert rv.status_code == 302, rv.status_code
    return rv


def test_login_page_served_when_auth_enabled(make_app):
    c = make_auth_client(make_app)
    rv = c.get("/login")
    assert rv.status_code == 200
    assert b'name="username"' in rv.data
    assert b'name="password"' in rv.data


def test_open_mode_has_no_login_requirement(client):
    assert client.get("/").status_code == 200


def test_unauthenticated_page_redirects_to_login(make_app):
    c = make_auth_client(make_app)
    rv = c.get("/")
    assert rv.status_code == 302
    assert rv.headers["Location"].startswith("/login")
    assert "next=/" in rv.headers["Location"]


def test_unauthenticated_api_returns_401_json(make_app):
    c = make_auth_client(make_app)
    rv = c.get("/api/jobs/nope")
    assert rv.status_code == 401
    assert rv.get_json()["error"]


def test_login_wrong_password_rejected(make_app):
    c = make_auth_client(make_app)
    rv = c.post("/login", data={"username": "user", "password": "nope"})
    assert rv.status_code == 401
    assert b"Incorrect username or password." in rv.data
    assert c.get("/").status_code == 302


def test_login_wrong_username_rejected(make_app):
    c = make_auth_client(make_app)
    rv = c.post("/login", data={"username": "unknown", "password": "user-pw"})
    assert rv.status_code == 401
    assert b"Incorrect username or password." in rv.data
    assert c.get("/").status_code == 302


def test_login_user_grants_access(make_app):
    c = make_auth_client(make_app)
    login(c, "user", "user-pw")
    rv = c.get("/")
    assert rv.status_code == 200
    assert b'name="pdf"' in rv.data


def test_login_admin_grants_admin(make_app):
    c = make_auth_client(make_app)
    login(c, "admin", "admin-pw")
    rv = c.get("/admin")
    assert rv.status_code == 200
    assert b"job log" in rv.data


def test_login_redirects_to_next(make_app):
    c = make_auth_client(make_app)
    rv = c.get("/jobs/xyz")
    assert rv.status_code == 302
    assert "next=/jobs/xyz" in rv.headers["Location"]
    rv = login(c, "user", "user-pw", next_url="/jobs/xyz")
    assert rv.headers["Location"] == "/jobs/xyz"


def test_login_next_blocks_open_redirect(make_app):
    c = make_auth_client(make_app)
    rv = c.post(
        "/login",
        data={"username": "user", "password": "user-pw", "next": "//evil.example"},
    )
    assert rv.status_code == 302
    assert rv.headers["Location"] != "//evil.example"
    assert rv.headers["Location"].startswith("/")


def test_user_cannot_access_admin(make_app):
    c = make_auth_client(make_app)
    login(c, "user", "user-pw")
    assert c.get("/admin").status_code == 403


def test_admin_page_shows_job_entries(make_app):
    c = make_auth_client(make_app)
    login(c, "admin", "admin-pw")
    job_id = upload(c)
    complete_job(c, job_id)
    rv = c.get("/admin")
    assert rv.status_code == 200
    assert job_id.encode() in rv.data
    assert b"com.pdf" in rv.data
    assert b"done" in rv.data


def test_admin_page_empty_state(make_app):
    c = make_auth_client(make_app)
    login(c, "admin", "admin-pw")
    rv = c.get("/admin")
    assert rv.status_code == 200
    assert b"No jobs" in rv.data


def test_logout_clears_session(make_app):
    c = make_auth_client(make_app)
    login(c, "user", "user-pw")
    assert c.get("/").status_code == 200
    assert c.get("/logout").status_code == 302
    assert c.get("/").status_code == 302


def test_admin_nav_link_only_for_admin(make_app):
    admin_client = make_auth_client(make_app)
    login(admin_client, "admin", "admin-pw")
    assert b'href="/admin"' in admin_client.get("/").data

    user_client = make_auth_client(make_app)
    login(user_client, "user", "user-pw")
    assert b'href="/admin"' not in user_client.get("/").data


def test_session_cookie_samesite_is_lax(make_app):
    c = make_auth_client(make_app)
    assert c.application.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_unauthenticated_api_endpoints_denied(make_app):
    app_ = make_app(
        seed_admin_username="admin", seed_admin_password="admin-pw",
        seed_user_username="user", seed_user_password="user-pw",
    )
    admin = app_.test_client()
    login(admin, "admin", "admin-pw")
    job_id = upload(admin)
    complete_job(admin, job_id)  # DONE + report.md present: otherwise reachable
    anon = app_.test_client()
    for url in (
        f"/api/jobs/{job_id}/report",
        f"/api/jobs/{job_id}/artifacts/report.md",
        f"/api/jobs/{job_id}/log",
    ):
        rv = anon.get(url)
        assert rv.status_code == 401, url
        assert rv.get_json()["error"]
    rv = anon.post(f"/api/jobs/{job_id}/cancel")
    assert rv.status_code == 401
    assert rv.get_json()["error"]


def test_unauthenticated_upload_redirects_and_creates_no_job(make_app, tmp_path):
    app_ = make_app(
        seed_admin_username="admin", seed_admin_password="admin-pw",
        seed_user_username="user", seed_user_password="user-pw",
    )
    anon = app_.test_client()
    rv = anon.post("/jobs", data={"pdf": (io.BytesIO(PDF_BYTES), "com.pdf")})
    assert rv.status_code == 302
    assert rv.headers["Location"].startswith("/login")
    # the guard rejects before the route runs, so nothing may be stored
    assert not list((tmp_path / "uploads").iterdir())


def test_admin_page_escapes_job_error_and_pdf_name(make_app):
    c = make_auth_client(make_app)
    login(c, "admin", "admin-pw")
    job_id = upload(c, filename="<script>evil.pdf")
    registry_for(c).transition(
        job_id, status=FAILED, error='<script>alert("&")</script>'
    )
    rv = c.get("/admin")
    assert rv.status_code == 200
    assert b"<script>" not in rv.data
    assert b"&lt;script&gt;" in rv.data
    assert b"&amp;" in rv.data


# ----------------------------------------------------- seeding / startup guards


def test_login_hostile_username_returns_generic_401(make_app):
    c = make_auth_client(make_app)
    rv = c.post(
        "/login",
        data={"username": "alice' OR '1'='1", "password": "user-pw"},
    )
    assert rv.status_code == 401
    assert b"Incorrect username or password." in rv.data


def test_seeding_skipped_once_users_exist(make_app, tmp_path):
    # Pre-create a user in the DB that create_app will open: seeding must be
    # skipped entirely, so the seeded credentials never exist.
    db = Database(tmp_path / "a2t.db")
    db.init()
    db.create_user("existing", "pw", "admin")
    app_ = make_app(
        seed_admin_username="admin", seed_admin_password="admin-pw",
        seed_user_username="user", seed_user_password="user-pw",
    )
    db2 = app_.extensions["a2t_db"]
    assert db2.count_users() == 1
    assert db2.get_user_by_username("existing") is not None
    assert db2.get_user_by_username("admin") is None
    assert db2.get_user_by_username("user") is None


def test_seeding_same_admin_and_user_username_keeps_admin(make_app):
    app_ = make_app(
        seed_admin_username="same", seed_admin_password="admin-pw",
        seed_user_username="same", seed_user_password="user-pw",
    )
    db = app_.extensions["a2t_db"]
    assert db.count_users() == 1
    assert db.get_user_by_username("same")["role"] == "admin"
    c = app_.test_client()
    assert c.post(
        "/login", data={"username": "same", "password": "admin-pw"}
    ).status_code == 302
    assert c.post(
        "/login", data={"username": "same", "password": "user-pw"}
    ).status_code == 401


def test_main_exits_cleanly_on_broken_db(monkeypatch, tmp_path, capsys):
    """A database that cannot be opened must fail fast with a one-line
    message (exit 1), not a raw traceback — and must never serve open."""
    bad_path = tmp_path / "db-dir"  # a directory where the DB file should be
    bad_path.mkdir()
    settings = WebSettings(
        data_dir=tmp_path,
        cache_dir=tmp_path / "cache",
        max_workers=1,
        db_path=bad_path,
    )
    monkeypatch.setattr(app_module, "settings_from_env", lambda: settings)
    with pytest.raises(SystemExit) as excinfo:
        app_module.main()
    assert excinfo.value.code == 1
    assert "database" in capsys.readouterr().err