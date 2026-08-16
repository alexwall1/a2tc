# Implementation Plan: password gate (user + admin) and durable /admin job log

This plan adds two features to the existing self-hosted Flask web app:

1. A **simple password gate** for the whole app: the operator puts a user
   password in `.secrets/user-password` and an admin password in
   `.secrets/admin-password`; the app enforces a login session on all pages
   **and** the JSON API. The same login form accepts either password and stores
   the role (`user` | `admin`) in the Flask session cookie. `/admin` requires
   role `admin`.
2. A **server-rendered admin page at `GET /admin`** showing a durable,
   append-only log of every job that has been run (id, PDF filename, status,
   created/started/finished timestamps, error) that survives restarts.

Design only. The implementer executes the steps in **Implementation sequence**.
No new dependencies (no flask-login, no flask-httpauth — the `web` extra stays
`flask>=3.0` only). Existing endpoints and tests stay intact: when **no
password is configured** the app runs in an open "dev mode" identical to today,
so the 40+ existing `tests/test_web_app.py` tests pass unchanged.

---

## 1. Objective

1. **Auth mechanism**: Flask's built-in signed session cookie
   (`session`) + a `before_request` guard. `app.secret_key` is a random
   per-process value (`secrets.token_hex(32)`) unless `A2T_WEB_SECRET_KEY` is
   set. A `GET /login` form accepts **either** the user password (role
   `user`) or the admin password (role `admin`), compared in constant time with
   `hmac.compare_digest`, and stores `session["authenticated"]` +
   `session["role"]`. `GET /logout` clears the session. `POST /login`
   redirects to a validated `next` parameter or `/`.
2. **Enforcement scope**: enforced **only when a password is configured**
   (`bool(user_password or admin_password)`). In open mode there is no guard
   and no usable login page. When enabled:
   - `/api/*` without a session → `401` JSON via the existing `api_error`.
   - any other page (including `/admin`) without a session → `302` to
     `/login?next=<path>`.
   - `/static`, `/login`, `/logout` are exempt.
   - `/admin` additionally requires `session["role"] == "admin"`; a logged-in
     `user` gets `403`.
3. **Durable job log**: a tab-separated append-only file at
   `<data_dir>/jobs.log`. `JobRegistry.create` appends a "create" record;
   every terminal transition (`DONE`/`FAILED`/`CANCELLED`) through
   `transition`/`finalize` appends a "terminal" record. `GET /admin` renders
   the file contents (one row per job, newest first, capped at 200), surviving
   restarts and even `sweep`-deleted jobs.
4. **Secrets hygiene**: passwords are read only from the secrets dir
   (`A2T_WEB_SECRETS_DIR`, default `.secrets`) at startup; missing or
   empty-after-trim files **fail closed with a clear `ValueError`**. `.secrets/`
   is added to `.gitignore`. Passwords never appear in HTML, logs, or error
   messages.

Non-goals: no user management, no registration, no hashing/DB, no
remember-me, no API tokens, no LDAP/SSO, no live in-memory registry view on
`/admin` (the durable file is authoritative), no JS changes (the existing
polling code already tolerates non-ok `/api` responses).

---

## 2. Current system (facts verified from the code)

- `a2t_web/config.py`: frozen `WebSettings` dataclass (data_dir,
  max_upload_bytes, cache_dir, max_workers, job_timeout, job_ttl_hours,
  poll_seconds, host, port) + `settings_from_env()` reading `A2T_WEB_*`/`A2T_CACHE`.
  `_INT_FIELDS` = int-coerced keys; `_ENV_FOR` maps key→env var; `max_workers>1`
  raises. **No secrets handling today.**
- `a2t_web/app.py`: `create_app(settings=None)` → `ensure_data_dirs`, `Flask`,
  `JobRegistry()` (no args), `Worker(registry, settings)`, filters
  (`basename`, `human_ts`). Routes: `GET /` (`index`), `POST /jobs`
  (`upload`), `GET /jobs/<id>` (`status`), `GET /api/jobs/<id>` (`api_status`),
  `GET /api/jobs/<id>/artifacts/<name>` (`artifact`), `POST /api/jobs/<id>/cancel`
  (`cancel_api`), `GET /api/jobs/<id>/log` (`job_log`), `GET /api/jobs/<id>/report`
  (`report_api`), `errorhandler(413)`. Errors via `api_error(message, status)`
  (JSON). `main()` calls `settings_from_env()` + `create_app` + `app.run`.
  No `session`, no `secret_key`, no `before_request`.
