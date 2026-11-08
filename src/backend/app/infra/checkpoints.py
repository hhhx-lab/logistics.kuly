from contextlib import contextmanager
from typing import Iterator

from langgraph.checkpoint.postgres import PostgresSaver


def checkpoint_database_url(settings) -> str:
    if not settings.checkpoint_database_url:raise ValueError('CHECKPOINT_DATABASE_URL 尚未配置')
    return settings.checkpoint_database_url.replace('postgresql+psycopg://','postgresql://',1).replace('postgresql+asyncpg://','postgresql://',1)


@contextmanager
def open_checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    with PostgresSaver.from_conn_string(database_url) as checkpointer:
        yield checkpointer


def initialize_checkpointer(database_url: str) -> None:
    with open_checkpointer(database_url) as checkpointer:
        checkpointer.setup()
