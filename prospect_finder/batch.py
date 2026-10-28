"""Bulk website checks that survive refreshes, restarts and crashes.

A batch ("job") is a list of prospects stored in the database. Worker threads
claim one queued item at a time with a single atomic UPDATE, run the existing
``scan_business`` check (the same one behind the "Check website" button), and
save the item's outcome straight away. Because every state change is written
to SQLite, closing the browser changes nothing, and if the server stops mid-run
the job is detected as interrupted (its heartbeat goes quiet) and can be resumed
without repeating finished items.

No external queue or paid service is used: the runner is a few threads in the
app process. Google Places lookups are never made from a batch.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from .analyzer.fetcher import HostThrottle
from .checks import REASON_LABELS, CheckRules, eligibility, priority_key
from .config import Config
from .models import format_ts, parse_ts, utcnow
from .repository import Repository, connect
from .scanner import make_fetcher, scan_business

log = logging.getLogger(__name__)

ACTIVE = ("running", "stopping")
HEARTBEAT_SECONDS = 5
HEARTBEAT_STALE_SECONDS = 30
MAX_CONCURRENCY = 5
MAX_BATCH_SIZE = 500

# Threads running in this process, by job id.
_threads: dict[int, threading.Thread] = {}
_threads_lock = threading.Lock()


class BatchBusy(Exception):
    def __init__(self, job_id: int | None):
        super().__init__(f"website check batch #{job_id} is already running")
        self.job_id = job_id


class NothingToCheck(Exception):
    pass


@dataclass(frozen=True)
class BatchSettings:
    batch_size: int = 10
    concurrency: int = 3
    rules: CheckRules = CheckRules()

    @classmethod
    def from_config(cls, config: Config, batch_size: int | None = None) -> "BatchSettings":
        size = batch_size or config.check_batch_size
        return cls(
            batch_size=max(1, min(int(size), MAX_BATCH_SIZE)),
            concurrency=max(1, min(config.check_concurrency, MAX_CONCURRENCY)),
            rules=CheckRules(failed_retry_hours=config.check_failed_retry_hours,
                             max_failed_attempts=config.check_max_failed_attempts),
        )


# ---------------------------------------------------------------- planning


def _context(repo: Repository):
    outreach = repo.all_outreach()
    checks = repo.all_website_checks()
    return outreach, checks


def eligible_ids(repo: Repository, now: datetime, rules: CheckRules) -> tuple[list[int], Counter]:
    """Prospects a "Check unevaluated websites" run would pick, in priority order, plus why others were left out."""
    outreach, checks = _context(repo)
    picked, left_out = [], Counter()
    for b in repo.list_businesses():
        e = eligibility(b, checks.get(b.id), bool(outreach.get(b.id, {}).get("opted_out")), now, rules=rules)
        if e.eligible:
            picked.append(b)
        elif e.reason != "fresh":
            left_out[e.reason] += 1
    picked.sort(key=lambda b: priority_key(checks.get(b.id), now, rules))
    return [b.id for b in picked], left_out


def create_job(repo: Repository, settings: BatchSettings, *, mode: str = "unevaluated",
               selected_ids: list[int] | None = None, force: bool = False, now: datetime | None = None) -> int:
    """Store a new batch and its items. Raises BatchBusy if another batch is running."""
    now = now or utcnow()
    expire_stale_jobs(repo, now)
    active = active_job(repo)
    if active:
        raise BatchBusy(active["id"])
    queued: list[int] = []
    skipped: list[tuple[int, str, str]] = []
    extra: dict = {}
    if mode == "unevaluated":
        ids, left_out = eligible_ids(repo, now, settings.rules)
        queued = ids[: settings.batch_size]
        extra["left_out"] = dict(left_out)
    elif mode == "selected":
        outreach, checks = _context(repo)
        seen = set()
        for bid in selected_ids or []:
            if bid in seen:
                continue  # the same prospect selected twice is checked once
            seen.add(bid)
            b = repo.get_business(bid)
            if b is None:
                continue
            e = eligibility(b, checks.get(bid), bool(outreach.get(bid, {}).get("opted_out")), now,
                            selected=True, force=force, rules=settings.rules)
            if not e.eligible:
                skipped.append((bid, e.reason, e.detail))
            elif len(queued) < settings.batch_size:
                queued.append(bid)
            else:
                extra.setdefault("overflow_ids", []).append(bid)
    else:
        raise ValueError(f"unknown batch mode {mode}")
    if not queued and not skipped:
        raise NothingToCheck("No prospects need a website check right now.")

    try:
        cur = repo.conn.execute(
            "INSERT INTO check_jobs (mode, status, batch_size, force, created_at, heartbeat_at, extra) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (mode, "running" if queued else "completed", settings.batch_size, int(force), format_ts(now),
             format_ts(now), json.dumps(extra)))
    except sqlite3.IntegrityError:
        repo.conn.rollback()
        raise BatchBusy(active_job(repo)["id"] if active_job(repo) else None) from None
    job_id = cur.lastrowid
    repo.conn.executemany(
        "INSERT INTO check_job_items (job_id, business_id, state) VALUES (?, ?, 'queued')",
        [(job_id, bid) for bid in queued])
    repo.conn.executemany(
        "INSERT INTO check_job_items (job_id, business_id, state, result, detail, finished_at) "
        "VALUES (?, ?, 'skipped', ?, ?, ?)",
        [(job_id, bid, reason, detail, format_ts(now)) for bid, reason, detail in skipped])
    if not queued:
        _finish_job(repo, job_id, now, settings.rules)
    repo.conn.commit()
    return job_id


# ---------------------------------------------------------------- job state


def get_job(repo: Repository, job_id: int) -> dict | None:
    row = repo.conn.execute("SELECT * FROM check_jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    job = dict(row)
    job["extra"] = json.loads(job["extra"] or "{}")
    return job


def active_job(repo: Repository) -> dict | None:
    row = repo.conn.execute("SELECT id FROM check_jobs WHERE status IN ('running', 'stopping') "
                            "ORDER BY id DESC LIMIT 1").fetchone()
    return get_job(repo, row["id"]) if row else None


def latest_job(repo: Repository) -> dict | None:
    row = repo.conn.execute("SELECT id FROM check_jobs ORDER BY id DESC LIMIT 1").fetchone()
    return get_job(repo, row["id"]) if row else None


def _thread_alive(job_id: int) -> bool:
    with _threads_lock:
        t = _threads.get(job_id)
        return bool(t and t.is_alive())


def expire_stale_jobs(repo: Repository, now: datetime | None = None) -> list[int]:
    """Mark running jobs whose heartbeat stopped (server restarted or crashed) as interrupted.

    Items that were mid-check go back to the queue; finished items keep their results.
    """
    now = now or utcnow()
    cutoff = now - timedelta(seconds=HEARTBEAT_STALE_SECONDS)
    interrupted = []
    for row in repo.conn.execute("SELECT id, heartbeat_at FROM check_jobs WHERE status IN ('running', 'stopping')"):
        beat = parse_ts(row["heartbeat_at"])
        if not _thread_alive(row["id"]) and (beat is None or beat < cutoff):
            interrupted.append(row["id"])
    for job_id in interrupted:
        repo.conn.execute("UPDATE check_jobs SET status = 'interrupted' WHERE id = ?", (job_id,))
        repo.conn.execute("UPDATE check_job_items SET state = 'queued', started_at = NULL "
                          "WHERE job_id = ? AND state = 'processing'", (job_id,))
    repo.conn.commit()
    return interrupted


def resume_job(repo: Repository, job_id: int, now: datetime | None = None) -> None:
    """Put an interrupted or stopped job back to running (its queued items will be processed)."""
    now = now or utcnow()
    expire_stale_jobs(repo, now)
    job = get_job(repo, job_id)
    if not job or job["status"] not in ("interrupted", "stopped"):
        raise ValueError("Only interrupted or stopped batches can be resumed.")
    active = active_job(repo)
    if active:
        raise BatchBusy(active["id"])
    repo.conn.execute("UPDATE check_job_items SET state = 'queued', started_at = NULL "
                      "WHERE job_id = ? AND state = 'processing'", (job_id,))
    try:
        repo.conn.execute("UPDATE check_jobs SET status = 'running', heartbeat_at = ?, finished_at = NULL "
                          "WHERE id = ?", (format_ts(now), job_id))
    except sqlite3.IntegrityError:
        repo.conn.rollback()
        raise BatchBusy(None) from None
    repo.conn.commit()


def request_stop(repo: Repository, job_id: int) -> None:
    repo.conn.execute("UPDATE check_jobs SET status = 'stopping' WHERE id = ? AND status = 'running'", (job_id,))
    repo.conn.commit()


def claim_next(repo: Repository, job_id: int, now: datetime | None = None) -> tuple[int, int] | None:
    """Atomically move the oldest queued item to 'processing'. Two workers can never get the same item."""
    row = repo.conn.execute(
        """UPDATE check_job_items SET state = 'processing', started_at = ?, attempts = attempts + 1
            WHERE id = (SELECT id FROM check_job_items WHERE job_id = ? AND state = 'queued' ORDER BY id LIMIT 1)
              AND state = 'queued'
        RETURNING id, business_id""", (format_ts(now or utcnow()), job_id)).fetchone()
    repo.conn.commit()
    return (row["id"], row["business_id"]) if row else None


def _finish_item(repo: Repository, item_id: int, state: str, result: str, detail: str) -> None:
    repo.conn.execute("UPDATE check_job_items SET state = ?, result = ?, detail = ?, finished_at = ? WHERE id = ?",
                      (state, result, detail[:1000], format_ts(utcnow()), item_id))
    repo.conn.commit()


def _finish_job(repo: Repository, job_id: int, now: datetime | None = None,
                rules: CheckRules = CheckRules()) -> None:
    now = now or utcnow()
    job = get_job(repo, job_id)
    remaining = len(eligible_ids(repo, now, rules)[0])
    final = "stopped" if job["status"] == "stopping" else "completed"
    if repo.conn.execute("SELECT 1 FROM check_job_items WHERE job_id = ? AND state IN ('queued', 'processing')",
                         (job_id,)).fetchone() and final == "completed":
        final = "stopped"
    repo.conn.execute("UPDATE check_jobs SET status = ?, finished_at = ?, remaining_after = ? WHERE id = ?",
                      (final, format_ts(now), remaining, job_id))
    repo.conn.commit()


# ---------------------------------------------------------------- processing

SAVED_STATE = {"ok": "completed", "blocked": "completed", "failed": "failed", "no_website": "skipped"}


def process_item(repo: Repository, job: dict, item_id: int, business_id: int, fetcher, config: Config,
                 settings: BatchSettings) -> str:
    """Check one claimed prospect and save the outcome. Returns the item's final state."""
    now = utcnow()
    b = repo.get_business(business_id)
    if b is None:
        _finish_item(repo, item_id, "skipped", "deleted", "prospect was deleted")
        return "skipped"
    check = repo.website_check(business_id)
    if check and check["last_attempt_at"] and check["last_attempt_at"] >= parse_ts(job["created_at"]):
        # Already checked since this batch began: saved just before an interruption, or checked
        # individually in the meantime. Reuse that saved result instead of fetching the site again.
        state = SAVED_STATE[check["status"]]
        _finish_item(repo, item_id, state, check["status"], f"already checked during this batch: {check['detail']}")
        return state
    opted_out = bool(repo.get_outreach(business_id).get("opted_out"))
    e = eligibility(b, check, opted_out, now, selected=job["mode"] == "selected", force=bool(job["force"]),
                    rules=settings.rules)
    if not e.eligible:
        _finish_item(repo, item_id, "skipped", e.reason, e.detail)
        return "skipped"
    result = scan_business(repo, b, fetcher, config, google_client=None)
    if result.status in ("ok", "blocked"):
        state = "completed"
    elif result.status == "failed":
        state = "failed"
    else:
        state = "skipped"
    _finish_item(repo, item_id, state, result.status, result.detail)
    return state