- `a2t_web/jobs.py`: `Job` dataclass (id, pdf_path, outdir, pdf_name, status,
  error, created_at, started_at, finished_at, proc, cancelled);
  `JobRegistry` with `_lock`, `_jobs`; `create`, `get`, `state_snapshot`,
  `transition`, `finalize`, `position`, `set_proc`, `was_cancelled`,
  `request_cancel`, `active_procs`, `job_dir`, `sweep`. `finalize` refuses to
  overwrite a terminal state. `JobRegistry()` is constructed with **no args**
  in `create_app` and in every `test_web_jobs.py` test.
- `a2t_web/worker.py`: `Worker._run` drives the lifecycle:
  `transition(RUNNING)` then `finalize(CANCELLED|DONE|FAILED)`.
  `Worker.cancel` calls `request_cancel` + `finalize(CANCELLED)`.
  `_failure_error` returns the log tail (can contain newlines).
- `a2t_web/storage.py`: `ensure_data_dirs` creates `uploads/` + `jobs/` only.
  `safe_basename` strips control characters (`\x00\r\n\t`) from display names.
- `a2t_web/templates/base.html`: minimal shell, **no nav**, no session usage.
  `index.html` / `status.html` extend it. `status.html` uses the `human_ts`
  filter already.
- `a2t_web/static/app.js`: polling tolerates non-ok `/api` responses
  (`refreshStatus` returns null, `refreshLog` returns ""). **No change needed.**
- `a2t_web/static/style.css`: `:root` variables, `.job-meta`, `.artifacts`,
  `.log`, `.report*` styles.
- `tests/test_web_app.py`: `make_settings(tmp_path, **overrides)` builds
  `WebSettings` directly (no env, no `.secrets`); `make_app` fixture →
  `create_app(make_settings(tmp_path, **overrides))`; `client` fixture uses
  the open-mode app. 40+ tests call `client.get/post` directly.
- `tests/test_web_config.py`: tests `settings_from_env()` with env vars; the
  defaults test deletes the `_ALL_ENV` vars. **Will break** unless the new
  password-file reading is given a tmp secrets dir.
- `tests/test_web_jobs.py`: `registry` fixture = `JobRegistry()` (no log).
  Worker lifecycle tests drive transitions through `registry`.
- `.gitignore`: ignores `.openrouter_key`, `/webapp-data/`, `*.docx`, etc.
  **`.secrets/` is NOT ignored today** (untracked; would be committed by
  mistake).
- `.secrets/` contains only `openrouter-key` (opencode's own; NEVER read,
  log, print, modify, or commit it).

---

## 3. Proposed design

### 3.1 Configuration (`a2t_web/config.py`)

Add three fields to the frozen `WebSettings` dataclass (defaults keep the
existing 40+ tests and any direct `WebSettings(...)` construction working):

```python
secrets_dir: Path = Path(".secrets")
user_password: str = ""
admin_password: str = ""
secret_key: str | None = None          # Flask session signing; None → random per process
```

`_ENV_FOR` additions:

| key         | env var               | type  |
|-------------|-----------------------|-------|
| `secrets_dir` | `A2T_WEB_SECRETS_DIR` | Path (default `.secrets`) |
| `secret_key`  | `A2T_WEB_SECRET_KEY`  | str, optional |

`settings_from_env()` flow:

1. Parse env as today (add `secrets_dir` to the Path coercion branch:
   `elif key in {"data_dir", "cache_dir", "secrets_dir"}`; `secret_key` is a
   plain str).
2. After coercing `values`, determine the secrets dir:
   `secrets_dir = values.get("secrets_dir", Path(".secrets"))`.
3. Read the two password files with a private helper:

```python
def _read_secret(secrets_dir: Path, name: str) -> str:
    path = secrets_dir / name
    if not path.is_file():
        raise ValueError(
            f"Missing password file {path}. Create {secrets_dir / 'user-password'} and "
            f"{secrets_dir / 'admin-password'} to enable access control, then restart."
        )
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"Password file {path} is empty; set a non-empty password.")
    return value
```

   Then `values["user_password"] = _read_secret(secrets_dir, "user-password")`
   and `values["admin_password"] = _read_secret(secrets_dir, "admin-password")`.
   Reading is the **only** filesystem interaction — never create/write the
   files, never invent a password.
4. Build `settings = WebSettings(**values)`, keep the existing `max_workers`
   invariant check.

Consequences (intended, fail-closed):

- `settings_from_env()` **always** returns non-empty passwords (or raises).
  A fresh checkout running `a2t-web` will refuse to start until the operator
  creates both files. This is deliberate: the feature is "protect the app".
- Open/dev mode remains reachable only when the caller constructs
  `WebSettings` directly with empty passwords (exactly what the existing app
  tests do).
- Passwords never appear in exception text — only file paths.

### 3.2 Durable job log — new module `a2t_web/joblog.py`

A small, dependency-free module with the format + IO half. The registry (3.3)
churns it; the admin route reads it.

