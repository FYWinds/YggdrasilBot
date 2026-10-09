"""self ops: invocation reply id, private admin.ops trigger

Revision ID: b7e2c91f4a3d
Revises: a1c4e7d2b9f0
Create Date: 2026-10-09 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2c91f4a3d"
down_revision: str | Sequence[str] | None = "a1c4e7d2b9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invocations", sa.Column("reply_qq_message_id", sa.BigInteger(), nullable=True))
    # Existing installs were seeded before admin.ops existed; fresh ones get it from seed.py.
    # Column defaults live on the ORM side, so every column is spelled out.
    op.execute(
        """
        INSERT INTO command_triggers (handler, scope, group_id, match_type, patterns, arg_template,
            require_mention, target, priority, enabled, cooldown_s, allowed_roles, note, created_at, updated_at)
        SELECT 'admin.ops', 'private', NULL, 'regex', ARRAY['\\S'], NULL, false, 'quoted_or_self', 50, true, 0,
               ARRAY['superuser'], '超级管理员私聊：自然语言查询和修改配置', now(), now()
        WHERE EXISTS (SELECT 1 FROM command_triggers)
          AND NOT EXISTS (SELECT 1 FROM command_triggers WHERE handler = 'admin.ops')
        """
    )


def downgrade() -> None:
    op.execute("DELETE FROM command_triggers WHERE handler = 'admin.ops'")
    op.drop_column("invocations", "reply_qq_message_id")
