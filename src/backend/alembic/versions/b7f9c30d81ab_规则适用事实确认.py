"""规则适用事实按业务版本保存，强制租户隔离与不可变审计。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b7f9c30d81ab"
down_revision = "a6dc821f049e"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("rule_input_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("case_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("business_revision_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("business_revisions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False), sa.Column("scope_key", sa.String(200), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False), sa.Column("basis", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("decided_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "business_revision_id", "kind", "scope_key"))
    op.create_index("ix_rule_input_decisions_tenant_id", "rule_input_decisions", ["tenant_id"])
    op.execute("ALTER TABLE rule_input_decisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE rule_input_decisions FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON rule_input_decisions USING (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid) WITH CHECK (tenant_id = nullif(current_setting('app.tenant_id', true), '')::uuid)")
    op.execute("GRANT SELECT, INSERT, DELETE ON rule_input_decisions TO logistics_app, logistics_worker")
    op.execute("REVOKE UPDATE ON rule_input_decisions FROM logistics_app, logistics_worker")


def downgrade():
    op.drop_table("rule_input_decisions")
