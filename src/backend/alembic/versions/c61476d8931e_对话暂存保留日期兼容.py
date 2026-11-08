"""对话保留哨兵兼容asyncpg与psycopg，物理删除规则不变。"""
from alembic import op
import sqlalchemy as sa

revision = "c61476d8931e"
down_revision = "ab304109f9d1"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    for tenant_id in conn.execute(sa.text("SELECT id FROM tenants")).scalars().all():
        conn.execute(sa.text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_id)})
        conn.execute(sa.text("UPDATE conversation_work_items SET expires_at = '9999-12-30 00:00:00+00' WHERE expires_at = 'infinity'::timestamptz"))


def downgrade():
    # 有限哨兵也是旧版本可读的合法日期，不恢复不兼容的infinity。
    pass
