"""Administration, from the command line, with the owner's credential (DESIGN §12a).

    uv run python -m app.api.admin invite --email someone@example.com [--days 14]
    uv run python -m app.api.admin invite --github their-username [--days 14]
    uv run python -m app.api.admin make-admin someone@example.com
    uv run python -m app.api.admin reset-password someone@example.com

The owner, because the Web Server's role can do none of this: it cannot create
an invite, set ``is_superuser``, or change a password it did not just check
(DESIGN §11). A code or password is printed **once** and stored only hashed.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from fastapi_users.password import PasswordHelper
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.auth import hash_invite_code, new_invite_code
from app.db.web import Invite, User

DEFAULT_DAYS = 14


async def create_email_invite(
    session_factory: async_sessionmaker, email: str, days: int = DEFAULT_DAYS
) -> str:
    """An invite for one address. Returns the code -- the only time it exists."""
    code = new_invite_code()
    async with session_factory() as session:
        session.add(
            Invite(
                id=uuid.uuid4(),
                kind="email",
                email=email.strip().lower(),
                code_hash=hash_invite_code(code),
                expires_at=datetime.now(UTC) + timedelta(days=days),
            )
        )
        await session.commit()
    return code


async def create_github_invite(
    session_factory: async_sessionmaker, account_id: str, days: int = DEFAULT_DAYS
) -> None:
    """An invite for one GitHub account, by its numeric id -- permanent, and not
    claimable by someone else the way an email GitHub hands back is (DESIGN §10)."""
    async with session_factory() as session:
        session.add(
            Invite(
                id=uuid.uuid4(),
                kind="github",
                github_account_id=str(account_id),
                expires_at=datetime.now(UTC) + timedelta(days=days),
            )
        )
        await session.commit()


async def github_account_id(username: str) -> str:
    """A GitHub username's numeric id, from GitHub's public API."""
    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(
            f"https://api.github.com/users/{username}",
            headers={"Accept": "application/vnd.github+json"},
        )
    if response.status_code == 404:
        raise LookupError(f"no GitHub user {username!r}")
    response.raise_for_status()
    return str(response.json()["id"])


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
    invite = commands.add_parser("invite", help="invite someone")
    who = invite.add_mutually_exclusive_group(required=True)
    who.add_argument("--email", help="an address; prints a single-use code to hand them")
    who.add_argument("--github", metavar="USERNAME", help="a GitHub account, by username")
    who.add_argument("--github-id", help="a GitHub account, by numeric id")
    invite.add_argument("--days", type=int, default=DEFAULT_DAYS, help="valid for (default 14)")
    admin = commands.add_parser("make-admin", help="make an existing user an administrator")
    admin.add_argument("email")
    reset = commands.add_parser("reset-password", help="give a user a new random password")
    reset.add_argument("email")
    args = parser.parse_args(argv)

    from app.db.session import SessionLocal  # the owner

    if args.command == "invite":
        if args.email:
            code = asyncio.run(create_email_invite(SessionLocal, args.email, args.days))
            print(f"Invite for {args.email}, valid {args.days} days. Code (shown once): {code}")
            return 0
        account = args.github_id or asyncio.run(github_account_id(args.github))
        asyncio.run(create_github_invite(SessionLocal, account, args.days))
        name = args.github or f"id {account}"
        print(f"Invite for GitHub account {name} (id {account}), valid {args.days} days.")
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
