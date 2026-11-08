"""
Alembic 环境。

连接串来自项目 `.env`（同步 psycopg 驱动），不在 alembic.ini 里硬写。
开发与生产环境都只能通过迁移建表，不允许 API 启动时 `create_all()` 偷建。
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import get_settings
from app.infra import models_base
from app.infra.database import sync_database_url

# 导入全部模型模块，让 target_metadata 能收集到所有表
from app.identity import models as _identity_models  # noqa: F401
from app.documents import models as _documents_models  # noqa: F401
from app.documents import evidence_models as _evidence_models  # noqa: F401
from app.workflows import models as _workflow_models  # noqa: F401
from app.exports import models as _exports_models  # noqa: F401
from app.extensions import models as _extensions_models  # noqa: F401
from app.preferences import models as _preferences_models  # noqa: F401
from app.management import models as _management_models  # noqa: F401
from app.review import models as _review_models  # noqa: F401
from app.conversation import models as _conversation_models  # noqa: F401
from app.consignment import models as _consignment_models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", sync_database_url(get_settings()))

target_metadata = models_base.Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and name in {"checkpoints", "checkpoint_writes", "checkpoint_blobs", "checkpoint_migrations"})


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True, include_object=include_object)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
