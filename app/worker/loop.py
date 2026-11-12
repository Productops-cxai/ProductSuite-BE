"""Main worker loop: LISTEN/NOTIFY + periodic due sweep + thread pool."""

from __future__ import annotations

import logging
import os
import select
import signal
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone

from app.core.config import settings
from app.infrastructure.database.session import SessionLocal, engine
from app.worker.claim import claim_due_jobs, reclaim_stale_running_jobs
from app.worker.handlers import process_job

logger = logging.getLogger("app.worker")

_stop = False

# Seconds to wait before re-opening the LISTEN connection after a database error.
_RECONNECT_SECONDS = 5


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


def claim_budget(*, batch_size: int, concurrency: int, in_flight: int) -> int:
    """How many jobs to claim now.

    A claimed job is locked from that moment, so claiming far more than the pool
    can start lets queued jobs age past the stale window and get run twice.
    Keep at most one extra round of work queued behind the running threads.
    """
    room = concurrency * 2 - in_flight
    return max(0, min(batch_size, room))


def _drain_due(
    worker_id: str,
    pool: ThreadPoolExecutor,
    in_flight: set,
    *,
    stale_seconds: int,
    concurrency: int,
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
        budget = claim_budget(
            batch_size=int(settings.WORKFLOW_WORKER_BATCH_SIZE or 50),
            concurrency=concurrency,
            in_flight=len(in_flight),
        )
        job_ids = (
            claim_due_jobs(db, worker_id=worker_id, batch_size=budget) if budget > 0 else []
        )
    finally:
        db.close()

    for job_id in job_ids:
        fut = pool.submit(process_job, job_id, worker_id)
        in_flight.add(fut)
        fut.add_done_callback(in_flight.discard)


def _scan_email_performance() -> None:
    """One pass of the automatic email-performance check (its own thread and database session)."""
    try:
        from app.modules.payflow.services.email_subject_ai import PayflowEmailSubjectAiService

        db = SessionLocal()
        try:
            result = PayflowEmailSubjectAiService(db).scan_low_performers()
            if result.get("proposals"):
                logger.info("email performance check %s", result)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("email performance check failed")


def run_maintenance(state: dict) -> None:
    """Periodic housekeeping that no event triggers. Each part fails on its own."""
    # Payment plans: mark overdue instalments, auto-charge the ones that are due.
    try:
        from app.modules.payflow.services.payment_outcome_service import (
            PayflowPaymentOutcomeService,
        )
        from app.modules.payflow.services.plan_auto_charge_service import (
            PayflowPlanAutoChargeService,
        )

        db = SessionLocal()
        try:
            PayflowPaymentOutcomeService(db).refresh_overdue_plans(None)
            db.commit()
            result = PayflowPlanAutoChargeService(db).charge_due_installments()
            if result.get("charged") or result.get("failed"):
                logger.info("plan auto-charge sweep %s", result)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("overdue / auto-charge payment plan sweep failed")

    # Runs parked by a hold: resume the ones whose hold has lifted.
    try:
        from app.modules.payflow.services.workflow_execution_service import (
            WorkflowExecutionService,
        )

        db = SessionLocal()
        try:
            result = WorkflowExecutionService(db).resume_released_holds()
            if result.get("resumed"):
                logger.info("hold release sweep %s", result)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("hold release sweep failed")

    # Human reviews held until a date: release the ones that are due.
    try:
        from app.modules.payflow.services.review_service import PayflowReviewService

        db = SessionLocal()
        try:
            released = PayflowReviewService(db).release_due_holds()
            if released:
                logger.info("released %s review hold(s) that reached their date", released)
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("review hold release sweep failed")

    # Import / AI-suggest jobs left "running" by an API restart.
    try:
        from app.modules.payflow.services.background_job_recovery import (
            recover_interrupted_jobs,
        )

        db = SessionLocal()
        try:
            result = recover_interrupted_jobs(db)
            if any(result.values()):
                logger.warning("recovered interrupted background jobs %s", result)
        finally:
            db.close()
        pending_ai = list(result.get("ai_jobs_to_run") or [])
        runner = state.get("ai_runner")
        if pending_ai and not (runner is not None and runner.is_alive()):
            from app.modules.payflow.services.ai_suggest_job_service import (
                process_ai_suggest_jobs,
            )

            # Minutes of LLM calls: never on the loop that schedules customer messages.
            runner = threading.Thread(
                target=process_ai_suggest_jobs,
                args=(pending_ai,),
                name="ai-suggest-recovery",
                daemon=True,
            )
            state["ai_runner"] = runner
            runner.start()
    except Exception:  # noqa: BLE001
        logger.exception("background job recovery sweep failed")

    # Email performance: under-performing emails of Active workflows get AI proposals for review.
    scan_seconds = int(settings.EMAIL_OPTIMIZATION_SCAN_HOURS or 0) * 3600
    scanner = state.get("email_scan_runner")
    if (
        scan_seconds > 0
        and time.monotonic() - state.get("last_email_scan", 0.0) >= scan_seconds
        and not (scanner is not None and scanner.is_alive())
    ):
        state["last_email_scan"] = time.monotonic()
        # AI calls can take minutes: never on the loop that schedules customer messages.
        scanner = threading.Thread(target=_scan_email_performance, name="email-performance-scan", daemon=True)
        state["email_scan_runner"] = scanner
        scanner.start()

    # Gap fill: accounts that fit an Active workflow but were never enrolled
    # (enroll job lost, account aged into the segment, activate hiccup).
    gap_seconds = int(settings.WORKFLOW_GAP_FILL_SECONDS or 0)
    now = time.monotonic()
    if gap_seconds > 0 and now - state.get("last_gap_fill", 0.0) >= gap_seconds:
        state["last_gap_fill"] = now
        try:
            from app.modules.payflow.services.workflow_queue import enqueue_gap_fill_once

            db = SessionLocal()
            try:
                enqueue_gap_fill_once(db)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.exception("gap fill enqueue failed")


def _listen_loop(
    worker_id: str,
    pool: ThreadPoolExecutor,
    in_flight: set,
    state: dict,
    *,
    channel: str,
    sweep_seconds: int,
    stale_seconds: int,
    concurrency: int,
) -> None:
    """One LISTEN connection's lifetime. Raises on a database error; the caller reconnects."""
    # Dedicated connection for LISTEN (psycopg2).
    raw = engine.raw_connection()
    try:
        raw.set_isolation_level(0)  # AUTOCOMMIT required for LISTEN
        with raw.cursor() as cur:
            cur.execute(f"LISTEN {channel}")
        logger.info("LISTEN %s armed", channel)

        # Catch up on anything that became due while we were not listening.
        _drain_due(worker_id, pool, in_flight, stale_seconds=stale_seconds, concurrency=concurrency)
        last_sweep = time.monotonic()

        while not _stop:
            now = time.monotonic()
            if now - last_sweep >= sweep_seconds:
                _drain_due(
                    worker_id, pool, in_flight, stale_seconds=stale_seconds, concurrency=concurrency
                )
                run_maintenance(state)
                last_sweep = now

            # Wait for notify or timeout (sweep cadence).
            try:
                ready, _, _ = select.select([raw], [], [], min(5.0, float(sweep_seconds)))
            except (ValueError, OSError):
                # Interrupted by a signal: stop, or let the caller reconnect.
                if _stop:
                    return
                raise

            if ready:
                raw.poll()
                notified = bool(raw.notifies)
                while raw.notifies:
                    raw.notifies.pop(0)
                if notified:
                    _drain_due(
                        worker_id,
                        pool,
                        in_flight,
                        stale_seconds=stale_seconds,
                        concurrency=concurrency,
                    )
            elif in_flight and len(in_flight) < concurrency:
                # Pool has room again: pull the next jobs without waiting for the sweep.
                _drain_due(
                    worker_id, pool, in_flight, stale_seconds=stale_seconds, concurrency=concurrency
                )

            # Reap completed futures without blocking the loop hard.
            if in_flight:
                done, _ = wait(list(in_flight), timeout=0, return_when=FIRST_COMPLETED)
                for fut in done:
                    try:
                        fut.result()
                    except Exception:  # noqa: BLE001
                        logger.exception("in-flight job raised")
    finally:
        try:
            raw.close()
        except Exception:  # noqa: BLE001
            pass


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
    state: dict = {}

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="wf") as pool:
        # A database restart or network blip must not kill the worker: drop the
        # LISTEN connection, wait, and connect again.
        while not _stop:
            try:
                _listen_loop(
                    worker_id,
                    pool,
                    in_flight,
                    state,
                    channel=channel,
                    sweep_seconds=sweep_seconds,
                    stale_seconds=stale_seconds,
                    concurrency=concurrency,
                )
            except Exception:  # noqa: BLE001
                if _stop:
                    break
                logger.exception(
                    "worker loop lost the database — reconnecting in %ss", _RECONNECT_SECONDS
                )
                time.sleep(_RECONNECT_SECONDS)

        logger.info("draining %s in-flight jobs…", len(in_flight))
        if in_flight:
            wait(list(in_flight), timeout=60)

    logger.info("PayFlow workflow worker stopped id=%s", worker_id)
