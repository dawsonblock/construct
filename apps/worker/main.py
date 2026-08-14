"""Job worker.

Reserves jobs from Redis, dispatches to a handler, records the outcome on the
job record. A handler that raises fails the job — it never leaves a half-written
decision behind, and it never retries automatically, because a silent retry of a
financial preparation step is not safe by default.

v0.4.4: the worker also runs the outbox relay before each reserve cycle. This
ensures jobs enqueued via the transactional outbox are pushed to Redis even if
the enqueuing process didn't run the relay itself.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from construction_ai.jobs.handlers import HANDLERS  # noqa: E402
from construction_ai.jobs.queue import JobQueue  # noqa: E402
from construction_ai.persistence.repositories import Repositories  # noqa: E402

log = logging.getLogger("worker")

_running = True


def _stop(signum, _frame):
    global _running
    log.info("received signal %s, finishing current job then exiting", signum)
    _running = False


def run(queue: JobQueue, *, once: bool = False, timeout: int = 5, relay: bool = True) -> int:
    processed = 0
    while _running:
        # Relay unpublished outbox rows to Redis before trying to reserve.
        if relay:
            try:
                queue.relay_outbox(limit=50)
            except Exception:
                log.exception("outbox relay failed — continuing to reserve")
        job = queue.reserve(timeout=timeout)
        if job is None:
            if once:
                break
            continue
        handler = HANDLERS.get(job.job_type)
        if handler is None:
            queue.fail(job, f"no handler for job type {job.job_type!r}")
            log.error("job %s: unknown type %s", job.job_id, job.job_type)
        else:
            try:
                result = handler(queue.repos, job)
            except Exception as exc:  # noqa: BLE001 - the failure is recorded on the job
                queue.fail(job, f"{type(exc).__name__}: {exc}")
                log.exception("job %s failed", job.job_id)
            else:
                queue.complete(job, result)
                log.info("job %s completed: %s", job.job_id, result.get("status"))
        processed += 1
        if once:
            break
    return processed


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    queue = JobQueue.from_env(Repositories.from_env())
    log.info("worker ready on queue %s", queue.queue_name)
    run(queue)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
