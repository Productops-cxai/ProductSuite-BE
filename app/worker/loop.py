"""Main worker loop: LISTEN/NOTIFY + periodic due sweep + thread pool."""

from __future__ import annotations

import logging
import os
import select
import signal
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.config import settings
from app.infrastructure.database.session import SessionLocal, engine
from app.worker.claim import claim_due_jobs, reclaim_stale_running_jobs
from app.worker.handlers import process_job

logger = logging.getLogger("app.worker")

_stop = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _worker_id() -> str:
    configured = (settings.WORKFLOW_WORKER_ID or "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _handle_signal(signum, _frame) -> None:  # noqa: ANN001
    global _stop
    logger.info("signal %s received — stopping worker", signum)
    _stop = True


def _drain_due(
    worker_id: str,
    pool: ThreadPoolExecutor,
    in_flight: set,
    *,
    stale_seconds: int,
) -> None:
    db = SessionLocal()
    try:
        # Crash / dead-worker recovery: RUNNING locks older than stale window
        # go back to PENDING so they can be claimed again.
        reclaim_stale_running_jobs(
            db,
            stale_seconds=stale_seconds,
            batch_size=max(50, int(settings.WORKFLOW_WORKER_BATCH_SIZE or 50) * 4),
        )
        job_ids = claim_due_jobs(
            db,
            worker_id=worker_id,
            batch_size=int(settings.WORKFLOW_WORKER_BATCH_SIZE or 50),
        )
    finally:
        db.close()

    for job_id in job_ids:
        fut = pool.submit(process_job, job_id)
        in_flight.add(fut)
        fut.add_done_callback(in_flight.discard)


def run_worker() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    global _stop
    _stop = False
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    worker_id = _worker_id()
    concurrency = max(1, int(settings.WORKFLOW_WORKER_CONCURRENCY or 8))
    sweep_seconds = max(5, int(settings.WORKFLOW_WORKER_SWEEP_SECONDS or 20))
    stale_seconds = max(30, int(settings.WORKFLOW_WORKER_STALE_SECONDS or 300))
    channel = settings.WORKFLOW_JOB_NOTIFY_CHANNEL or "workflow_jobs"

    logger.info(
        "PayFlow workflow worker starting id=%s concurrency=%s sweep=%ss stale=%ss channel=%s",
        worker_id,
        concurrency,
        sweep_seconds,
        stale_seconds,
        channel,
    )

    in_flight: set = set()
    last_sweep = 0.0

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="wf") as pool:
        # Initial drain
        _drain_due(worker_id, pool, in_flight, stale_seconds=stale_seconds)

        # Dedicated connection for LISTEN (psycopg2).
        raw = engine.raw_connection()
        try:
            raw.set_isolation_level(0)  # AUTOCOMMIT required for LISTEN
            with raw.cursor() as cur:
                cur.execute(f"LISTEN {channel}")
            logger.info("LISTEN %s armed", channel)

            while not _stop:
                now = time.monotonic()
                if now - last_sweep >= sweep_seconds:
                    _drain_due(worker_id, pool, in_flight, stale_seconds=stale_seconds)
                    last_sweep = now

                # Wait for notify or timeout (sweep cadence).
                try:
                    ready, _, _ = select.select([raw], [], [], min(5.0, float(sweep_seconds)))
                except (ValueError, OSError):
                    # Connection closed / interrupted
                    if _stop:
                        break
                    time.sleep(1)
                    continue

                if ready:
                    raw.poll()
                    notified = bool(raw.notifies)
                    while raw.notifies:
                        raw.notifies.pop(0)
                    if notified:
                        _drain_due(worker_id, pool, in_flight, stale_seconds=stale_seconds)
                        last_sweep = time.monotonic()

                # Reap completed futures without blocking the loop hard.
                if in_flight:
                    done, _ = wait(list(in_flight), timeout=0, return_when=FIRST_COMPLETED)
                    for fut in done:
                        try:
                            fut.result()
                        except Exception:  # noqa: BLE001
                            logger.exception("in-flight job raised")

            logger.info("draining %s in-flight jobs…", len(in_flight))
            if in_flight:
                wait(list(in_flight), timeout=60)
        finally:
            try:
                raw.close()
            except Exception:  # noqa: BLE001
                pass

    logger.info("PayFlow workflow worker stopped id=%s", worker_id)