**File location**: `<data_dir>/jobs.log` (data dir is already runtime data;
already gitignored via `/webapp-data/`). Parent dirs exist because
`ensure_data_dirs` ran.

**Row format** — TSV, one record per line, 9 fields, escaped safely:

```
ts  event  job_id  pdf_name  status  created_at  started_at  finished_at  error
```

- `ts`: event timestamp as `str(float(time.time()))`.
- `event`: `"create"` | `"terminal"`.
- `pdf_name`: the display name from `storage.safe_basename` (already
  control-char-free, but escape anyway).
- `status`: job status at the moment of the event (`queued` on create; the
  terminal status on terminal).
- `created_at` / `started_at` / `finished_at`: `str(float)` or `""` when
  unset.
- `error`: `job.error` or `""`.

**Escaping** (handles newlines in error tails, tabs, backslashes):

```python
def _escape(s: str) -> str:
    return (s.replace("\\", "\\\\")
             .replace("\t", "\\t")
             .replace("\n", "\\n")
             .replace("\r", "\\r"))

def _unescape(s: str) -> str:
    # process left to right: \\ → \, \t → tab, \n → LF, \r → CR
```

**Public API** (all duck-typed on `Job` — `joblog.py` must NOT import
`jobs.py`):

```python
_FIELDS = ("ts", "event", "job_id", "pdf_name", "status",
           "created_at", "started_at", "finished_at", "error")

def create_record(job) -> dict:      # event="create", status=job.status (queued)
def terminal_record(job) -> dict:    # event="terminal", status=job.status, finished_at set

def format_row(record: dict) -> str: # escape + tab-join + "\n"
def parse_line(line: str) -> dict | None:
    # strip trailing newline; split on "\t"; require exactly len(_FIELDS);
    # unescape text fields; numeric fields (ts/created_at/started_at/finished_at)
    # → float(value) if truthy else None (try/except → None on garbage).
    # Return None for malformed lines (skip defensively).

def append_log(path: Path, record: dict) -> None:
    # path.parent.mkdir(parents=True, exist_ok=True)
    # with path.open("a", encoding="utf-8") as f: f.write(format_row(record))
    # OSError propagates: if we cannot persist the runtime log, fail loudly.

def read_entries(path: Path, limit: int | None = None) -> list[dict]:
    # missing file → []
    # read all lines, parse (skip None), keep the LAST record per job_id
    #   (later lines override earlier → a finished job shows its terminal
    #    record; a never-finished job shows its create record),
    # sort newest-first by ts (desc), cap at `limit`, return list of dicts
    #   with float/None timestamps ready for the human_ts filter.
```

Why TSV+escape instead of JSON-lines: the requirement asks for "tab or CSV
separated … escaped safely", keeps the file human-readable for an operator
(`cat jobs.log`), and the escape pair is trivial to unit-test. Why a separate
module: the format/IO half is reused by both `jobs.py` (write) and `app.py`
(read), and a pure module is directly testable without a Flask app.

### 3.3 Registry hooks (`a2t_web/jobs.py`)

`JobRegistry.__init__` gains an **optional** `log_path`:

```python
def __init__(self, log_path: Path | None = None) -> None:
    self._lock = threading.Lock()
    self._jobs: dict[str, Job] = {}
    self._log_path = log_path
```

No other call site changes: `JobRegistry()` (tests, and the default) simply
writes nothing.

Hooks (write **inside** the existing lock, after the state mutation, so the
file ordering matches state changes; appends are single small writes):

- `create`: after `self._jobs[job_id] = job`,
  `self._log_event(job, create_record(job))`.
- `transition`: after the existing mutation, only when
  `status in TERMINAL_STATUSES` → `self._log_event(job, terminal_record(job))`.
  (Non-terminal RUNNING is intentionally NOT logged — the file stays small and
  matches the requirement "create and terminal transitions".)
- `finalize`: after setting the terminal state (inside the lock, before
  `return status`), `self._log_event(job, terminal_record(job))`. The
  early-return branch for an already-terminal job logs **nothing**, so a late
  cancel cannot double-log a finished job.

```python
def _log_event(self, job: Job, record: dict) -> None:
    if self._log_path is not None:
        append_log(self._log_path, record)
```

Records are captured after mutation, so they carry the authoritative status /
`finished_at` / `error`. `sweep` does not log (the durable record stays — that
is the point of durability).

### 3.4 Auth + admin routes (`a2t_web/app.py`)

New imports: `import hmac`, `from flask import ..., session, ...` (add
`session` to the existing import list), `from a2t_web import joblog, storage`.

In `create_app`:

```python
app.secret_key = settings.secret_key or secrets.token_hex(32)
auth_enabled = bool(settings.user_password or settings.admin_password)
job_log_path = settings.data_dir / "jobs.log"
registry = JobRegistry(log_path=job_log_path)
```

