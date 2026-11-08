from celery import Celery

from app.config import Settings


def create_task_queue(settings: Settings) -> Celery:
    if not settings.redis_url:
        raise ValueError("REDIS_URL 尚未配置")
    queue = Celery("document_agent", broker=settings.redis_url)
    queue.conf.update(
        broker_transport_options={"visibility_timeout":settings.celery_visibility_timeout_seconds},
        task_routes={"document_agent.turn":{"queue":"conversation"},
                     "document_agent.continuation":{"queue":"conversation"},
                     "document_agent.run":{"queue":"documents"},
                     "document_agent.summary":{"queue":"maintenance"},
                     "document_agent.purge":{"queue":"maintenance"}},
    )
    return queue
