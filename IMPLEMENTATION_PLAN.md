# Implementation Plan: username/password login with SQLite user store and durable job history

This plan replaces the current password-only auth and the TSV `jobs.log` with:

1. **Username + password login** backed by an SQLite `users` table (roles: `user` / `admin`).
2. **An SQLite persistence layer** (`a2t_web/db.py`, stdlib `sqlite3`, no new dependencies)
   storing users and the job history.
3. **Historical jobs that survive restarts**: every job create and every terminal
   transition (DONE/FAILED/CANCELLED) is written to SQLite; `/admin` renders from
   SQLite instead of the TSV job log.

The in-memory `JobRegistry` stays as the live runtime store (queue/status polls/status
page). Only the durable record moves from `joblog.py` (TSV) to `db.py` (SQLite).

---

## Objective

- Change authentication from "password only" to "username + password", with users
  stored in SQLite (normal `user` and `admin` roles) instead of two plaintext
  password files compared with `hmac.compare_digest`.
- Add a stdlib `sqlite3` backend under `settings.data_dir` that stores users and
  job history.
- Replace the TSV `jobs.log` registry persistence with SQLite writes; `/admin`
  renders history from SQLite so it survives restarts and registry sweeps.
- Deprecate `.secrets/user-password` / `.secrets/admin-password`; seed the first
  admin/user from environment variables on first run, with a small CLI as backup.

---

## Current system

- **`a2t_web/config.py`**: frozen `WebSettings(data_dir, secrets_dir, user_password,
  admin_password, secret_key, ...)`. `settings_from_env()` reads
  `.secrets/user-password` and `.secrets/admin-password` and **fails closed** with
  `ValueError` if either is missing, empty, or non-UTF-8. Env: `A2T_WEB_SECRETS_DIR`,
  `A2T_WEB_SECRET_KEY`, `A2T_WEB_DATA_DIR`, etc.
- **`a2t_web/app.py`**: `create_app(settings=None)`; `auth_enabled =
  bool(settings.user_password or settings.admin_password)`; `before_request` guard
  (blocked when open); `GET/POST /login` (password-only, `hmac.compare_digest`,
  session role from which password matched); `GET /logout`; `GET /admin`
  (`joblog.read_entries`); API routes; `main()` catches `ValueError` from
  `settings_from_env()`. `job_log_path = data_dir / "jobs.log"` and
  `JobRegistry(log_path=job_log_path)` are constructed unconditionally.
- **`a2t_web/jobs.py`**: in-memory lock-guarded `RegistryJob`; `JobRegistry(log_path)`
  with `_log_event` appending TSV records via `joblog.append_log` on create and on
  terminal transitions. `finalize` explicitly never double-logs (early-return to
  already-terminal jobs logs nothing).
- **`a2t_web/joblog.py`**: TSV append-only helpers (`create_record`,
  `terminal_record`, `format_row`, `parse_line`, `append_log`, `read_entries`).
- **`a2t_web/worker.py`**: runs `a2t convert` as subprocess; mutates registry state;
  `build_command` monkeypatched in tests.
- **Templates** (`a2t_web/templates/`): `base.html` nav uses `auth_enabled` +
  session role; `login.html` is password-only with a `next` hidden field;
  `admin.html` renders `entries` (each has `job_id`, `pdf_name`, `status`,
  `created_at`, `started_at`, `finished_at`, `error`).
- **Tests**: `tests/test_web_app.py` (page flows + auth via
  `make_app(user_password=..., admin_password=...)`), `tests/test_web_config.py`
  (password-file handling), `tests/test_web_jobs.py` (registry + worker; joblog
  integration via `JobRegistry(log_path=...)`), `tests/test_web_joblog.py`
  (pure joblog tests), plus pipeline/storage/js/sanity tests unaffected here.
