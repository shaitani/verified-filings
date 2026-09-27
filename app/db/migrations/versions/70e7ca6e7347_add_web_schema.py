"""add web schema

The Web Server's tables: users and sign-in, invitations, conversations, jobs,
their traces and feedback. Design: app/api/DESIGN.md section 11. Nothing in
`xbrl` changes.

Hand-corrected from autogenerate (ALEMBIC.md Part 2):
* CREATE SCHEMA / DROP SCHEMA added -- autogenerate does not emit them;
* the library's GUID / TIMESTAMPAware written as the plain UUID / timestamptz
  they compile to, so this file does not import library internals;
* the two status-like columns as text plus a CHECK named by the house
  convention (ck_<table>_<name>), their values spelt out here.

DOWNGRADE DESTROYS EVERY USER, CONVERSATION AND TRACE. Harmless while the
schema is empty; not after.

Revision ID: 70e7ca6e7347
Revises: a8b5b820cf1a
Create Date: 2026-09-27 16:08:30.922230

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '70e7ca6e7347'
down_revision: Union[str, Sequence[str], None] = 'a8b5b820cf1a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE SCHEMA web")
    op.create_table('user',
    sa.Column('is_superuser', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('hashed_password', sa.String(length=1024), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('is_verified', sa.Boolean(), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user')),
    schema='web'
    )
    op.create_index(op.f('ix_web_user_email'), 'user', ['email'], unique=True, schema='web')
    op.create_index('uq_user_email_lower', 'user', [sa.literal_column('lower(email)')], unique=True, schema='web')
    op.create_table('access_token',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('token', sa.String(length=43), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['web.user.id'], name=op.f('fk_access_token_user_id_user'), ondelete='cascade'),
    sa.PrimaryKeyConstraint('token', name=op.f('pk_access_token')),
    schema='web'
    )
    op.create_index(op.f('ix_web_access_token_created_at'), 'access_token', ['created_at'], unique=False, schema='web')
    op.create_table('conversation',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['web.user.id'], name=op.f('fk_conversation_user_id_user'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversation')),
    schema='web'
    )
    op.create_index('ix_conversation_user_created', 'conversation', ['user_id', 'created_at'], unique=False, schema='web')
    op.create_table('invite',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('kind', sa.String(length=6), nullable=False),
    sa.CheckConstraint("kind IN ('email', 'github')", name=op.f('ck_invite_invite_kind')),
    sa.Column('email', sa.String(length=320), nullable=True),
    sa.Column('code_hash', sa.String(length=1024), nullable=True),
    sa.Column('github_account_id', sa.String(length=320), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('used_by', sa.UUID(), nullable=True),
    sa.CheckConstraint("(kind = 'email' AND email IS NOT NULL AND code_hash IS NOT NULL AND github_account_id IS NULL) OR (kind = 'github' AND github_account_id IS NOT NULL AND email IS NULL AND code_hash IS NULL)", name=op.f('ck_invite_one_way_in')),
    sa.CheckConstraint('used_by IS NULL OR used_at IS NOT NULL', name=op.f('ck_invite_used_by_needs_used_at')),
    sa.ForeignKeyConstraint(['used_by'], ['web.user.id'], name=op.f('fk_invite_used_by_user'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_invite')),
    schema='web'
    )
    op.create_table('oauth_account',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('oauth_name', sa.String(length=100), nullable=False),
    sa.Column('access_token', sa.String(length=1024), nullable=False),
    sa.Column('expires_at', sa.Integer(), nullable=True),
    sa.Column('refresh_token', sa.String(length=1024), nullable=True),
    sa.Column('account_id', sa.String(length=320), nullable=False),
    sa.Column('account_email', sa.String(length=320), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['web.user.id'], name=op.f('fk_oauth_account_user_id_user'), ondelete='cascade'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_oauth_account')),
    schema='web'
    )
    op.create_index(op.f('ix_web_oauth_account_account_id'), 'oauth_account', ['account_id'], unique=False, schema='web')
    op.create_index(op.f('ix_web_oauth_account_oauth_name'), 'oauth_account', ['oauth_name'], unique=False, schema='web')
    op.create_table('job',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('conversation_id', sa.UUID(), nullable=False),
    sa.Column('round', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    # app/schemas/job.py JOB_STATUSES; a new status is a migration altering this CHECK.
    sa.CheckConstraint("status IN ('queued', 'parsing', 'mapping', 'fetching', 'presenting', 'done', 'failed')", name=op.f('ck_job_job_status')),
    sa.Column('asks', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('reply', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("(finished_at IS NOT NULL) = (status IN ('done', 'failed'))", name=op.f('ck_job_finished_when_finished')),
    sa.CheckConstraint("(reply IS NOT NULL) = (status = 'done')", name=op.f('ck_job_reply_when_done')),
    sa.CheckConstraint('round >= 1', name=op.f('ck_job_round_positive')),
    sa.ForeignKeyConstraint(['conversation_id'], ['web.conversation.id'], name=op.f('fk_job_conversation_id_conversation'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job')),
    sa.UniqueConstraint('conversation_id', 'round', name='uq_job_conversation_round'),
    schema='web'
    )
    op.create_table('job_feedback',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['web.job.id'], name=op.f('fk_job_feedback_job_id_job'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['web.user.id'], name=op.f('fk_job_feedback_user_id_user'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_feedback')),
    schema='web'
    )
    op.create_index(op.f('ix_web_job_feedback_job_id'), 'job_feedback', ['job_id'], unique=False, schema='web')
    op.create_table('job_trace',
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('code_version', sa.String(length=64), nullable=False),
    sa.Column('models', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('query_in', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('plan', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('model_calls', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('statements', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('timings', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('errors', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['web.job.id'], name=op.f('fk_job_trace_job_id_job'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('job_id', name=op.f('pk_job_trace')),
    schema='web'
    )


def downgrade() -> None:
    """Downgrade schema. Destroys all web data -- see the module docstring."""
    op.drop_table('job_trace', schema='web')
    op.drop_index(op.f('ix_web_job_feedback_job_id'), table_name='job_feedback', schema='web')
    op.drop_table('job_feedback', schema='web')
    op.drop_table('job', schema='web')
    op.drop_index(op.f('ix_web_oauth_account_oauth_name'), table_name='oauth_account', schema='web')
    op.drop_index(op.f('ix_web_oauth_account_account_id'), table_name='oauth_account', schema='web')
    op.drop_table('oauth_account', schema='web')
    op.drop_table('invite', schema='web')
    op.drop_index('ix_conversation_user_created', table_name='conversation', schema='web')
    op.drop_table('conversation', schema='web')
    op.drop_index(op.f('ix_web_access_token_created_at'), table_name='access_token', schema='web')
    op.drop_table('access_token', schema='web')
    op.drop_index('uq_user_email_lower', table_name='user', schema='web')
    op.drop_index(op.f('ix_web_user_email'), table_name='user', schema='web')
    op.drop_table('user', schema='web')
    op.execute("DROP SCHEMA web")  # no CASCADE: anything unexpected left fails loudly