def run_job(db_path: str, job_id: int, config: Config, settings: BatchSettings, *, google_cache_days: int = 0,
            fetcher_factory=None, on_item=None) -> dict:
    """Process every queued item of a job with up to ``settings.concurrency`` workers, then finish it."""
    fetcher_factory = fetcher_factory or (lambda throttle: make_fetcher(config, throttle))
    throttle = HostThrottle()
    stop_beat = threading.Event()

    def heartbeat():
        conn = connect(db_path)
        try:
            while not stop_beat.wait(HEARTBEAT_SECONDS):
                conn.execute("UPDATE check_jobs SET heartbeat_at = ? WHERE id = ?", (format_ts(utcnow()), job_id))
                conn.commit()
        finally:
            conn.close()

    def worker():
        conn = connect(db_path)
        repo = Repository(conn, google_cache_days)
        fetcher = fetcher_factory(throttle)
        try:
            while True:
                job = get_job(repo, job_id)
                if not job or job["status"] != "running":
                    break  # stop requested (or job removed): leave the rest queued
                claimed = claim_next(repo, job_id)
                if not claimed:
                    break
                item_id, business_id = claimed
                try:
                    state = process_item(repo, job, item_id, business_id, fetcher, config, settings)
                except Exception as exc:  # one bad site must not end the batch
                    log.exception("website check failed for prospect %s", business_id)
                    repo.conn.rollback()
                    _finish_item(repo, item_id, "failed", "error", f"internal error: {exc}")
                    state = "failed"
                if on_item:
                    on_item(business_id, state)
        finally:
            conn.close()

    beat = threading.Thread(target=heartbeat, daemon=True)
    beat.start()
    try:
        conn = connect(db_path)
        repo = Repository(conn, google_cache_days)
        queued = repo.conn.execute("SELECT COUNT(*) FROM check_job_items WHERE job_id = ? AND state = 'queued'",
                                   (job_id,)).fetchone()[0]
        workers = [threading.Thread(target=worker, daemon=True)
                   for _ in range(max(1, min(settings.concurrency, queued)))]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        _finish_job(repo, job_id, rules=settings.rules)
        progress = job_progress(repo, job_id)
        conn.close()
        return progress
    finally:
        stop_beat.set()