- **Deployment**: `Dockerfile` CMD writes `$A2T_USER_PASSWORD` /
  `$A2T_ADMIN_PASSWORD` into `.secrets/` files before running
  `python -m a2t_web.app`; `.dockerignore` excludes `.secrets` and `webapp-data`;
  README documents password files and caps at 200 admin rows.

---

## Proposed design

### 1. Persistence layer — new module `a2t_web/db.py` (stdlib `sqlite3`)

**DB file**: `<data_dir>/a2t.db` (a new `db_path` field on `WebSettings`, default
`data_dir / "a2t.db"`). Created on app startup by `Database.init()` which runs
`CREATE TABLE IF NOT EXISTS` (idempotent) and sets pragmas `journal_mode=WAL`,
`synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`.

**Connection strategy**: a per-instance `threading.Lock` serializes all writes;
every function opens its **own** fresh connection (context manager), applies the
pragmas, runs the query with `?`-parameterized SQL, commits on write, and closes.
No connection crosses threads. This is safe under Flask's threaded dev server and
keeps the module trivially unit-testable.

**Schema**:

```sql
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user','admin')),
    created_at REAL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    pdf_name TEXT,
    status TEXT,
    error TEXT,
    created_at REAL,
    started_at REAL,
    finished_at REAL
);
```

**Module API** (class `Database`; methods open their own connection):

- `Database(path: Path)`
- `init()` — create schema + pragmas; idempotent (called by `create_app`).
- `create_user(username, password, role) -> bool` — hashes with
  `werkzeug.security.generate_password_hash` (werkzeug ships with Flask);
  returns `False` if username already exists (unique constraint); never stores
  plaintext.
- `get_user_by_username(username) -> dict | None` — row with keys `id`,
  `username`, `password_hash`, `role`, `created_at`.
- `verify_login(username, password) -> dict | None` — looks up by username,
  runs `werkzeug.security.check_password_hash`, returns the user dict (minus the
  hash) on success, `None` otherwise.
- `list_users() -> list[dict]` — all users ordered by `id`.
- `count_users() -> int` — decides auth-on vs open mode and whether to seed.
- `insert_job(job) -> None` — one INSERT on job create (status usually `queued`),
  reading fields off the `Job` dataclass (duck-typed like `joblog` was, so `db.py`
  does not import `jobs.py`).
- `set_job_terminal(job_id, status, error, finished_at) -> None` — one UPDATE
  on terminal transition.
- `list_jobs(limit: int | None = None) -> list[dict]` — `SELECT ... ORDER BY
  created_at DESC LIMIT ?`; returns dicts with keys `job_id`, `pdf_name`,
  `status`, `error`, `created_at`, `started_at`, `finished_at` (key `job_id`
  matches the existing `admin.html` template so the template needs no rework).

Rationale: two writes per job (INSERT on create, UPDATE on terminal) mirror today's
two TSV records and avoid rewriting history; parameterized SQL everywhere prevents
injection; WAL + a single-instance write lock avoids SQLite `database is locked`
under the threaded server.

### 2. Auth rework — `app.py`, `config.py`, `login.html`

- `login.html` gains a username field (`name="username"`, `autofocus` moves to it);
  password field stays. Error stays generic ("Incorrect username or password.")
  to avoid user enumeration.
- `POST /login` calls `db.verify_login(username, password)`. On success:
  `session.clear()`, then `session["authenticated"]=True`,
  `session["role"]=user["role"]`, `session["username"]=user["username"]`, redirect
  `next` (same open-redirect guard) or `/`. On failure: `401` + generic error.
- **Auth on/open mode**: auth is enabled whenever `db.count_users() > 0`
  (`auth_enabled` computed after `db.init()`). An empty users table = open mode
  (no guard, `/login` redirects to `/`), identical to today's behavior when both
  passwords were empty. Existing non-auth app tests keep working because the
  `make_app` fixture points at a fresh tmp data_dir with an empty DB.
