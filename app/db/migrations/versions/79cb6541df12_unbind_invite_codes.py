"""unbind invite codes

An invite becomes a single-use code bound to no address: it registers one
account, by password or through GitHub. Its `kind`, `email` and
`github_account_id` go; `code_hash` (now exactly a SHA-256 hex digest) and
`expires_at` become required, and `code_hash` unique, since it is how an
invite is found. Design: app/api/DESIGN.md section 10.

Hand-corrected from autogenerate (docs/ALEMBIC.md): the data steps are added.
A GitHub invite has no code to carry over, so it is deleted first -- a spent
one's account keeps its GitHub link in `oauth_account`, and loses only the
record of how it was invited. An invite without an expiry gets the default 14
days from its creation. Downgrade's CHECKs are written as the first migration
wrote them, not as PostgreSQL echoes them back.

DOWNGRADE DELETES EVERY INVITE: an unbound code cannot be bound back to an
address.

Revision ID: 79cb6541df12
Revises: 966a8d209afd
Create Date: 2026-09-30 23:26:49.009310

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '79cb6541df12'
down_revision: Union[str, Sequence[str], None] = '966a8d209afd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("DELETE FROM web.invite WHERE code_hash IS NULL")
    op.execute(
        "UPDATE web.invite SET expires_at = created_at + interval '14 days' "
        "WHERE expires_at IS NULL"
    )
    op.drop_constraint(op.f('ck_invite_invite_kind'), 'invite', schema='web', type_='check')
    op.drop_constraint(op.f('ck_invite_one_way_in'), 'invite', schema='web', type_='check')
    op.drop_column('invite', 'email', schema='web')
    op.drop_column('invite', 'kind', schema='web')
    op.drop_column('invite', 'github_account_id', schema='web')
    op.alter_column('invite', 'code_hash',
               existing_type=sa.VARCHAR(length=1024),
               type_=sa.String(length=64),
               nullable=False,
               schema='web')
    op.alter_column('invite', 'expires_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               nullable=False,
               schema='web')
    op.create_unique_constraint(op.f('uq_invite_code_hash'), 'invite', ['code_hash'], schema='web')


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DELETE FROM web.invite")
    op.drop_constraint(op.f('uq_invite_code_hash'), 'invite', schema='web', type_='unique')
    op.alter_column('invite', 'expires_at',
               existing_type=postgresql.TIMESTAMP(timezone=True),
               nullable=True,
               schema='web')
    op.alter_column('invite', 'code_hash',
               existing_type=sa.String(length=64),
               type_=sa.VARCHAR(length=1024),
               nullable=True,
               schema='web')
    op.add_column('invite', sa.Column('github_account_id', sa.VARCHAR(length=320), nullable=True), schema='web')
    op.add_column('invite', sa.Column('kind', sa.VARCHAR(length=6), nullable=False), schema='web')
    op.add_column('invite', sa.Column('email', sa.VARCHAR(length=320), nullable=True), schema='web')
    op.create_check_constraint(
        op.f('ck_invite_one_way_in'), 'invite',
        "(kind = 'email' AND email IS NOT NULL AND code_hash IS NOT NULL AND github_account_id IS NULL)"
        " OR (kind = 'github' AND github_account_id IS NOT NULL AND email IS NULL AND code_hash IS NULL)",
        schema='web',
    )
    op.create_check_constraint(
        op.f('ck_invite_invite_kind'), 'invite', "kind IN ('email', 'github')", schema='web'
    )
