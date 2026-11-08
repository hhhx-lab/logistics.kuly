"""普通用户邮箱注册与全局登录标识唯一性。"""
from alembic import op
import sqlalchemy as sa

revision = "f19a2c7e3104"
down_revision = "54cf908f20a1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("email", sa.String(254), nullable=True))
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("CREATE UNIQUE INDEX uq_users_account_lower ON users (lower(account))")
    op.execute("CREATE UNIQUE INDEX uq_users_email_lower ON users (lower(email))")
    op.execute("GRANT INSERT ON tenants, users TO logistics_app")


def downgrade():
    op.execute("REVOKE INSERT ON tenants, users FROM logistics_app")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_index("uq_users_account_lower", table_name="users")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "email")
