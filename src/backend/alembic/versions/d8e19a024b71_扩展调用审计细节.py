"""扩展调用安全摘要和时间；历史未知值保持空。"""
from alembic import op
import sqlalchemy as sa

revision = "d8e19a024b71"
down_revision = "c2e84a7d910f"
branch_labels = None
depends_on = None


def upgrade():
    for column in (
        sa.Column("params_redacted", sa.String(2000)),
        sa.Column("destination", sa.String(300)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer),
    ):
        op.add_column("extension_calls", column)
    op.create_check_constraint("duration_nonnegative", "extension_calls", "duration_ms IS NULL OR duration_ms >= 0")


def downgrade():
    op.drop_constraint("ck_extension_calls_duration_nonnegative", "extension_calls", type_="check")
    for name in ("duration_ms", "finished_at", "started_at", "destination", "params_redacted"):
        op.drop_column("extension_calls", name)
