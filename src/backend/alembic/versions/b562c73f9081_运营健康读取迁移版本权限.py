"""运营健康读取迁移版本权限。"""

from alembic import op

revision = "b562c73f9081"
down_revision = "e84adb13b0b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("GRANT SELECT ON public.alembic_version TO logistics_app")


def downgrade() -> None:
    op.execute("REVOKE SELECT ON public.alembic_version FROM logistics_app")
