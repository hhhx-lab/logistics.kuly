"""保存接收材料时确认的非秘密政策快照。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "71db8e66291f"
down_revision = "b562c73f9081"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("upload_sessions", sa.Column("disclosure_snapshot", postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column("upload_sessions", "disclosure_snapshot")
