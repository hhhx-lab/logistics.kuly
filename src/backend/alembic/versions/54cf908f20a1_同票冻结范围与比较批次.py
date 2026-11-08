"""同票冻结范围、关系、分组合计与比较批次。"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = "54cf908f20a1"
down_revision = "d750d2ab46ce"
branch_labels = None
depends_on = None


def upgrade():
    op.execute('\nCREATE TABLE case_scope_revisions (\n\tcase_id UUID NOT NULL, \n\trevision_no INTEGER NOT NULL, \n\tparent_revision_id UUID, \n\tpayload JSONB NOT NULL, \n\tfingerprint VARCHAR(64) NOT NULL, \n\tcreated_by UUID NOT NULL, \n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_case_scope_revisions PRIMARY KEY (id), \n\tCONSTRAINT uq_case_scope_revisions_tenant_id_case_id_id UNIQUE (tenant_id, case_id, id), \n\tCONSTRAINT uq_case_scope_revisions_tenant_id_case_id_revision_no UNIQUE (tenant_id, case_id, revision_no), \n\tCONSTRAINT fk_case_scope_revisions_case_id FOREIGN KEY(case_id) REFERENCES cases (id) ON DELETE CASCADE, \n\tCONSTRAINT fk_case_scope_revisions_parent_revision_id FOREIGN KEY(parent_revision_id) REFERENCES case_scope_revisions (id)\n)\n\n')
    op.execute('CREATE INDEX ix_case_scope_revisions_tenant_id ON case_scope_revisions (tenant_id)')
    op.execute('\nCREATE TABLE relation_assertions (\n\tcase_id UUID NOT NULL, \n\tscope_revision_id UUID NOT NULL, \n\tkind VARCHAR(40) NOT NULL, \n\tstate VARCHAR(24) NOT NULL, \n\tleft_logical_id UUID NOT NULL, \n\tright_logical_id UUID NOT NULL, \n\tbasis JSONB NOT NULL, \n\tsupersedes_id UUID, \n\tdecided_by UUID, \n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_relation_assertions PRIMARY KEY (id), \n\tCONSTRAINT uq_relation_assertions_tenant_id_case_id_id UNIQUE (tenant_id, case_id, id), \n\tCONSTRAINT fk_relation_assertions_tenant_id_case_id_scope_revision_id FOREIGN KEY(tenant_id, case_id, scope_revision_id) REFERENCES case_scope_revisions (tenant_id, case_id, id), \n\tCONSTRAINT fk_relation_assertions_case_id FOREIGN KEY(case_id) REFERENCES cases (id) ON DELETE CASCADE, \n\tCONSTRAINT fk_relation_assertions_left_logical_id FOREIGN KEY(left_logical_id) REFERENCES logical_documents (id), \n\tCONSTRAINT fk_relation_assertions_right_logical_id FOREIGN KEY(right_logical_id) REFERENCES logical_documents (id), \n\tCONSTRAINT fk_relation_assertions_supersedes_id FOREIGN KEY(supersedes_id) REFERENCES relation_assertions (id)\n)\n\n')
    op.execute('CREATE INDEX ix_relation_assertions_tenant_id ON relation_assertions (tenant_id)')
    op.execute('\nCREATE TABLE cargo_match_groups (\n\tcase_id UUID NOT NULL, \n\tscope_revision_id UUID NOT NULL, \n\trelation_id UUID, \n\tstate VARCHAR(24) NOT NULL, \n\tcardinality VARCHAR(12) NOT NULL, \n\tscope JSONB NOT NULL, \n\tbasis JSONB NOT NULL, \n\tsupersedes_id UUID, \n\tdecided_by UUID, \n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_cargo_match_groups PRIMARY KEY (id), \n\tCONSTRAINT uq_cargo_match_groups_tenant_id_case_id_id UNIQUE (tenant_id, case_id, id), \n\tCONSTRAINT fk_cargo_match_groups_tenant_id_case_id_scope_revision_id FOREIGN KEY(tenant_id, case_id, scope_revision_id) REFERENCES case_scope_revisions (tenant_id, case_id, id), \n\tCONSTRAINT fk_cargo_match_groups_case_id FOREIGN KEY(case_id) REFERENCES cases (id) ON DELETE CASCADE, \n\tCONSTRAINT fk_cargo_match_groups_relation_id FOREIGN KEY(relation_id) REFERENCES relation_assertions (id), \n\tCONSTRAINT fk_cargo_match_groups_supersedes_id FOREIGN KEY(supersedes_id) REFERENCES cargo_match_groups (id)\n)\n\n')
    op.execute('CREATE INDEX ix_cargo_match_groups_tenant_id ON cargo_match_groups (tenant_id)')
    op.execute("\nCREATE TABLE cargo_match_members (\n\tcase_id UUID NOT NULL, \n\tgroup_id UUID NOT NULL, \n\tside VARCHAR(8) NOT NULL, \n\tline_item_id UUID NOT NULL, \n\tbasis JSONB NOT NULL, \n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_cargo_match_members PRIMARY KEY (id), \n\tCONSTRAINT uq_cargo_match_members_tenant_id_group_id_side_line_item_id UNIQUE (tenant_id, group_id, side, line_item_id), \n\tCONSTRAINT ck_cargo_match_members_member_side CHECK (side IN ('left','right')), \n\tCONSTRAINT fk_cargo_match_members_tenant_id_case_id_group_id FOREIGN KEY(tenant_id, case_id, group_id) REFERENCES cargo_match_groups (tenant_id, case_id, id) ON DELETE CASCADE, \n\tCONSTRAINT fk_cargo_match_members_case_id FOREIGN KEY(case_id) REFERENCES cases (id) ON DELETE CASCADE, \n\tCONSTRAINT fk_cargo_match_members_line_item_id FOREIGN KEY(line_item_id) REFERENCES line_items (id)\n)\n\n")
    op.execute('CREATE INDEX ix_cargo_match_members_tenant_id ON cargo_match_members (tenant_id)')
    op.execute('\nCREATE TABLE comparison_batches (\n\tcase_id UUID NOT NULL, \n\trun_id UUID NOT NULL, \n\tepoch INTEGER NOT NULL, \n\tscope_revision_id UUID NOT NULL, \n\tbusiness_revision_id UUID NOT NULL, \n\tparent_batch_id UUID, \n\tstatus VARCHAR(16) NOT NULL, \n\tmanifest JSONB NOT NULL, \n\tinput_fingerprint VARCHAR(64) NOT NULL, \n\tsummary JSONB NOT NULL, \n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_comparison_batches PRIMARY KEY (id), \n\tCONSTRAINT uq_comparison_batches_tenant_id_case_id_id UNIQUE (tenant_id, case_id, id), \n\tCONSTRAINT uq_comparison_batches_tenant_id_run_id_epoch UNIQUE (tenant_id, run_id, epoch), \n\tCONSTRAINT fk_comparison_batches_tenant_id_case_id_run_id FOREIGN KEY(tenant_id, case_id, run_id) REFERENCES runs (tenant_id, case_id, id) ON DELETE CASCADE, \n\tCONSTRAINT fk_comparison_batches_tenant_id_case_id_scope_revision_id FOREIGN KEY(tenant_id, case_id, scope_revision_id) REFERENCES case_scope_revisions (tenant_id, case_id, id), \n\tCONSTRAINT fk_comparison_batches_case_id FOREIGN KEY(case_id) REFERENCES cases (id) ON DELETE CASCADE, \n\tCONSTRAINT fk_comparison_batches_business_revision_id FOREIGN KEY(business_revision_id) REFERENCES business_revisions (id), \n\tCONSTRAINT fk_comparison_batches_parent_batch_id FOREIGN KEY(parent_batch_id) REFERENCES comparison_batches (id)\n)\n\n')
    op.execute('CREATE INDEX ix_comparison_batches_tenant_id ON comparison_batches (tenant_id)')
    op.execute('\nCREATE TABLE run_comparison_bindings (\n\tcase_id UUID NOT NULL, \n\trun_id UUID NOT NULL, \n\tbatch_id UUID NOT NULL, \n\tdelivery_document_ids JSONB NOT NULL, \n\tdependency_fingerprint VARCHAR(64) NOT NULL, \n\tsupersedes_id UUID, \n\tid UUID NOT NULL, \n\ttenant_id UUID NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tCONSTRAINT pk_run_comparison_bindings PRIMARY KEY (id), \n\tCONSTRAINT uq_run_comparison_bindings_tenant_id_case_id_id UNIQUE (tenant_id, case_id, id), \n\tCONSTRAINT fk_run_comparison_bindings_tenant_id_case_id_run_id FOREIGN KEY(tenant_id, case_id, run_id) REFERENCES runs (tenant_id, case_id, id) ON DELETE CASCADE, \n\tCONSTRAINT fk_run_comparison_bindings_tenant_id_case_id_batch_id FOREIGN KEY(tenant_id, case_id, batch_id) REFERENCES comparison_batches (tenant_id, case_id, id), \n\tCONSTRAINT fk_run_comparison_bindings_case_id FOREIGN KEY(case_id) REFERENCES cases (id) ON DELETE CASCADE, \n\tCONSTRAINT fk_run_comparison_bindings_supersedes_id FOREIGN KEY(supersedes_id) REFERENCES run_comparison_bindings (id)\n)\n\n')
    op.execute('CREATE INDEX ix_run_comparison_bindings_tenant_id ON run_comparison_bindings (tenant_id)')
    for table, column in (("cases","current_scope_revision_id"),("cases","current_comparison_batch_id"),("runs","current_comparison_binding_id"),("rule_checks","comparison_batch_id"),("issues","comparison_batch_id")):
        op.add_column(table,sa.Column(column,postgresql.UUID(as_uuid=True),nullable=True))
    for column in ("comparison_basis","stable_check_key"):
        op.add_column("rule_checks",sa.Column(column,sa.String(12 if column=="comparison_basis" else 64),nullable=True))
    op.add_column("issues",sa.Column("stable_issue_key",sa.String(64),nullable=True))
    for column in ("sku","specification","lot_no","source_item_no","package_marks","package_range","country_of_origin"):
        op.add_column("line_items",sa.Column(column,sa.Text(),nullable=True))
    for column in ("net_weight_unit","gross_weight_unit","chargeable_weight_unit"):
        op.add_column("line_items",sa.Column(column,sa.String(16),nullable=True))
    op.add_column("line_items",sa.Column("chargeable_weight",sa.Numeric(20,6),nullable=True))
    op.add_column("line_items",sa.Column("measurements",postgresql.JSONB(),nullable=False,server_default=sa.text("'[]'::jsonb")))
    op.add_column("line_items",sa.Column("cell_sources",postgresql.JSONB(),nullable=False,server_default=sa.text("'{}'::jsonb")))
    op.execute("CREATE UNIQUE INDEX uq_comparison_checks ON rule_checks(tenant_id,comparison_batch_id,rule_id,rule_version,stable_check_key,comparison_basis) WHERE comparison_batch_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX uq_comparison_issues ON issues(tenant_id,comparison_batch_id,rule_check_id,stable_issue_key) WHERE comparison_batch_id IS NOT NULL")
    for table in ("rule_checks","issues"):
        op.create_foreign_key("fk_"+table+"_comparison_batch_same_case",table,"comparison_batches",["tenant_id","case_id","comparison_batch_id"],["tenant_id","case_id","id"],ondelete="CASCADE")
    op.execute("ALTER TABLE rule_checks ADD CONSTRAINT ck_comparison_basis CHECK (comparison_batch_id IS NULL OR (comparison_basis IN ('source','adopted') AND stable_check_key IS NOT NULL))")
    for table in ("case_scope_revisions","relation_assertions","cargo_match_groups","cargo_match_members","comparison_batches","run_comparison_bindings"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_tenant_isolation ON {table} USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid) WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid)")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO logistics_app, logistics_worker")
        if table!="comparison_batches":
            op.execute(f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION forbid_row_update()")
    op.execute("""
        CREATE FUNCTION guard_consignment_batch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.case_id IS DISTINCT FROM OLD.case_id OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.run_id IS DISTINCT FROM OLD.run_id OR NEW.epoch IS DISTINCT FROM OLD.epoch
               OR NEW.scope_revision_id IS DISTINCT FROM OLD.scope_revision_id
               OR NEW.business_revision_id IS DISTINCT FROM OLD.business_revision_id
               OR NEW.manifest IS DISTINCT FROM OLD.manifest
               OR NEW.input_fingerprint IS DISTINCT FROM OLD.input_fingerprint
               OR OLD.status <> 'building' THEN
                RAISE EXCEPTION 'Comparison batch inputs and completed results are immutable' USING ERRCODE='check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_comparison_batch_immutable BEFORE UPDATE ON comparison_batches FOR EACH ROW EXECUTE FUNCTION guard_consignment_batch();
        CREATE FUNCTION guard_comparison_result() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.comparison_batch_id IS NOT NULL THEN
                RAISE EXCEPTION 'Comparison result is append only' USING ERRCODE='check_violation';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_comparison_check_immutable BEFORE UPDATE ON rule_checks FOR EACH ROW EXECUTE FUNCTION guard_comparison_result();
        CREATE TRIGGER trg_comparison_issue_immutable BEFORE UPDATE ON issues FOR EACH ROW EXECUTE FUNCTION guard_comparison_result();
        CREATE FUNCTION guard_consignment_ownership() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE t uuid; c uuid; r uuid; b uuid; st text;
        BEGIN
            SELECT tenant_id INTO t FROM cases WHERE id=NEW.case_id;
            IF t IS DISTINCT FROM NEW.tenant_id THEN RAISE EXCEPTION 'Case tenant mismatch'; END IF;
            IF TG_TABLE_NAME='case_scope_revisions' THEN
                IF NEW.parent_revision_id IS NOT NULL THEN
                    SELECT tenant_id,case_id INTO t,c FROM case_scope_revisions WHERE id=NEW.parent_revision_id;
                ELSE RETURN NEW; END IF;
            ELSIF TG_TABLE_NAME='cargo_match_groups' THEN
                IF NEW.relation_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM relation_assertions WHERE id=NEW.relation_id AND tenant_id=NEW.tenant_id AND case_id=NEW.case_id) THEN RAISE EXCEPTION 'Cargo relation outside Case'; END IF;
                IF NEW.supersedes_id IS NOT NULL THEN
                    SELECT tenant_id,case_id INTO t,c FROM cargo_match_groups WHERE id=NEW.supersedes_id;
                ELSE RETURN NEW; END IF;
            ELSIF TG_TABLE_NAME='run_comparison_bindings' THEN
                IF NEW.supersedes_id IS NOT NULL THEN
                    SELECT tenant_id,case_id INTO t,c FROM run_comparison_bindings WHERE id=NEW.supersedes_id;
                ELSE RETURN NEW; END IF;
            ELSIF TG_TABLE_NAME='relation_assertions' THEN
                IF NEW.supersedes_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM relation_assertions WHERE id=NEW.supersedes_id AND tenant_id=NEW.tenant_id AND case_id=NEW.case_id) THEN RAISE EXCEPTION 'Relation predecessor outside Case'; END IF;
                SELECT tenant_id,case_id INTO t,c FROM logical_documents WHERE id=NEW.left_logical_id;
                IF t IS DISTINCT FROM NEW.tenant_id OR c IS DISTINCT FROM NEW.case_id THEN RAISE EXCEPTION 'Left document outside Case'; END IF;
                SELECT tenant_id,case_id INTO t,c FROM logical_documents WHERE id=NEW.right_logical_id;
            ELSIF TG_TABLE_NAME='cargo_match_members' THEN
                SELECT tenant_id,case_id INTO t,c FROM line_items WHERE id=NEW.line_item_id;
            ELSIF TG_TABLE_NAME='comparison_batches' THEN
                IF NEW.parent_batch_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM comparison_batches WHERE id=NEW.parent_batch_id AND tenant_id=NEW.tenant_id AND case_id=NEW.case_id) THEN RAISE EXCEPTION 'Batch predecessor outside Case'; END IF;
                SELECT tenant_id,case_id INTO t,c FROM business_revisions WHERE id=NEW.business_revision_id;
            ELSIF TG_TABLE_NAME='issues' THEN
                IF NEW.comparison_batch_id IS NULL THEN RETURN NEW; END IF;
                SELECT tenant_id,case_id,run_id,business_revision_id,status INTO t,c,r,b,st FROM comparison_batches WHERE id=NEW.comparison_batch_id;
                IF st <> 'building' THEN RAISE EXCEPTION 'Cannot add results to published batch'; END IF;
                IF NEW.run_id IS DISTINCT FROM r OR NEW.business_revision_id IS DISTINCT FROM b THEN RAISE EXCEPTION 'Issue outside batch owner'; END IF;
                IF NOT EXISTS(SELECT 1 FROM rule_checks WHERE id=NEW.rule_check_id AND comparison_batch_id=NEW.comparison_batch_id AND tenant_id=NEW.tenant_id AND case_id=NEW.case_id) THEN RAISE EXCEPTION 'Issue check outside batch'; END IF;
            ELSIF TG_TABLE_NAME='rule_checks' THEN
                IF NEW.comparison_batch_id IS NULL THEN RETURN NEW; END IF;
                SELECT tenant_id,case_id,run_id,business_revision_id,status INTO t,c,r,b,st FROM comparison_batches WHERE id=NEW.comparison_batch_id;
                IF st <> 'building' THEN RAISE EXCEPTION 'Cannot add results to published batch'; END IF;
                IF NEW.run_id IS DISTINCT FROM r OR NEW.business_revision_id IS DISTINCT FROM b THEN RAISE EXCEPTION 'Check outside batch owner'; END IF;
            END IF;
            IF t IS DISTINCT FROM NEW.tenant_id OR c IS DISTINCT FROM NEW.case_id THEN RAISE EXCEPTION 'Consignment ownership mismatch'; END IF;
            RETURN NEW;
        END $$;
    """)
    for table in ("case_scope_revisions","relation_assertions","cargo_match_groups","cargo_match_members","comparison_batches","run_comparison_bindings","rule_checks","issues"):
        op.execute(f"CREATE TRIGGER trg_{table}_consignment_owner BEFORE INSERT ON {table} FOR EACH ROW EXECUTE FUNCTION guard_consignment_ownership()")

    op.execute("""
        CREATE FUNCTION guard_consignment_pointer() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME='cases' THEN
                IF NEW.current_scope_revision_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM case_scope_revisions WHERE id=NEW.current_scope_revision_id AND case_id=NEW.id AND tenant_id=NEW.tenant_id) THEN RAISE EXCEPTION 'Scope pointer outside Case'; END IF;
                IF NEW.current_comparison_batch_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM comparison_batches WHERE id=NEW.current_comparison_batch_id AND case_id=NEW.id AND tenant_id=NEW.tenant_id) THEN RAISE EXCEPTION 'Comparison pointer outside Case'; END IF;
            ELSE
                IF NEW.current_comparison_binding_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM run_comparison_bindings WHERE id=NEW.current_comparison_binding_id AND run_id=NEW.id AND case_id=NEW.case_id AND tenant_id=NEW.tenant_id) THEN RAISE EXCEPTION 'Binding pointer outside Run'; END IF;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER trg_case_consignment_pointer BEFORE INSERT OR UPDATE ON cases FOR EACH ROW EXECUTE FUNCTION guard_consignment_pointer();
        CREATE TRIGGER trg_run_consignment_pointer BEFORE INSERT OR UPDATE ON runs FOR EACH ROW EXECUTE FUNCTION guard_consignment_pointer();
    """)


def downgrade():
    op.execute("DROP TRIGGER trg_case_consignment_pointer ON cases")
    op.execute("DROP TRIGGER trg_run_consignment_pointer ON runs")
    op.execute("DROP FUNCTION guard_consignment_pointer()")
    for table in ("case_scope_revisions","relation_assertions","cargo_match_groups","cargo_match_members","comparison_batches","run_comparison_bindings","rule_checks","issues"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_consignment_owner ON {table}")
    op.execute("DROP TRIGGER IF EXISTS trg_comparison_check_immutable ON rule_checks")
    op.execute("DROP TRIGGER IF EXISTS trg_comparison_issue_immutable ON issues")
    op.execute("DROP FUNCTION guard_consignment_ownership()")
    op.execute("DROP FUNCTION guard_comparison_result()")
    op.execute("DROP TRIGGER trg_comparison_batch_immutable ON comparison_batches")
    op.execute("DROP FUNCTION guard_consignment_batch()")
    op.execute("DROP INDEX uq_comparison_checks")
    op.execute("DROP INDEX uq_comparison_issues")
    op.execute("ALTER TABLE rule_checks DROP CONSTRAINT ck_comparison_basis")
    for table in ("rule_checks","issues"):
        op.drop_constraint("fk_"+table+"_comparison_batch_same_case",table,type_="foreignkey")
    for table,column in (("issues","stable_issue_key"),("issues","comparison_batch_id"),("rule_checks","stable_check_key"),("rule_checks","comparison_basis"),("rule_checks","comparison_batch_id"),("runs","current_comparison_binding_id"),("cases","current_comparison_batch_id"),("cases","current_scope_revision_id")):
        op.drop_column(table,column)
    for column in ("sku","specification","lot_no","source_item_no","package_marks","package_range","country_of_origin","net_weight_unit","gross_weight_unit","chargeable_weight_unit","chargeable_weight","measurements","cell_sources"):
        op.drop_column("line_items",column)
    for table in ("run_comparison_bindings","comparison_batches","cargo_match_members","cargo_match_groups","relation_assertions","case_scope_revisions"):
        op.drop_table(table)