- **Bootstrap (primary)**: `create_app` calls a `seed_users(settings, db)`
  helper that, **only when `db.count_users() == 0`**, creates users from env-derived
  `WebSettings` seed fields:
  - `seed_admin_username` (env `A2T_ADMIN_USERNAME`, default `"admin"`) +
    `seed_admin_password` (env `A2T_ADMIN_PASSWORD`, required for admin seeding) →
    role `admin`.
  - `seed_user_username` (env `A2T_USER_USERNAME`, default `"user"`) +
    `seed_user_password` (env `A2T_USER_PASSWORD`) → role `user`, only if set.
  If `A2T_ADMIN_PASSWORD` is unset AND `A2T_USER_PASSWORD` is unset, nothing is
  seeded and the app stays in open mode. Seeding is skipped entirely once any user
  exists (safe across restarts; never duplicates).
- **CLI (secondary)**: new minimal module `a2t_web/users.py` with
  `create_user(db_path, username, password, role)` plus a `main()` handling
  `create-admin`/`create-user` subcommands, exposed as a new console script
  `a2t-web-users`. Usable anytime to add/change users (e.g. reset an admin).
  `python -m a2t_web.app` is unchanged.
- **`config.py`**:
  - Remove `user_password`, `admin_password`, `secrets_dir`, and `_read_secret`.
    The `.secrets/openrouter-key` file is read independently by `a2t.config`
    (unchanged, unrelated to `WebSettings`), so nothing in `a2t_web.config`
    needs a secrets dir anymore.
  - Add `db_path: Path = Path("webapp-data") / "a2t.db"` and seed fields
    `seed_admin_username: str = "admin"`, `seed_admin_password: str = ""`,
    `seed_user_username: str = "user"`, `seed_user_password: str = ""`.
  - `settings_from_env()` reads new vars `A2T_ADMIN_USERNAME`, `A2T_ADMIN_PASSWORD`,
    `A2T_USER_USERNAME`, `A2T_USER_PASSWORD`; **no longer fails closed** on
    password files. Keep `A2T_WEB_SECRET_KEY`, `A2T_WEB_DATA_DIR`, etc., and the
    `max_workers` invariant. `db_path` derives from `data_dir` ("<data_dir>/a2t.db")
    and is not env-overridable (kept simple; deployments use `A2T_WEB_DATA_DIR`).
  - Keep `main()` unchanged (still catches `ValueError`, though now mostly for the
    `max_workers` invariant and env parsing).
- **Guard/before_request** stays in place: pages → `/login?next=…`; `/api/*` → 401
  JSON; `/admin` additionally requires `session["role"] == "admin"` when auth on.

Rationale: one store for users + jobs; bootstrap from env needs no interactive
setup and matches a Docker/Render deployment; the CLI covers ops like password
resets; deleting the plaintext files removes the fail-closed startup hazard and
the "one password per role" limitation.

### 3. Historical jobs — registry persistence hooks + `/admin`

- **`JobRegistry`** keeps its in-memory dict but replaces `log_path` with `db`:

  ```python
  class JobRegistry:
      def __init__(self, db=None): ...  # db is Optional[a2t_web.db.Database]
  ```

  - `create(...)`: after inserting into `self._jobs`, call `self._db.insert_job(job)`
    if `db` is not None.
  - `transition(...)`: when `status in TERMINAL_STATUSES`, call
    `self._db.set_job_terminal(job.id, job.status, job.error, job.finished_at)`.
    RUNNING transitions are not persisted (kept small, mirrors joblog contract).
  - `finalize(...)`: same as today — if the job is already terminal it returns early
    **without** a second write (no double-terminal); otherwise it sets terminal
    state and persists via `set_job_terminal`.
  - Persistence calls happen while holding `self._lock` (same position as today's
    `_log_event`), so terminal rows are never interleaved.
  - `JobRegistry()` (no `db`) still works and writes nothing — all existing
    registry/worker/sweep tests that build `JobRegistry()` continue to pass
    unmodified.