Flask session defaults are sufficient and must not be weakened:
`SESSION_COOKIE_HTTPONLY=True` (default), `SameSite=Lax` (Flask 2.3+ default).
Do not set `SESSION_COOKIE_SECURE` — the app serves plain HTTP on 127.0.0.1.

**Guard** (register after `api_error` is defined, so the closure exists):

```python
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
```

Path-based exemption (not endpoint names) avoids coupling to route-function
names. Unknown non-API paths bounce to login too — harmless.

**Login** (register both GET and POST for `/login`):

```python
@app.get("/login")
def login():
    if not auth_enabled:
        return redirect(url_for("index"))
    return render_template("login.html", error=None)

@app.post("/login")
def login_post():
    if not auth_enabled:
        return redirect(url_for("index"))
    password = request.form.get("password", "")
    pw = password.encode("utf-8")
    if hmac.compare_digest(pw, settings.user_password.encode("utf-8")):
        role = "user"
    elif hmac.compare_digest(pw, settings.admin_password.encode("utf-8")):
        role = "admin"
    else:
        return render_template("login.html", error="Incorrect password."), 401
    session.clear()
    session["authenticated"] = True
    session["role"] = role
    next_url = request.form.get("next")
    if next_url and next_url.startswith("/") and not next_url.startswith("//") \
            and "\\" not in next_url:
        return redirect(next_url)
    return redirect(url_for("index"))
```

- `hmac.compare_digest` on UTF-8 bytes works with arbitrary (non-ASCII)
  passwords; both comparisons run for a wrong password (the tiny
  "matched-user vs matched-admin" timing difference is inherent to two
  passwords and not a meaningful leak for this threat model).
- Generic error message `"Incorrect password."` — never echo the input, never
  log it.
- `next` is restricted to same-origin absolute paths (rejects `//host` and
  backslash tricks) to prevent open redirect.

**Logout**:

```python
@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))
```

**Admin**:

```python
@app.get("/admin")
def admin():
    if auth_enabled and session.get("role") != "admin":
        abort(403)
    entries = joblog.read_entries(job_log_path, limit=ADMIN_LOG_LIMIT)
    return render_template("admin.html", entries=entries)
```

with module-level `ADMIN_LOG_LIMIT = 200`.

- In auth mode an unauthenticated visitor never reaches the route (guard
  redirects to login); a logged-in `user` gets 403; an `admin` renders.
- In open mode the route renders (no role to check; the whole app is open
  anyway, so the job log is no more sensitive than the rest).

**Template state** (so `base.html` can conditionally render nav):

```python
@app.context_processor
def _inject_auth_state():
    return {"auth_enabled": auth_enabled}
```

`session` is already a Jinja global; the nav uses it directly.

### 3.5 Templates

**`a2t_web/templates/base.html`** — add a nav strip inside `<main class="container">`
(no links shown in open mode / for plain users, so existing page-content tests
are unaffected):

```html
{% if auth_enabled or session.get("role") == "admin" %}
<nav class="topnav">
  <a href="{{ url_for('index') }}">Upload</a>
  {% if session.get("role") == "admin" %}<a href="{{ url_for('admin') }}">Admin</a>{% endif %}
  {% if session.get("authenticated") %}<a href="{{ url_for('logout') }}">Log out</a>{% endif %}
</nav>
{% endif %}
```

**`a2t_web/templates/login.html`** (new) — extends base; password input styled
to match; carries the `next` hidden field from `request.args.get("next", "")`
(rendered escaped by Jinja automatically):

```html
{% extends "base.html" %}
{% block title %}Sign in — A2T Web{% endblock %}
{% block content %}
  <h1>Sign in</h1>
  {% if error %}<p class="error" role="alert">{{ error }}</p>{% endif %}
  <form method="post" action="{{ url_for('login') }}" class="login-form">
    <input type="hidden" name="next" value="{{ request.args.get('next', '') }}">
    <label for="password">Password</label>
    <input type="password" id="password" name="password" autofocus required>
    <button type="submit">Sign in</button>
  </form>
  <p class="muted">Administrators: enter the admin password to reach the admin page.</p>
{% endblock %}
```

**`a2t_web/templates/admin.html`** (new) — server-rendered table; every field
rendered through Jinja autoescaping (`{{ }}`), never `|safe`; reuses the
existing `human_ts` filter (float/None timestamps come from `read_entries`):

