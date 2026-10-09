"""
Runs jobs in the background.

Two things can start a job: the admin panel (in-process thread) and the
`run_due_jobs` management command (Windows Task Scheduler). Both go through
start_job(), which refuses to start a second copy of a job that is already
running — so the two schedulers can safely coexist.
"""
import threading
import traceback

from django.db import close_old_connections
from django.utils import timezone

from .jobs import HANDLERS
from .models import JobRun, Schedule

_lock = threading.Lock()


def active_run(job=None):
    """The job that is currently running, ignoring rows left behind by a crash."""
    qs = JobRun.objects.filter(status='running')
    if job:
        qs = qs.filter(job=job)
    for run in qs:
        if run.is_stale:
            run.status      = 'failed'
            run.error       = 'Abgebrochen — Server wurde beendet.'
            run.finished_at = timezone.now()
            run.save(update_fields=['status', 'error', 'finished_at'])
            continue
        return run
    return None


def start_job(job, params=None, triggered_by='admin'):
    """
    Queue a job and run it in a background thread.

    Returns (JobRun, error_message). Only one job runs at a time — the AI
    provider is the bottleneck, so running two at once just causes rate limits.
    """
    if job not in HANDLERS:
        return None, f'Unbekannter Job: {job}'

    with _lock:
        busy = active_run()
        if busy:
            return None, f'„{busy.get_job_display()}" läuft gerade. Bitte warten.'
        run = JobRun.objects.create(
            job=job, params=params or {}, triggered_by=triggered_by,
            status='running', started_at=timezone.now(), heartbeat=timezone.now(),
        )

    thread = threading.Thread(target=_execute, args=(run.pk,), daemon=True)
    thread.start()
    return run, None


def _execute(run_pk):
    close_old_connections()
    run = JobRun.objects.get(pk=run_pk)
    try:
        HANDLERS[run.job](run, run.params or {})
        cancelled = JobRun.objects.filter(pk=run_pk, cancel_requested=True).exists()
        JobRun.objects.filter(pk=run_pk).update(
            status='cancelled' if cancelled else 'done',
            finished_at=timezone.now(), heartbeat=timezone.now(), current='',
        )
    except Exception as e:
        JobRun.objects.filter(pk=run_pk).update(
            status='failed', error=f'{e}\n\n{traceback.format_exc()[-2000:]}',
            finished_at=timezone.now(), heartbeat=timezone.now(), current='',
        )
    finally:
        close_old_connections()


def run_due_schedules(triggered_by='schedule'):
    """
    Start whatever is due right now. Safe to call repeatedly and from two
    places at once: last_run_on is claimed before the job starts, so a schedule
    can only fire once per day no matter who calls this.
    """
    started = []
    today   = timezone.localtime().date()

    for schedule in Schedule.objects.filter(enabled=True):
        if not schedule.is_due():
            continue

        # Claim the slot first. The UPDATE ... WHERE last_run_on IS DISTINCT
        # only succeeds for one caller, so the other one sees 0 rows and skips.
        claimed = Schedule.objects.filter(pk=schedule.pk).exclude(
            last_run_on=today
        ).update(last_run_on=today)
        if not claimed:
            continue

        params = {'count': schedule.count, 'pages': max(1, schedule.count // 10)}
        run, err = start_job(schedule.job, params, triggered_by=triggered_by)
        if run:
            started.append(schedule.job)
        else:
            # Could not start (something else is running) — let it try again later.
            Schedule.objects.filter(pk=schedule.pk).update(last_run_on=None)
    return started


# --------------------------------------------------------------------------
# In-process scheduler: a quiet tick while the Django server is running.
# --------------------------------------------------------------------------

_ticker_started = False
TICK_SECONDS = 60


def _tick_forever():
    import time
    while True:
        time.sleep(TICK_SECONDS)
        try:
            close_old_connections()
            run_due_schedules(triggered_by='server')
        except Exception:
            pass


def start_ticker():
    global _ticker_started
    if _ticker_started:
        return
    _ticker_started = True
    threading.Thread(target=_tick_forever, daemon=True).start()
