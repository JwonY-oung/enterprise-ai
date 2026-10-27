"""文档处理 Worker：独立进程轮询持久化队列，支持失败重试与断点恢复。

启动：`python -m app.worker`（或 `docker compose` 中作为独立服务运行）。
启动时先恢复中断任务，再循环认领并处理文档。
"""
import logging
import time
from uuid import uuid4

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import Document
from app.services import queue
from app.services.documents import process_document

logger = logging.getLogger("app.worker")


def run() -> None:
    worker_id = uuid4().hex[:8]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    with SessionLocal() as db:
        recovered = queue.recover_interrupted(db)
    logger.info("worker %s started, recovered %d interrupted task(s)", worker_id, recovered)

    while True:
        with SessionLocal() as db:
            try:
                document = queue.claim_next(db, worker_id)
                if document:
                    logger.info("worker %s processing document=%s kb=%s attempts=%d", worker_id, document.id, document.knowledge_base_id, document.attempts)
                    try:
                        process_document(document.id, db)
                        fresh = db.get(Document, document.id)
                        logger.info(
                            "worker %s document=%s finished status=%s chunks=%s",
                            worker_id, document.id, fresh.status if fresh else "missing", len(fresh.chunks) if fresh else 0,
                        )
                    except Exception as exc:
                        logger.exception("worker %s document=%s failed", worker_id, document.id)
                        queue.mark_failed(db, document.id, str(exc))
            except Exception:
                logger.exception("worker %s iteration error; continuing", worker_id)
        time.sleep(settings.worker_poll_interval_seconds)


if __name__ == "__main__":
    run()