```html
{% extends "base.html" %}
{% block title %}Admin — job log — A2T Web{% endblock %}
{% block content %}
  <p><a href="{{ url_for('index') }}">&larr; Back</a></p>
  <h1>Admin — job log</h1>
  {% if entries %}
  <table class="joblog">
    <thead>
      <tr><th>Job ID</th><th>File</th><th>Status</th><th>Created</th><th>Started</th><th>Finished</th><th>Error</th></tr>
    </thead>
    <tbody>
      {% for e in entries %}
      <tr>
        <td><code>{{ e.job_id }}</code></td>
        <td>{{ e.pdf_name }}</td>
        <td class="status">{{ e.status }}</td>
        <td>{{ e.created_at | human_ts }}</td>
        <td>{{ e.started_at | human_ts }}</td>
        <td>{{ e.finished_at | human_ts }}</td>
        <td class="error">{{ e.error }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% else %}
  <p>No jobs have been run yet.</p>
  {% endif %}
{% endblock %}
```

### 3.6 Styles (`a2t_web/static/style.css`)

Append (using existing `:root` variables):

```css
.topnav { margin-bottom: 1rem; }
.topnav a { margin-right: 1rem; }

.login-form { max-width: 24rem; }
.login-form input[type="password"] { width: 100%; padding: 0.4rem; margin-top: 0.25rem; }

.joblog { border-collapse: collapse; width: 100%; background: #ffffff; }
.joblog th, .joblog td { border: 1px solid var(--border); padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; font-size: 0.9rem; }
.joblog th { background: rgba(0, 0, 0, 0.03); }
.joblog td.error { color: var(--error); }
.joblog code { font-size: 0.85em; }
.muted { opacity: 0.7; }
```

### 3.7 `.gitignore`

Add (protects both the new password files and the existing
`.secrets/openrouter-key`):

```
# Local secrets: web-app user/admin passwords (and opencode's own key)
.secrets/
```

### 3.8 `main()` friendly failure

In `main()`, wrap `settings_from_env()` so a missing/empty password file prints
a clear message instead of a raw traceback:

```python
def main() -> None:
    try:
        settings = settings_from_env()
    except ValueError as exc:
        print(f"a2t-web: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    ...
```

(Add `import sys`.) `create_app(settings)` path is unchanged; tests call
`settings_from_env()` directly and still see the `ValueError`.

### 3.9 README

Update the **Web app** section:

- Document that `a2t-web` now requires `.secrets/user-password` and
  `.secrets/admin-password` next to where the app is started (create them with
  e.g. `printf 'mypassword' > .secrets/user-password`), that both passwords are
  required, and that startup fails fast if either file is missing/empty.
- Document `/login`, `/logout`, and `/admin` (admin password required).
- Document `A2T_WEB_SECRETS_DIR` (default `.secrets`) and the optional
  `A2T_WEB_SECRET_KEY` (fixed value keeps sessions valid across restarts;
  default is a fresh random key per process).
- Update the "Bind to 127.0.0.1 only" bullet: with passwords configured the
  app is no longer unauthenticated on the LAN, but localhost-only binding is
  still recommended; keep the note that cookies are signed with a per-process
  key by default.

---

## 4. Files affected

### Files to modify

**`/home/alexander-wall/Dokument/a2tc-web/.gitignore`**
- Add `.secrets/` (see 3.7). Do not remove any existing entries.

**`/home/alexander-wall/Dokument/a2tc-web/a2t_web/config.py`**
- Symbols: `WebSettings`, `settings_from_env`, `_ENV_FOR`, new module-level
  `_read_secret`.
- Add fields `secrets_dir: Path = Path(".secrets")`, `user_password: str = ""`,
  `admin_password: str = ""`, `secret_key: str | None = None`.
- `_ENV_FOR` gains `"secrets_dir": "A2T_WEB_SECRETS_DIR"` and
  `"secret_key": "A2T_WEB_SECRET_KEY"`; Path coercion set gains `secrets_dir`.
- `settings_from_env` reads both password files after env coercion, before
  constructing `WebSettings` (see 3.1). Update the module docstring/env
  comment with the new env vars.

**`/home/alexander-wall/Dokument/a2tc-web/a2t_web/jobs.py`**
- `JobRegistry.__init__(self, log_path: Path | None = None)`.
- `create`: append `create_record(job)` when `log_path` set.
- `transition`: append `terminal_record(job)` when the new status is terminal.
- `finalize`: append `terminal_record(job)` when it actually writes a terminal
  state (not on the early-return path).
- New private `_log_event(self, job, record)`. Import
  `from a2t_web.joblog import append_log, create_record, terminal_record`.

**`/home/alexander-wall/Dokument/a2tc-web/a2t_web/app.py`**
- Imports: `hmac`, `sys`, `session` (add to the flask import list),
  `from a2t_web import joblog, storage`.
