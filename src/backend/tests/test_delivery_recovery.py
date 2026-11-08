from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.parametrize("committed", [False, True])
def test_export_checkpoint_error_preserves_committed_delivery(monkeypatch, committed):
    from app.exports import worker as exports
    from app.workflows import worker as workflows
    from app.workflows.models import RunStatus

    attempt = SimpleNamespace(id=uuid4(), status="queued")
    run = SimpleNamespace(id=uuid4(), status=RunStatus.EXPORTING)
    closed, events = [], []

    class Worker:
        def __init__(self, run_id, epoch):
            self.settings = None
            self.engine = SimpleNamespace(connect=lambda: Advisory())
        @contextmanager
        def transaction(self):
            yield SimpleNamespace(scalar=lambda query: attempt, scalars=lambda query: [])
        def locked(self, db):
            return run
        def close(self):
            closed.append("worker")

    class Advisory:
        def execute(self, *args):
            return SimpleNamespace(scalar_one=lambda: True)
        def close(self):
            closed.append("advisory")

    class Graph:
        def compile(self, **kwargs):
            return self
        def invoke(self, state, config, **kwargs):
            assert kwargs["durability"] == "sync"
            if committed:
                attempt.status = "succeeded"
                run.status = RunStatus.EXPORTED
            raise RuntimeError("checkpoint unavailable")

    @contextmanager
    def saver(*args):
        yield None

    monkeypatch.setattr(workflows, "DocumentWorker", Worker)
    monkeypatch.setattr(workflows, "append_event", lambda *args: events.append(args))
    monkeypatch.setattr("app.infra.checkpoints.open_checkpointer", saver)
    monkeypatch.setattr("app.infra.checkpoints.checkpoint_database_url", lambda _: "")
    monkeypatch.setattr("app.workflows.graphs.build_export_graph", lambda _: Graph())
    monkeypatch.setattr(exports, "review_gate", lambda *args: {"confirmable": True})
    if committed:
        exports.execute_export(str(run.id), 1)
        assert attempt.status == "succeeded" and run.status is RunStatus.EXPORTED
        assert not events
    else:
        with pytest.raises(RuntimeError, match="checkpoint unavailable"):
            exports.execute_export(str(run.id), 1)
        assert attempt.status == "failed_retryable" and run.status is RunStatus.READY_TO_EXPORT
        assert len(events) == 1
    assert closed == ["advisory", "worker"]


def test_outbox_rotates_tenants_with_one_total_batch_budget(monkeypatch):
    from app.infra import outbox

    tenants = [uuid4() for _ in range(3)]
    rows = {tenant: [SimpleNamespace(topic="turn", payload={"turn_id": str(uuid4())},
        attempts=0, status="pending") for _ in range(5)] for tenant in tenants}
    visited, sent, disposed = [], [], []

    class Engine:
        @contextmanager
        def begin(self):
            yield SimpleNamespace(execute=lambda query: SimpleNamespace(scalars=lambda: tenants))
        def dispose(self):
            disposed.append(True)

    class Session:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, query):
            pass
        def scalars(self, query):
            assert query._limit_clause.value == 1
            available = [row for row in rows[self.tenant] if row.status == "pending"]
            return available[:1]

    @contextmanager
    def transaction(db, tenant):
        db.tenant = tenant
        visited.append(tenant)
        yield db

    class Broker:
        cursor = 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def incrby(self, key, amount):
            self.cursor += amount
            return self.cursor

    engine, broker = Engine(), Broker()
    monkeypatch.setattr(outbox, "create_sync_engine", lambda _: engine)
    monkeypatch.setattr(outbox, "create_sync_session_factory", lambda _: Session)
    monkeypatch.setattr(outbox, "sync_tenant_transaction", transaction)
    monkeypatch.setattr("redis.Redis.from_url", lambda *args, **kwargs: broker)
    monkeypatch.setattr(outbox.queue, "send_task", lambda name, **kwargs: sent.append(kwargs))
    assert outbox.publish_once(batch_size=4) == 4
    assert visited == [tenants[0], tenants[1], tenants[2], tenants[0]]
    visited.clear()
    assert outbox.publish_once(batch_size=4) == 4
    assert visited == [tenants[1], tenants[2], tenants[0], tenants[1]]
    assert len(sent) == 8 and disposed == [True, True]
    assert outbox.publish_once(batch_size=0) == 0
