"""User management for the web app's SQLite user store.

Primary bootstrap is environment seeding on first run (``seed_users``, called
by ``create_app`` when the users table is empty). The console script
``a2t-web-users`` is the backup: create or reset users in an existing
database, e.g. to recover an admin account.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from a2t_web.db import Database

_ROLE_FOR_COMMAND = {"create-admin": "admin", "create-user": "user"}


def seed_users(settings, db: Database) -> None:
    """Seed the first users from settings; a no-op once any user exists.

    Admin is seeded (role ``admin``) only when ``seed_admin_password`` is
    set; the normal user (role ``user``) only when ``seed_user_password`` is
    set. If neither password is set nothing is seeded and the app stays in
    open (no-auth) mode. Seeding is skipped entirely once the users table is
    non-empty, so restarts never duplicate users.
    """
    if db.count_users() > 0:
        return
    if settings.seed_admin_password:
        username = settings.seed_admin_username.strip() or "admin"
        db.create_user(username, settings.seed_admin_password, "admin")
    if settings.seed_user_password:
        username = settings.seed_user_username.strip() or "user"
        db.create_user(username, settings.seed_user_password, "user")


def create_user_file(db_path: Path, username: str, password: str, role: str) -> bool:
    """Create one user in ``db_path``; False for a duplicate/invalid role."""
    db = Database(db_path)
    db.init()
    return db.create_user(username, password, role)


def _add_user_parser(subparsers, command: str, role: str):
    article = "an" if role == "admin" else "a"
    parser = subparsers.add_parser(command, help=f"create {article} {role} account")
    parser.add_argument("--db", required=True, help="path to the a2t.db database file")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="a2t-web-users",
        description="Create or reset users in the a2t web database.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _add_user_parser(subparsers, "create-admin", "admin")
    _add_user_parser(subparsers, "create-user", "user")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(
            f"a2t-web-users: database file not found: {db_path} "
            "(start the web app once to create it, then retry)",
            file=sys.stderr,
        )
        return 2
    role = _ROLE_FOR_COMMAND[args.command]
    created = create_user_file(db_path, args.username, args.password, role)
    if not created:
        print(
            f"a2t-web-users: could not create {role} user {args.username!r} "
            "(duplicate username, unknown role, or empty username/password)",
            file=sys.stderr,
        )
        return 1
    print(f"a2t-web-users: created {role} user {args.username!r} in {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())