- Module constant `ADMIN_LOG_LIMIT = 200`.
- `create_app`: `app.secret_key`, `auth_enabled`, `job_log_path`,
  `JobRegistry(log_path=job_log_path)`; `before_request` guard; `GET/POST
  /login`; `GET /logout`; `GET /admin`; context processor `_inject_auth_state`.
- `main()`: friendly `ValueError` handling (3.8).
- All existing routes unchanged.

**`/home/alexander-wall/Dokument/a2tc-web/a2t_web/templates/base.html`**
- Add the conditional `<nav class="topnav">` block (3.5). Everything else
  unchanged.

**`/home/alexander-wall/Dokument/a2tc-web/a2t_web/static/style.css`**
- Append `.topnav`, `.login-form`, `.joblog`, `.muted` rules (3.6).

**`/home/alexander-wall/Dokument/a2tc-web/tests/test_web_config.py`**
- `_ALL_ENV` gains `"A2T_WEB_SECRETS_DIR"` (and `"A2T_WEB_SECRET_KEY"`).
- Add an autouse fixture that creates a tmp secrets dir with
  `user-password`/`admin-password` files and sets `A2T_WEB_SECRETS_DIR`, so all
  existing `settings_from_env()` tests keep passing.
- New tests (section 6.2).

**`/home/alexander-wall/Dokument/a2tc-web/tests/test_web_app.py`**
- Keep all existing tests (open mode). Add an auth section (section 6.1)
  using `make_app(user_password=..., admin_password=...)` and dedicated test
  clients. `make_settings` needs **no** change (new fields have defaults).

**`/home/alexander-wall/Dokument/a2tc-web/tests/test_web_jobs.py`**
- Add job-log integration tests (section 6.3). Existing tests unchanged
  (`JobRegistry()` writes nothing).

**`/home/alexander-wall/Dokument/a2tc-web/README.md`**
- Document password files, `/admin`, and the new env vars (3.9).

### Files to create

**`/home/alexander-wall/Dokument/a2tc-web/a2t_web/joblog.py`** — format/IO
module (3.2).

**`/home/alexander-wall/Dokument/a2tc-web/a2t_web/templates/login.html`** —
login form (3.5).

**`/home/alexander-wall/Dokument/a2tc-web/a2t_web/templates/admin.html`** —
job-log table (3.5).

**`/home/alexander-wall/Dokument/a2tc-web/tests/test_web_joblog.py`** — pure
unit tests for `joblog` (section 6.4).

### Explicitly NOT changed

- `a2t_web/static/app.js` (polling already tolerates 401s), `worker.py`,
  `storage.py`, `status.html`, `index.html`, `opencode.json`, `prompts/*.md`,
  `.secrets/openrouter-key` (never read/print/modify), stale `build/lib/…`
  copies.

---

## 5. Implementation sequence

1. **`.gitignore`**: add `.secrets/`.
2. **`a2t_web/joblog.py`** (new): `_escape`, `_unescape`, `_FIELDS`,
   `create_record`, `terminal_record`, `format_row`, `parse_line`,
   `append_log`, `read_entries`. Unit-test round-trips before wiring anything.
3. **`a2t_web/config.py`**: new `WebSettings` fields + `_ENV_FOR` + `_read_secret`
   + `settings_from_env` password-file reading. Update `tests/test_web_config.py`
   (autouse tmp-secrets fixture + new tests); run `pytest tests/test_web_config.py`.
4. **`a2t_web/jobs.py`**: `log_path` param + hooks in `create`/`transition`/
   `finalize`. Add job-log tests to `tests/test_web_jobs.py`; run
   `pytest tests/test_web_jobs.py`.
5. **`a2t_web/app.py`**: secret key, `auth_enabled`, `JobRegistry(log_path=…)`,
   guard, `/login`, `/logout`, `/admin`, context processor, `main()` friendly
   error. 
6. **Templates + styles**: `base.html` nav, `login.html`, `admin.html`,
   `style.css` additions.
7. **Auth tests**: add the auth section to `tests/test_web_app.py`; run
   `pytest tests/test_web_app.py`.
8. **`tests/test_web_joblog.py`** (new): pure `joblog` unit tests; run it.
9. **README.md** web-app section updates.
10. **Full suite**: `pip install -e ".[dev,web]" && pytest` — everything must
    pass. Manual smoke (optional, not automated): with
    `.secrets/user-password` + `.secrets/admin-password` created, start
    `a2t-web`, confirm `/` redirects to `/login`, wrong password shows
    "Incorrect password.", user login reaches `/`, admin login reaches
    `/admin`, a plain user gets 403 on `/admin`, `/logout` returns to `/login`
    on next navigation, `/api/jobs/…` without a session returns 401 JSON, and
    `jobs.log` appears in the data dir.

---

## 6. Tests

### 6.1 New auth tests — `tests/test_web_app.py`

