"""Flask app: upload, sanity-gate, background conversion, artifact download.

The pipeline never runs in the Flask process — `POST /jobs` does the cheap
local sanity check synchronously, then hands the job to the background worker
and redirects to a status page that polls the JSON API.
"""

from __future__ import annotations

import atexit
import secrets
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from a2t_web import db as db_module
from a2t_web import storage
from a2t_web.config import WebSettings, settings_from_env
from a2t_web.jobs import CANCELLED, DONE, JobRegistry
from a2t_web.pipeline import artifact_titles, tail
from a2t_web.sanity import SanityError, sanity_check
from a2t_web.users import seed_users
from a2t_web.worker import Worker

ADMIN_LOG_LIMIT = 200


def human_ts(ts: float | None) -> str:
    """Epoch float as a readable local timestamp; "" for None/falsy."""
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def download_name_for(title: str | None, filename: str) -> str:
    """A friendly Content-Disposition name for a docx, or the on-disk name.

    `filename` stays the URL/download key; only the presented download name is
    rewritten. Slashes and backslashes are unsafe in filenames, so they are
    replaced with dashes.
    """
    if not title or not filename.endswith("_tracked.docx"):
        return filename
    version = filename[: -len("_tracked.docx")]
    return f"{title} ({version})_tracked.docx".replace("\\", "-").replace("/", "-")


