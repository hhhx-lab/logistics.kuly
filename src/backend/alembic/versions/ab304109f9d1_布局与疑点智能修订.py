"""保留解析布局、疑点建议与执行批次局部修改要求。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision="ab304109f9d1"
down_revision="ea615b08c932"
branch_labels=None
depends_on=None

def upgrade():
    for table,name in [("parse_artifacts","layout_manifest"),("issues","guidance"),("execution_epochs","repair_request")]:
        op.add_column(table,sa.Column(name,postgresql.JSONB(),nullable=False,server_default=sa.text("'{}'::jsonb")))

def downgrade():
    for table,name in [("execution_epochs","repair_request"),("issues","guidance"),("parse_artifacts","layout_manifest")]:op.drop_column(table,name)