Reuse `make_app` (it already forwards overrides into `make_settings`). For each
auth test build a dedicated client: `app_ = make_app(user_password="user-pw",
admin_password="admin-pw")` then `c = app_.test_client()`.

- `test_login_page_served_when_auth_enabled` — GET `/login` → 200; contains
  `name="password"`.
- `test_open_mode_has_no_login_requirement` — default `client` GET `/` → 200
  (already covered by existing tests; keep as an explicit guard).
- `test_unauthenticated_page_redirects_to_login` — auth app, GET `/` → 302,
  `Location` starts with `/login` and contains `next=/`.
- `test_unauthenticated_api_returns_401_json` — auth app, GET `/api/jobs/nope`
  → 401; `rv.get_json()["error"]` non-empty.
- `test_login_wrong_password_rejected` — POST `/login` `{"password":"nope"}` →
  401; body contains "Incorrect password."; then GET `/` still 302s to login.
- `test_login_user_password_grants_access` — POST `/login`
  `{"password":"user-pw"}` → 302; follow to `/` → 200 with upload form.
- `test_login_admin_password_grants_admin` — POST `/login`
  `{"password":"admin-pw"}` → 302; GET `/admin` → 200, contains "job log".
- `test_login_redirects_to_next` — GET `/jobs/xyz` unauthenticated → 302
  `Location` contains `next=/jobs/xyz`; then POST `/login` with
  `{"password":"user-pw","next":"/jobs/xyz"}` → 302 to `/jobs/xyz`.
- `test_login_next_blocks_open_redirect` — POST `/login` with
  `next="//evil.example"` → 302 to `/` (not the external host).
- `test_user_cannot_access_admin` — login as user; GET `/admin` → 403.
- `test_admin_page_shows_job_entries` — login as admin; upload a job
  (`c.post("/jobs", ...)`), `complete_job(c, job_id)`, GET `/admin` → 200 and
  contains the job id, `com.pdf`, and `done`.
- `test_admin_page_empty_state` — auth app, login as admin, GET `/admin` → 200
  and contains "No jobs".
- `test_logout_clears_session` — login as user; GET `/logout` → 302; GET `/`
  → 302 back to `/login`.
- `test_admin_nav_link_only_for_admin` — login as admin, GET `/` contains
  `href="/admin"`; login as user, GET `/` does not contain `href="/admin"`.

### 6.2 Updated/new config tests — `tests/test_web_config.py`

Autouse fixture: write `user-password` and `admin-password` into a tmp dir,
`monkeypatch.setenv("A2T_WEB_SECRETS_DIR", str(tmp))`, and clean all other
`_ALL_ENV` vars as the existing defaults test does. Existing tests then pass
unchanged in behavior (they only assert non-password fields). Add:

- `test_settings_reads_passwords_from_secrets_dir` — after fixture,
  `settings_from_env().user_password == "user"` and
  `.admin_password == "admin"` (fixture writes those values).
- `test_settings_missing_password_file_raises` — fresh tmp dir (no files),
  set `A2T_WEB_SECRETS_DIR`; `pytest.raises(ValueError, match="user-password")`.
- `test_settings_empty_password_file_raises` — file containing only
  `"\n"`; `pytest.raises(ValueError, match="empty")`.
- `test_settings_strips_password_whitespace` — files containing
  `"  pw  \n"` / `" admin-pw \n"` → stripped values.
- `test_settings_secrets_dir_env_override` — covered by the fixture, but add an
  explicit test that a second tmp dir with different values is honored.
- `test_settings_accepts_fixed_secret_key` — `A2T_WEB_SECRET_KEY=abc` →
  `settings.secret_key == "abc"`.

### 6.3 Job-log integration tests — `tests/test_web_jobs.py`

Use `JobRegistry(log_path=tmp_path / "jobs.log")`:

- `test_registry_create_appends_log` — create → file exists with 1 line;
  `parse_line` returns `event == "create"`, `job_id`, `pdf_name`, `status ==
  QUEUED`.
- `test_registry_terminal_transition_appends_log` — create, then
  `transition(DONE)` → 2 lines; last line `event == "terminal"`,
  `status == DONE`, `finished_at` is a float.
- `test_registry_finalize_appends_error_log` — create, `finalize(FAILED,
  error="boom")` → last line error `"boom"`.
- `test_registry_finalize_does_not_double_log_terminal` — create, `finalize(DONE)`,
  `finalize(CANCELLED)` → still 2 lines (early return logs nothing).
- `test_registry_no_log_path_writes_nothing` — `JobRegistry()` + create →
  no `jobs.log` file created.
- `test_registry_log_escapes_error_with_newlines` — `finalize(FAILED,
  error="line1\nline2\tboom")` → parsing the line round-trips the exact error.
