"""会话归档独立于业务结束状态，删除先撤权再清理。"""
from alembic import op
import sqlalchemy as sa

revision = "ea615b08c932"
down_revision = "d8e19a024b71"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("archived_at", "deleted_at", "purge_after"):
        op.add_column("conversations", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_conversations_purge_after", "conversations", ["purge_after"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"))


def downgrade():
    op.drop_index("ix_conversations_purge_after", table_name="conversations")
    for name in ("purge_after", "deleted_at", "archived_at"):
        op.drop_column("conversations", name)