def create_app(settings: WebSettings | None = None) -> Flask:
    settings = settings or settings_from_env()
    storage.ensure_data_dirs(settings.data_dir)

    db = db_module.Database(settings.db_path)
    db.init()
    seed_users(settings, db)

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = settings.max_upload_bytes
    # Pin SameSite=Lax (per IMPLEMENTATION_PLAN §3.4) instead of relying on
    # the framework default, so the signed session cookie is not sent on
    # cross-site POSTs.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    # Random per-process key unless the operator pins one; a fixed
    # A2T_WEB_SECRET_KEY keeps sessions valid across restarts.
    app.secret_key = settings.secret_key or secrets.token_hex(32)
    # Auth is on iff the users table has any users; an empty table = open mode.
    auth_enabled = db.count_users() > 0

    registry = JobRegistry(db=db)
    worker = Worker(registry, settings)
    app.extensions["a2t_jobs"] = registry
    app.extensions["a2t_worker"] = worker
    app.extensions["a2t_db"] = db

    app.jinja_env.filters["basename"] = storage.safe_basename
    app.jinja_env.filters["human_ts"] = human_ts

    def api_error(message: str, status: int = 404):
        return jsonify({"error": message}), status

    @app.before_request
    def _require_auth():
        if not auth_enabled:
            return None
        if request.path in {"/login", "/logout"} or request.path.startswith("/static"):
            return None
        if session.get("authenticated"):
            return None
        if request.path.startswith("/api/"):
            return api_error("Authentication required", 401)
        return redirect(url_for("login", next=request.path))

    @app.context_processor
    def _inject_auth_state():
        return {"auth_enabled": auth_enabled}

    @app.get("/login")
    def login():
        if not auth_enabled:
            return redirect(url_for("index"))
        return render_template("login.html", error=None)

    @app.post("/login")
    def login_post():
        if not auth_enabled:
            return redirect(url_for("index"))
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = db.verify_login(username, password)
        if user is None:
            return (
                render_template(
                    "login.html", error="Incorrect username or password."
                ),
                401,
            )
        session.clear()
        session["authenticated"] = True
        session["role"] = user["role"]
        session["username"] = user["username"]
        next_url = request.form.get("next")
        if next_url and next_url.startswith("/") and not next_url.startswith("//") \
                and "\\" not in next_url:
            return redirect(next_url)
        return redirect(url_for("index"))

    @app.get("/logout")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/admin")
    def admin():
        if auth_enabled and session.get("role") != "admin":
            abort(403)
        entries = db.list_jobs(limit=ADMIN_LOG_LIMIT)
        return render_template("admin.html", entries=entries)

    def render_index_error(message: str, status: int):
        return (
            render_template(
                "index.html",
                error=message,
                max_upload_bytes=settings.max_upload_bytes,
            ),
            status,
        )

    def gate_upload(pdf_path: Path) -> str | None:
        """File-type and sanity gates; returns an error message or None."""
        try:
            if not storage.looks_like_pdf(pdf_path):
                return "This does not look like a PDF file."
            sanity_check(pdf_path)
        except SanityError as exc:
            return str(exc)
        except Exception:
            pdf_path.unlink(missing_ok=True)
            raise
        return None

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            error=None,
            max_upload_bytes=settings.max_upload_bytes,
        )

    @app.post("/jobs")
    def upload():
        file = request.files.get("pdf")
        if file is None or not file.filename:
            return render_index_error("No file selected — choose a PDF file.", 400)
        if not file.filename.lower().endswith(".pdf"):
            return render_index_error("The file must be a PDF.", 400)
        job_id = secrets.token_urlsafe(12)
        pdf_path = settings.data_dir / "uploads" / f"{job_id}.pdf"
        try:
            storage.save_upload(file, pdf_path, settings.max_upload_bytes)
        except storage.UploadTooLarge as exc:
            pdf_path.unlink(missing_ok=True)
            return render_index_error(str(exc), 400)
        error = gate_upload(pdf_path)
        if error is not None:
            pdf_path.unlink(missing_ok=True)
            return render_index_error(error, 400)
        job = registry.create(
            job_id,
            pdf_path,
            settings.data_dir / "jobs" / job_id,
            storage.safe_basename(file.filename),
        )
        worker.submit(job)
        return redirect(url_for("status", job_id=job.id), 303)

    @app.get("/jobs/<job_id>")
    def status(job_id: str):
        job = registry.state_snapshot(job_id)
        if job is None:
            abort(404)
        return render_template(
            "status.html", job=job, poll_seconds=settings.poll_seconds
        )

    @app.get("/api/jobs/<job_id>")
    def api_status(job_id: str):
        snapshot = registry.state_snapshot(job_id)
        if snapshot is None:
            return api_error("Job not found")
        if snapshot["status"] == DONE:
            job_dir = registry.job_dir(job_id)
            if job_dir is None:
                return api_error("Job not found")
            artifacts = [
                {
                    "name": path.name,
                    "title": artifact_titles(job_dir / "report.md").get(path.name),
                    "download_url": url_for("artifact", job_id=job_id, name=path.name),
                }
                for path in storage.list_artifacts(job_dir)
            ]
        else:
            artifacts = []
        return jsonify({**snapshot, "artifacts": artifacts})

    @app.get("/api/jobs/<job_id>/artifacts/<name>")
    def artifact(job_id: str, name: str):
        snapshot = registry.state_snapshot(job_id)
        if snapshot is None:
            return api_error("Job not found")
        if snapshot["status"] != DONE:
            return api_error("Artifacts are available once the job is done", 404)
        job_dir = registry.job_dir(job_id)
        if job_dir is None:
            return api_error("Job not found")
        path = storage.resolve_artifact(job_dir, name)
        if path is None:
            return api_error("Artifact not found")
        title = artifact_titles(job_dir / "report.md").get(path.name)
        return send_file(
            path,
            as_attachment=True,
            download_name=download_name_for(title, path.name),
            max_age=0,
        )

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_api(job_id: str):
        snapshot = registry.state_snapshot(job_id)
        if snapshot is None:
            return api_error("Job not found")
        if not worker.cancel(job_id):
            return api_error("Job is already finished", 409)
        status = registry.finalize(job_id, status=CANCELLED)
        if status != CANCELLED:
            return api_error("Job is already finished", 409)
        return jsonify(registry.state_snapshot(job_id))

    @app.get("/api/jobs/<job_id>/log")
    def job_log(job_id: str):
        if registry.state_snapshot(job_id) is None:
            return api_error("Job not found")
        job_dir = registry.job_dir(job_id)
        if job_dir is None:
            return api_error("Job not found")
        return (
            tail(job_dir / "a2t.log"),
            200,
            {"Content-Type": "text/plain; charset=utf-8"},
        )

    @app.get("/api/jobs/<job_id>/report")
    def report_api(job_id: str):
        snapshot = registry.state_snapshot(job_id)
        if snapshot is None:
            return api_error("Job not found")
        if snapshot["status"] != DONE:
            return api_error("The report is available once the job is done", 404)
        job_dir = registry.job_dir(job_id)
        if job_dir is None:
            return api_error("Job not found")
        path = storage.resolve_artifact(job_dir, "report.md")
        if path is None:
            return api_error("Report not found")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return api_error("Report not found")
        return (text, 200, {"Content-Type": "text/markdown; charset=utf-8"})

    @app.errorhandler(413)
    def too_large(_exc):
        return render_index_error(
            f"File too large (limit {settings.max_upload_bytes} bytes).", 413
        )

    return app


def main() -> None:
    try:
        settings = settings_from_env()
        app = create_app(settings)
    except ValueError as exc:
        print(f"a2t-web: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (sqlite3.DatabaseError, OSError) as exc:
        # Broken/unwritable data dir or corrupt/unopenable database: fail fast
        # with a one-line message instead of a raw traceback. Nothing serves,
        # so there is no auth bypass.
        print(f"a2t-web: cannot open the database: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    atexit.register(app.extensions["a2t_worker"].shutdown)
    app.run(host=settings.host, port=settings.port, threaded=True)


if __name__ == "__main__":
    main()
