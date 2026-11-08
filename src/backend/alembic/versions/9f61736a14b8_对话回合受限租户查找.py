"""对话回合受限租户查找。"""
from alembic import op

revision = "9f61736a14b8"
down_revision = "e0866a998b46"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE OR REPLACE FUNCTION worker_tenant_of_turn(p_id uuid) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT tenant_id FROM conversation_turns WHERE id = p_id
        $$
    """)
    op.execute("REVOKE ALL ON FUNCTION worker_tenant_of_turn(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION worker_tenant_of_turn(uuid) TO logistics_worker")


def downgrade():
    op.execute("DROP FUNCTION IF EXISTS worker_tenant_of_turn(uuid)")
