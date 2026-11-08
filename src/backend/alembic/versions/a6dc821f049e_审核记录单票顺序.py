"""审核记录按单票分配顺序，旧记录仅确定性回填。"""

from alembic import op
import sqlalchemy as sa

revision = "a6dc821f049e"
down_revision = "71db8e66291f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 迁移在拥有DDL/BYPASSRLS的部署角色执行；锁阻止旧写命令穿过回填窗口。
    op.execute("LOCK TABLE cases, field_review_events, issue_dispositions IN ACCESS EXCLUSIVE MODE")
    for table in ("field_review_events", "issue_dispositions"):
        op.add_column(table, sa.Column("review_sequence", sa.Integer(), nullable=True))
    op.execute("""
        WITH ordered AS (
            SELECT kind,id,row_number() OVER (PARTITION BY tenant_id,case_id ORDER BY created_at,kind,id)::integer AS seq
            FROM (
                SELECT 'field' AS kind,id,tenant_id,case_id,created_at FROM field_review_events
                UNION ALL
                SELECT 'issue' AS kind,id,tenant_id,case_id,created_at FROM issue_dispositions
            ) events
        ), fields AS (
            UPDATE field_review_events e SET review_sequence=o.seq FROM ordered o
            WHERE o.kind='field' AND o.id=e.id RETURNING e.id
        )
        UPDATE issue_dispositions e SET review_sequence=o.seq FROM ordered o
        WHERE o.kind='issue' AND o.id=e.id
    """)
    op.execute("""
        UPDATE cases c SET review_state_version=GREATEST(c.review_state_version,events.maximum)
        FROM (
            SELECT tenant_id,case_id,max(review_sequence) AS maximum FROM (
                SELECT tenant_id,case_id,review_sequence FROM field_review_events
                UNION ALL SELECT tenant_id,case_id,review_sequence FROM issue_dispositions
            ) combined GROUP BY tenant_id,case_id
        ) events WHERE c.tenant_id=events.tenant_id AND c.id=events.case_id
    """)
    for table in ("field_review_events", "issue_dispositions"):
        op.alter_column(table,"review_sequence",nullable=False)
        op.create_check_constraint("review_sequence_positive",table,"review_sequence > 0")
        op.create_index(f"uq_{table}_case_review_sequence",table,["tenant_id","case_id","review_sequence"],unique=True)


def downgrade() -> None:
    for table in ("issue_dispositions", "field_review_events"):
        op.drop_index(f"uq_{table}_case_review_sequence",table_name=table)
        op.drop_constraint(op.f(f"ck_{table}_review_sequence_positive"),table,type_="check")
        op.drop_column(table,"review_sequence")
