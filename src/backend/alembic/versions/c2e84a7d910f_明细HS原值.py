"""保存原件明细HS字符串；旧行保持空值，不根据货描回填。"""
from alembic import op
import sqlalchemy as sa

revision = "c2e84a7d910f"
down_revision = "b7f9c30d81ab"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("line_items", sa.Column("hs_code", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("line_items", "hs_code")
