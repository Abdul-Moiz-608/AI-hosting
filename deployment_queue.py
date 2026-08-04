from __future__ import annotations

import importlib
import logging
import queue
import threading

try:
    from .config import get_settings
except ImportError:
    from config import get_settings

logger = logging.getLogger(__name__)

_MEMORY_QUEUE: queue.Queue[str] = queue.Queue()
_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


class QueueUnavailable(RuntimeError):
    pass


def _memory_queue_worker() -> None:
    """Processes enqueued deployment jobs in the background."""
    while True:
        job_id = _MEMORY_QUEUE.get()
        try:
            deploy_job = None
            module_names = ["code_deployment", "deployer", "services"]

            if __package__:
                module_names = [f"{__package__}.{m}" for m in module_names] + module_names

            for module_name in module_names:
                try:
                    mod = importlib.import_module(module_name)
                    deploy_job = getattr(mod, "process_deployment_job", None) or getattr(mod, "deploy_job", None)
                    if deploy_job:
                        break
                except (ImportError, ModuleNotFoundError):
                    continue

            if deploy_job is None:
                logger.warning("No deployment processor module found. Job %s queued in dry-run mode.", job_id)
                continue

            deploy_job(job_id)
        except Exception:
            logger.exception("Failed to process deployment job %s", job_id)
        finally:
            _MEMORY_QUEUE.task_done()


def _ensure_worker_started() -> None:
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        with _WORKER_LOCK:
            if not _WORKER_STARTED:
                thread = threading.Thread(target=_memory_queue_worker, daemon=True)
                thread.start()
                _WORKER_STARTED = True


def enqueue_job(job_id: str) -> None:
    redis_url = get_settings().redis_url.strip()

    # 1. If REDIS_URL is explicitly empty, use in-memory queue
    if not redis_url:
        _enqueue_memory(job_id)
        return

    # 2. Attempt Redis queueing with graceful fallback if Redis server is down/timing out
    try:
        import redis

        client = redis.Redis.from_url(
            redis_url, socket_connect_timeout=1, socket_timeout=1, decode_responses=True
        )
        client.ping()
        client.rpush("deployment-jobs", job_id)
        logger.info("Enqueued job %s to Redis queue", job_id)
    except Exception as exc:
        logger.warning(
            "Redis queue connection failed (%s). Falling back to in-memory queue for job %s",
            exc,
            job_id,
        )
        _enqueue_memory(job_id)


def _enqueue_memory(job_id: str) -> None:
    """Helper to enqueue jobs into the local thread worker."""
    _ensure_worker_started()
    _MEMORY_QUEUE.put(job_id)
    logger.info("Enqueued job %s to local background queue", job_id)