def start_job_thread(db_path: str, job_id: int, config: Config, settings: BatchSettings, *,
                     google_cache_days: int = 0, fetcher_factory=None) -> threading.Thread:
    """Run a job in the background of this process."""
    def target():
        try:
            run_job(db_path, job_id, config, settings, google_cache_days=google_cache_days,
                    fetcher_factory=fetcher_factory)
        except Exception:
            log.exception("website check batch %s stopped unexpectedly", job_id)

    t = threading.Thread(target=target, name=f"check-job-{job_id}", daemon=True)
    with _threads_lock:
        _threads[job_id] = t
    t.start()
    return t


def wait_for(job_id: int, timeout: float | None = None) -> None:
    with _threads_lock:
        t = _threads.get(job_id)
    if t:
        t.join(timeout)


# ---------------------------------------------------------------- progress


def job_progress(repo: Repository, job_id: int) -> dict | None:
    job = get_job(repo, job_id)
    if not job:
        return None
    rows = [dict(r) for r in repo.conn.execute(
        """SELECT i.*, b.name, b.website, b.google_place_id FROM check_job_items i
             JOIN businesses b ON b.id = i.business_id
            WHERE i.job_id = ? ORDER BY i.id""", (job_id,))]
    counts = Counter(r["state"] for r in rows)
    for r in rows:
        r["display_name"] = r["name"] or (f"Prospect #{r['business_id']}")
    left_out = job["extra"].get("left_out", {})
    return {
        "id": job["id"],
        "mode": job["mode"],
        "status": job["status"],
        "batch_size": job["batch_size"],
        "force": bool(job["force"]),
        "created_at": job["created_at"],
        "finished_at": job["finished_at"],
        "counts": {
            "total": len(rows),
            "queued": counts["queued"],
            "processing": counts["processing"],
            "completed": counts["completed"],
            "failed": counts["failed"],
            "skipped": counts["skipped"],
            "remaining": counts["queued"] + counts["processing"],
        },
        "processing": [r for r in rows if r["state"] == "processing"],
        "items": rows,
        "summary": {
            "evaluated": counts["completed"],
            "access_restricted": sum(1 for r in rows if r["result"] == "blocked"),
            "failed": counts["failed"],
            "skipped": counts["skipped"],
            "remaining_unevaluated": job["remaining_after"],
            "selected_not_included": len(job["extra"].get("overflow_ids", [])),
            "left_out": {REASON_LABELS.get(k, k): v for k, v in left_out.items()},
        },
        "overflow_ids": job["extra"].get("overflow_ids", []),
        "finished": job["status"] in ("completed", "stopped", "interrupted"),
    }


def wait_until_finished(repo: Repository, job_id: int, timeout: float = 30.0) -> dict:
    """Poll until a job leaves the running state (used by tests and the CLI)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = get_job(repo, job_id)
        if job and job["status"] not in ACTIVE:
            return job
        time.sleep(0.05)
    raise TimeoutError(f"job {job_id} still running")
