"""对话图暂存对象：正文不进入检查点。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "76b54c09a221"
down_revision = "0b631cb855ad"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("conversation_work_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversation_turns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index("ix_conversation_work_items_turn_kind", "conversation_work_items", ["tenant_id", "turn_id", "kind"])
    op.create_index("ix_conversation_work_items_tenant_id", "conversation_work_items", ["tenant_id"])
    op.execute("ALTER TABLE conversation_work_items ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE conversation_work_items FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON conversation_work_items USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON conversation_work_items TO logistics_app, logistics_worker")


def downgrade():
    op.drop_table("conversation_work_items")