- `test_registry_running_transition_not_logged` — create, `transition(RUNNING)`
  → still 1 line (only create).

### 6.4 Pure `joblog` unit tests — `tests/test_web_joblog.py` (new)

- escape/unescape round-trip: `\`, tab, LF, CR, mixed.
- `format_row`/`parse_line` round-trip a create and a terminal record.
- `parse_line` on malformed lines (wrong field count, junk) → None; empty
  string → None.
- `append_log` appends; parent dir is created when missing.
- `read_entries` on a missing file → [].
- `read_entries` newest-first ordering and `limit` cap.
- `read_entries` keeps the last record per job_id (create + terminal → one
  dict with the terminal status).

### Regression

- All existing `tests/test_web_app.py`, `tests/test_web_config.py`,
  `tests/test_web_jobs.py`, `tests/test_web_storage.py`, `tests/test_web_js.py`
  tests pass unchanged (open mode / no log_path defaults).
- Run: `pytest tests/test_web_config.py tests/test_web_app.py
  tests/test_web_jobs.py tests/test_web_joblog.py`, then full `pytest`.

---

## 7. Risks and constraints

- **XSS (admin page)**: job ids, PDF filenames, statuses, and **errors** are
  operator/data-derived text rendered in `admin.html`. Rely on Jinja
  autoescaping (default on) — never use `|safe`; the only safe construction is
  `{{ ... }}`. `read_entries` already caps rows, but the file itself is
  operator-writable on disk anyway (same trust boundary as the data dir).
- **Password leakage**: passwords exist only in `.secrets/*` and in memory.
  They must never appear in: HTML, `jobs.log`, exception text, test output,
  or README examples with real values. The login error is the generic
  `"Incorrect password."`; config errors name file paths only; `main()`
  prints the `ValueError` message (paths, not contents).
- **Fail closed**: `settings_from_env` raises on missing/empty password files.
  A fresh checkout will not start `a2t-web` until the operator creates
  `.secrets/user-password` and `.secrets/admin-password`. This is the
  intended behavior per the feature request; README must call it out. Open
  mode is only reachable by constructing `WebSettings` directly (tests).
- **Session/cookie security**: `secret_key` is random per process by default
  → sessions are invalidated on restart (users re-login; acceptable). Set
  `A2T_WEB_SECRET_KEY` to a fixed value to persist sessions. Flask defaults:
  HttpOnly + SameSite=Lax cookies. `SESSION_COOKIE_SECURE` stays off because
  the app serves plain HTTP on 127.0.0.1; keep localhost binding recommended.
  Signed cookies are tamper-proof; nothing sensitive is stored in the cookie
  besides a role string.
- **Timing**: `hmac.compare_digest` prevents password-timing attacks. The
  negligible "user vs admin match" timing difference is accepted (two
  passwords are a deliberate feature).
- **Job-log durability**: appends use `open(path, "a")` (O_APPEND) under the
  registry lock; no fsync per write. Worst case on a hard crash is the loss of
  the very last line — acceptable for a job history log. `OSError` from
  `append_log` propagates (fail loud; a read-only data dir means the app is
  broken anyway).
- **Concurrency**: all log writes happen inside the existing registry lock;
  the worker, cancel route, and upload route all mutate through the registry,
  so lines cannot interleave. `read_entries` reads the whole file without a
  lock — a concurrent append mid-read can only truncate the final partial
  line, which `parse_line` skips.
- **Backwards compatibility**: open mode preserves all existing tests and
  behavior. `JobRegistry()` without `log_path` writes nothing. Existing
  endpoints and API shapes unchanged. No new dependencies. `WebSettings` gains
  only optional fields with defaults.
- **Behavior change for operators**: `a2t-web` now requires the two password
  files; `/api/*` returns 401 without a session when auth is enabled; the
  JSON API and artifact downloads are gated too (that is the point — the API
  and artifacts would otherwise leak report.md and docx content).
- **Unresolved assumptions / open questions**:
  1. Startup fails when the seed files are absent — confirmed as the intended
     design; the consequence (can't run `a2t-web` on a fresh checkout without
     creating the files) is flagged for product sign-off.
  2. `/admin` renders only the durable file. A job that is still running (or
     crashed mid-flight) shows its `create` record with status `queued` until
     a terminal record lands. Overlaying live registry snapshots is a possible
     follow-up; deliberately out of scope to keep the page simple.
  3. `/admin` is reachable in open mode (no auth configured) as a dev
     convenience; if product prefers a hard 403 there even in open mode, that
     is a one-line change in the route.
  4. `A2T_WEB_SECRET_KEY` documentation (3.9) is included because session
     persistence across restarts is otherwise surprising; whether to advertise
     it more prominently is a docs choice.