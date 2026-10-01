"""Administration, from the command line, with the owner's credential (DESIGN §10).

    uv run python -m app.api.admin invite [--days 14]
    uv run python -m app.api.admin make-admin someone@example.com
    uv run python -m app.api.admin reset-password someone@example.com

The owner, because the Web Server's role can do none of this: it cannot create
an invite, set ``is_superuser``, or change a password it did not just check
(DESIGN §11). A code or password is printed **once** and stored only hashed.
An invite is a code to hand to someone yourself: nothing is emailed, and it is
bound to no address -- it registers one account, by password or with GitHub.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import uuid
from datetime import UTC, datetime, timedelta

from fastapi_users.password import PasswordHelper
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.auth import hash_invite_code, new_invite_code
from app.db.web import Invite, User

DEFAULT_DAYS = 14


async def create_invite(
    session_factory: async_sessionmaker,
    days: int = DEFAULT_DAYS,
    created_by: uuid.UUID | None = None,
) -> str:
    """A single-use invite code, valid ``days``. Returns the code -- the only time
    it exists; only its hash is stored. ``created_by``: the admin who made it,
    none from the CLI."""
    code = new_invite_code()
    async with session_factory() as session:
        session.add(
            Invite(
                id=uuid.uuid4(),
                code_hash=hash_invite_code(code),
                expires_at=datetime.now(UTC) + timedelta(days=days),
                created_by=created_by,
            )
        )
        await session.commit()
    return code


async def make_admin(session_factory: async_sessionmaker, email: str) -> bool:
    async with session_factory() as session:
        changed = await session.execute(
            update(User)
            .where(func.lower(User.email) == email.strip().lower())
            .values(is_superuser=True)
        )
        await session.commit()
    return changed.rowcount == 1


async def reset_password(session_factory: async_sessionmaker, email: str) -> str | None:
    """A new random password for this user, returned once. None: no such user."""
    password = secrets.token_urlsafe(18)  # 24 characters, well over the 12 minimum
    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(func.lower(User.email) == email.strip().lower())
        )
        if user is None:
            return None
        user.hashed_password = PasswordHelper().hash(password)
        await session.commit()
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Web Server administration (owner only).")
    commands = parser.add_subparsers(dest="command", required=True)
    invite = commands.add_parser("invite", help="print a single-use invite code to hand out")
    invite.add_argument("--days", type=int, default=DEFAULT_DAYS, help="valid for (default 14)")
    admin = commands.add_parser("make-admin", help="make an existing user an administrator")
    admin.add_argument("email")
    reset = commands.add_parser("reset-password", help="give a user a new random password")
    reset.add_argument("email")
    args = parser.parse_args(argv)

    from app.db.session import SessionLocal  # the owner

    if args.command == "invite":
        code = asyncio.run(create_invite(SessionLocal, args.days))
        print(f"Invite code, single use, valid {args.days} days (shown once): {code}")
        return 0
    if args.command == "make-admin":
        if not asyncio.run(make_admin(SessionLocal, args.email)):
            print(f"no user {args.email}", file=sys.stderr)
            return 1
        print(f"{args.email} is now an administrator.")
        return 0
    password = asyncio.run(reset_password(SessionLocal, args.email))
    if password is None:
        print(f"no user {args.email}", file=sys.stderr)
        return 1
    print(f"New password for {args.email} (shown once): {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
