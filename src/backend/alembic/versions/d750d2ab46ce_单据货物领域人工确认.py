"""单据货物领域人工确认。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = "d750d2ab46ce"
down_revision = "c61476d8931e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("section_assignment_decisions", sa.Column("cargo_profile_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.create_check_constraint("cargo_profiles_array", "section_assignment_decisions", "jsonb_typeof(cargo_profile_ids) = 'array' AND jsonb_array_length(cargo_profile_ids) <= 16")


def downgrade():
    op.drop_constraint("cargo_profiles_array", "section_assignment_decisions", type_="check")
    op.drop_column("section_assignment_decisions", "cargo_profile_ids")