- **`create_app`**: `db = db_module.Database(settings.db_path); db.init();
  seed_users(settings, db); registry = JobRegistry(db=db)`. Store `db` on
  `app.extensions["a2t_db"]`.
- **`/admin`** renders from `db.list_jobs(limit=ADMIN_LOG_LIMIT)` instead of
  `joblog.read_entries`. `ADMIN_LOG_LIMIT` (200) stays.
- **Delete `a2t_web/joblog.py`** and **`tests/test_web_joblog.py`** (recommended for
  cleanliness; nothing else imports `joblog` after the above). Also remove the
  `jobs.log` path/`log_path` references everywhere (`app.py`, `jobs.py`).
- **`admin.html`** needs no rework if `list_jobs` returns the keys the template
  already uses (`job_id`, `pdf_name`, `status`, `created_at`, `started_at`,
  `finished_at`, `error`); the column set already matches the desired one. Verify
  the mapping once when wiring, adjust keys if the template accesses differ.

The jobs table is durable: rows survive restarts and job-directory sweeps. The
in-memory registry remains the only source of truth for live state (status page,
queue position, artifact listing), with SQLite as the durable mirror.

### 4. Deployment

- **`Dockerfile`**: remove the `mkdir -p .secrets && printf ...` CMD prefix. New CMD:
  `CMD ["sh", "-c", "A2T_WEB_PORT=\"${A2T_WEB_PORT:-${PORT:-5000}}\" python -m a2t_web.app"]`.
  Container operators pass `A2T_ADMIN_PASSWORD` (required for auth),
  `A2T_ADMIN_USERNAME` (optional, default `admin`), `A2T_USER_PASSWORD`,
  `A2T_USER_USERNAME`, `A2T_WEB_SECRET_KEY` (recommended for stable sessions).
- **`README.md`**: rewrite the "Access control" section — new env vars, seeding
  on first run, the `a2t-web-users` CLI (create/reset users), DB file location
  `<A2T_WEB_DATA_DIR>/a2t.db`, note old `.secrets/user-password` /
  `.secrets/admin-password` are deprecated and no longer read.
- **`.dockerignore`**: keep `.secrets` and `webapp-data` entries (DB lives in
  webapp-data, so on Render free tier the DB is ephemeral-per-restart — document
  this caveat in README).

---

## Files affected

### Files to create

- **`a2t_web/db.py`** — module documented earlier. Functions on class `Database`:
  `__init__`, `init`, `create_user`, `get_user_by_username`, `verify_login`,
  `list_users`, `count_users`, `insert_job`, `set_job_terminal`, `list_jobs`,
  plus a private `_connect()` context manager and `human_ts`-independent floats
  (timestamps are stored as epoch `REAL`, consistent with `Job` fields).
- **`a2t_web/users.py`** — CLI:
  - `create_user_file(db_path, username, password, role) -> bool`
  - `main(argv=None) -> int` handling `a2t-web-users create-admin --db PATH --username U --password P` and
    `... create-user --db PATH --username U --password P` (+ optional `--username`
    default not needed; require explicit username), printing a human result
    and returning nonzero on a duplicate/unknown role/absent DB file.
- **`tests/test_web_db.py`** — new unit tests (see Tests).

### Files to modify

- **`a2t_web/config.py`**
  - `WebSettings`: drop `user_password`, `admin_password`, `secrets_dir`; add
    `db_path: Path`, `seed_admin_username`, `seed_admin_password`,
    `seed_user_username`, `seed_user_password`.
    - Define `db_path` as a `field(default=DEFAULT_DATA_DIR / "a2t.db")` (or as a
      `@property` after construction — simplest: make it a plain field and set it
      from `data_dir` in `settings_from_env`; tests pass it explicitly).
  - Remove `_read_secret`; delete env wiring for `A2T_WEB_SECRETS_DIR`; add env wiring
    for the four seed vars; keep all other fields/mapping and the `max_workers`
    invariant.
- **`a2t_web/app.py`**
  - Import `a2t_web.db` and `a2t_web.users`; drop `import joblog` and
    `hmac` (no longer used).
  - `create_app`: create `db`, `db.init()`, `seed_users(settings, db)`;
    `registry = JobRegistry(db=db)`; `job_log_path` deleted;
    `app.extensions["a2t_db"] = db`.
  - `login()`/`login_post()`: username+password; session
    authenticated/role/username; generic error; `auth_enabled = db.count_users() > 0` (computed after seeding; store on a local var for the request guard and context processor).
  - `/admin`: `entries = db.list_jobs(limit=ADMIN_LOG_LIMIT)`.
  - Guard behavior unchanged otherwise (401 for API, redirect for pages).
- **`a2t_web/jobs.py`**
  - `JobRegistry.__init__(self, db=None)`; drop `log_path` and the `joblog` import.
  - Persistence hooks: `insert_job` on create; `set_job_terminal` in `transition` and
    `finalize` terminal paths; keep the finalize early-return non-persisting.
  - Helper method `_persist(...)` continues to be called while holding
    the lock (same structure as today's `_log_event`).
- **`a2t_web/templates/login.html`** — add `<label for="username">Username</label>`
  + `<input type="text" id="username" name="username" autofocus required>` before
  the password field; keep the hidden `next` input; generic error copy "Incorrect
  username or password."; keep `.muted` hint updated to say you sign in with your
  username.
- **`a2t_web/templates/admin.html`** — unchanged unless `list_jobs` key names
  differ; verify the template still renders from `entries` dicts with the
  documented keys.
- **`Dockerfile`** — remove the `.secrets` writing step in CMD (see Deployment).
- **`README.md`** — access-control + deployment docs (see Deployment).
- **`pyproject.toml`** — add console script `a2t-web-users = "a2t_web.users:main"`.
- **`tests/test_web_app.py`** — rework auth tests (see Tests).
- **`tests/test_web_config.py`** — rewrite around the new fields (see Tests).
- **`tests/test_web_jobs.py`** — replace the joblog-integration tests with
  db-persistence registry tests; the rest stays (see Tests).

### Files to delete

- `a2t_web/joblog.py`
- `tests/test_web_joblog.py`

### Unchanged (intentionally)

- `a2t_web/worker.py` (or only still uses `registry`; no change), `wsgi.py`,
  `a2t_web/storage.py`, `a2t_web/sanity.py`, `a2t_web/pipeline.py`, all templates
  other than `login.html`, the remaining `tests/test_web_*.py` files.

---

## Implementation sequence

1. **`a2t_web/db.py`** (new) — schema, pragmas, lock, `_connect`, user functions,
   job functions, `list_jobs`. Unit-test `tests/test_web_db.py` and iterate until
   green before wiring anything else.
2. **`a2t_web/config.py`** — remove password fields; add `db_path` + seed fields;
   rewrite `settings_from_env` env mapping (env seed vars; no fail-closed password
   reads; keep `max_workers` invariant). Update `tests/test_web_config.py`.
3. **`a2t_web/jobs.py`** — swap `log_path`/joblog for optional `db`; wire
   `create`/`transition`/`finalize`. Keep `Registry()` (no db) behavior identical.
4. **`a2t_web/app.py`** — wire `db.init()`, seeding, `JobRegistry(db=db)`, the
   username/password `login_post`, session additions, `/admin` from DB, remove
   `job_log_path` usage. Update `tests/test_web_app.py` auth block.
5. **`templates/login.html`** — username field + error text.
6. **`a2t_web/users.py`** + **`pyproject.toml`** script; add `test_web_users.py`
   tests (or fold into `test_web_db.py`).
7. **Delete** `a2t_web/joblog.py` and `tests/test_web_joblog.py`; remove leftover
   references (grep `joblog`, `log_path`, `jobs.log`).
8. **`tests/test_web_jobs.py`** — swap the joblog-integration block for DB-backed
   registry tests.
9. **`Dockerfile`**, **`README.md`**, **`.dockerignore`** (verify).
10. **Full test run**: `python -m pytest tests/` (project has no test runner other
    than pytest; command used elsewhere in repo docs: `pytest`).

---

## Tests

### New: `tests/test_web_db.py`

- `init` is idempotent — calling twice creates no error, tables exist.
- Database file is created at the given path (parent dirs created).
- `create_user` then `verify_login` succeeds for a `user` and an `admin`.
- `verify_login` returns `None` for wrong password and for unknown username
  (both cases identical — no user enumeration via return shape).
- `create_user` with a duplicate username returns `False` and does not overwrite.
- Password stored is not the plaintext; it is a valid werkzeug hash
  (`check_password_hash` of stored hash == True).
- `list_users` returns created users ordered by id.
- `insert_job` + `set_job_terminal` (DONE/FAILED/CANCELLED) → `list_jobs` returns
  one terminal row with correct fields; ordering is `created_at` DESC; `limit`
  respected; unknown `set_job_terminal` on a missing id is a no-op (UPDATE affects
  0 rows — make it not raise).
- `error` survives round-trip, including newlines/tabs.
- Basic thread-safety: a few threads call `insert_job`/`set_job_terminal`
  concurrently into one `Database`, no `OperationalError` raised, all rows present.

### Rework: `tests/test_web_app.py` (auth section only, lines ~552+)

- Replace `make_auth_client(make_app, user_pw, admin_pw)` with a version that
  passes seed settings:

  ```python
  def make_auth_client(make_app, admin_user="admin", admin_pw="admin-pw",
                       user="user", user_pw="user-pw"):
      return make_app(
          seed_admin_username=admin_user, seed_admin_password=admin_pw,
          seed_user_username=user, seed_user_password=user_pw,
      ).test_client()
  ```

- Rewrite `login(client, password)` → `login(client, username, password,
  next_url=None)` posting `{"username": ..., "password": ...}`.
- Cases:
  - `test_login_page_served_when_auth_enabled` → 200, `name="username"` present.
  - `test_open_mode_has_no_login_requirement(client)` stays (empty DB): no seed
    vars → `/` 200.
  - `test_unauthenticated_page_redirects_to_login` — unchanged semantics.
  - `test_unauthenticated_api_returns_401_json` — unchanged.
  - `test_login_wrong_password_rejected` / new `test_login_wrong_username_rejected`
    → 401, body "Incorrect username or password.", `/` still redirects.
  - `test_login_user_grants_access` — `login(user, user_pw)` → `/` 200.
  - `test_login_admin_grants_admin` — `login(admin, admin_pw)` → `/admin` 200 with
    "job log".
  - `test_login_redirects_to_next`, `test_login_next_blocks_open_redirect` —
    updated to include username.
  - `test_user_cannot_access_admin` — user login → 403.
  - `test_admin_page_shows_job_entries` — after upload+complete, `/admin` shows the
    job id + pdf_name (reads from SQLite now).
  - `test_admin_page_empty_state` — no rows → "No jobs".
  - `test_logout_clears_session`, `test_admin_nav_link_only_for_admin` —
    updated to use seeded clients.
  - Delete tests tied to empty/identical passwords
    (`test_empty_configured_user_password_never_authenticates`,
    `test_empty_admin_password_falls_back_to_user`,
    `test_identical_user_and_admin_passwords_grant_admin`) — obsolete (role comes
    from the users table, not which password matched).
  - `test_session_cookie_samesite_is_lax`, `test_unauthenticated_api_endpoints_denied`,
    `test_unauthenticated_upload_redirects_and_creates_no_job`,
    `test_admin_page_escapes_job_error_and_pdf_name` — keep, seeded-client version.
- Non-auth page/API tests keep working: `client` fixture opens a fresh tmp
  data_dir => empty DB => open mode; `registry.transition` calls write through to
  db but harmlessly.

### Rework: `tests/test_web_config.py`

- Drop the `secrets_env` fixture and all `user_password`/`admin_password`/file tests.
- New env mapping tests: defaults for seed vars; `A2T_ADMIN_PASSWORD`/`A2T_USER_PASSWORD`
  parsed; `A2T_ADMIN_USERNAME`/`A2T_USER_USERNAME` parsed; still parses integer
  options and the `A2T_WEB_MAX_WORKERS` invariant; `db_path` default is
  `<A2T_WEB_DATA_DIR>/a2t.db` and honors `A2T_WEB_DATA_DIR`; `secret_key` env still
  set.

### Rework: `tests/test_web_jobs.py`

- Delete the import of `a2t_web.joblog`; the registry fixture `Registry()` (no db)
  stays for worker/registry tests.
- Replace the "job-log integration" block (lines ~201-271) with DB-backed
  equivalents using a `tmp_path` `Database` and `Registry(db=db)`:
  - create persists one row (status `queued`),
  - terminal transition persists `set_job_terminal`,
  - `finalize` persists and the early-return does not double-write,
  - running transition not persisted,
  - error with newlines round-trips.
- Other tests (queue position, sweep, worker lifecycle, tail) unchanged.

### Tests to delete

- `tests/test_web_joblog.py` (whole file).

### Optional new: `tests/test_web_users.py`

- CLI `create-user`/`create-admin` create users in a tmp DB; duplicate returns
  nonzero + message; missing/empty DB prints a clear error.

---

## Risks and constraints

- **Backwards compatibility**: breaking change — the `.secrets/user-password` /
  `.secrets/admin-password` files stop being read. Existing deployments must set
  the new seed env vars (or use the CLI) once. On first run with no seed vars the
  app starts **open** (no auth) — same as today's empty-password open mode.
  Data layout under `data_dir` (uploads, jobs) is unchanged; only the credential
  mechanism differs.
- **Security**:
  - Passwords hashed with `werkzeug.security.generate_password_hash`
    (scrypt/pbkdf2) — never plaintext; DB file should be protected by host file
    permissions.
  - Generic login error prevents username enumeration; `verify_login` returns the
    same `None` shape for unknown user and wrong password.
  - All SQL parameterized `?` placeholders — no string interpolation.
  - Session cookie unchanged: signed by `A2T_WEB_SECRET_KEY`.
  - Seeding seeds users only when the users table is empty; env-seed passwords must
    not be echoed in logs.
- **Concurrency**: SQLite single-writer lock serialized by `threading.Lock` inside
  `Database`; WAL + `busy_timeout` reduce contention. All connections are per-op,
  no cross-thread connection reuse. `max_workers` remains 1.
- **Performance**: WAL writes on job create/terminal are sub-ms; `/admin` query is
  indexed. `jobs` table may grow unbounded over time — acceptable for the single
  operator; future work could add a `ORDER BY created_at` index and pruning.
- **Ephemeral disk on Render free tier**: DB lives under `webapp-data` which is
  excluded from the image (`.dockerignore`) and is ephemeral on Render free tier —
  history will not survive a render re-deploy. Document; note that providing a
  persistent volume path via `A2T_WEB_DATA_DIR` makes history durable.
- **Unresolved assumptions**:
  - Username uniqueness is enforced after trim/lowercase? The plan stores username
    exactly as provided (case-sensitive UNIQUE). Optionally normalize with
    `.strip()` on create and on login to avoid accidental duplicates — implementer
    should call `.strip()` on usernames in `create_user`/`verify_login`/login form.
  - First-run seeding decides "admin" as the default username; admins can add more
    via CLI thereafter.
  - No migration is provided for legacy `jobs.log` — old history is not imported
    into the DB (out of scope).
  - `admin.html` is kept as-is except if keys differ; verify during step 4.