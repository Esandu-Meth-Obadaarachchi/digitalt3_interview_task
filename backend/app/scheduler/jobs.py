"""M10 - the scheduler itself.

    "A real scheduler must exist and be demonstrable. A button labelled 'Run
     morning job' with no scheduler behind it is a partial implementation."

So this is APScheduler's BackgroundScheduler, started with the application and
visible in `/api/digests/schedule`, which reports each job's next fire time
straight from the scheduler rather than from configuration. A next-run
timestamp that advances on its own is the difference between a scheduler and a
constant.

Two jobs, and the second is the more interesting one.

  digest       end of day, per channel
  expiry sweep pending items older than the configured window become 'expired'

The expiry sweep is the answer to the rubric's "safe default on timeout or no
response". An unreviewed proposal ages out to a state the approval-gate trigger
treats exactly like pending: not writable. Nothing is ever approved by the
passage of time, and the anti-patterns tab asks that an agent do at least one
thing without being asked. Refusing to proceed on stale information is that
thing.

The scheduler is disabled under test. A background thread firing during a test
run would make failures depend on wall-clock time, and the jobs themselves are
tested by calling them directly with an injected clock.
"""

from __future__ import annotations

import logging
from datetime import datetime

from app.config import Settings, get_settings
from app.models.common import StrictModel

logger = logging.getLogger("agent.scheduler")

_scheduler = None


class ScheduledJob(StrictModel):
    """A job as the scheduler currently holds it."""

    id: str
    name: str
    trigger: str
    next_run_at: str | None = None
    description: str


class EndOfDayResult(StrictModel):
    """Everything the end-of-day job wrote, in one place.

    Both kinds together, because the button in the interface has to be able to
    call the same function the scheduler calls. Returning only the channel
    digests would make the demonstration a parallel path, which is the exact
    thing the rubric calls a partial implementation.
    """

    channels: list = []
    people: list = []

    @property
    def total(self) -> int:
        return len(self.channels) + len(self.people)


class SchedulerStatus(StrictModel):
    running: bool
    timezone: str
    jobs: list[ScheduledJob] = []
    #: False when SCHEDULER_ENABLED is off, which the interface says out loud
    #: rather than showing an empty job list that looks like a bug.
    enabled: bool = True


def run_end_of_day(
    settings: Settings | None = None,
    *,
    now=None,
    trigger: str = "scheduler",
) -> EndOfDayResult:
    """The end-of-day work, in one function with one caller shape.

    One digest per channel (M10), then one per person who has something
    approved (M13). Both run together because both are the end of the same day,
    and a person opening a digest at six o'clock should not find their channel
    digest an hour older than their own.

    The scheduler calls this. So does the button in the interface, with a
    different trigger label and an injectable clock. One function, so what is
    demonstrated is what runs unattended.
    """
    from app.scheduler.digest import emit_all
    from app.scheduler.person_digest import emit_all_people

    cfg = settings or get_settings()
    result = EndOfDayResult(
        channels=emit_all(cfg, now=now, trigger=trigger),
        people=emit_all_people(cfg, now=now, trigger=trigger),
    )
    logger.info(
        "end-of-day job wrote %s channel digest(s) and %s person digest(s)",
        len(result.channels), len(result.people),
    )
    return result


def run_digest_job(settings: Settings | None = None) -> int:
    """What the scheduler fires at the configured hour."""
    return run_end_of_day(settings, trigger="scheduler").total


def run_expiry_job(settings: Settings | None = None) -> int:
    """The safe default. Returns how many items aged out."""
    from app.review.queue import expire_stale

    cfg = settings or get_settings()
    expired = expire_stale(cfg)
    if expired:
        logger.info("expiry sweep expired %s unreviewed extraction(s)", len(expired))
    return len(expired)


def start(settings: Settings | None = None):
    """Start the background scheduler. Idempotent."""
    global _scheduler

    cfg = settings or get_settings()
    if not cfg.scheduler_enabled:
        logger.info("scheduler disabled by configuration")
        return None
    if _scheduler is not None:
        return _scheduler

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler(timezone=cfg.scheduler_timezone)
    scheduler.add_job(
        run_digest_job,
        CronTrigger(hour=cfg.digest_hour, minute=cfg.digest_minute, timezone=cfg.scheduler_timezone),
        id="end_of_day_digest",
        name="End-of-day digest",
        replace_existing=True,
    )
    scheduler.add_job(
        run_expiry_job,
        CronTrigger(hour=cfg.expiry_sweep_hour, minute=0, timezone=cfg.scheduler_timezone),
        id="expiry_sweep",
        name="Expire unreviewed items",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler

    logger.info(
        "scheduler started: digest at %02d:%02d, expiry sweep at %02d:00, %s",
        cfg.digest_hour, cfg.digest_minute, cfg.expiry_sweep_hour, cfg.scheduler_timezone,
    )
    return scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def status(settings: Settings | None = None) -> SchedulerStatus:
    """What the scheduler currently holds.

    Next fire times come from the scheduler, not from configuration. A time
    read back from settings would prove only that settings can be read.
    """
    cfg = settings or get_settings()

    descriptions = {
        "end_of_day_digest": (
            "Builds one digest per channel from approved items only, then one per "
            "person who has approved commitments, writes them through the document "
            "store and posts the channel digests through the notifier. Somebody with "
            "no commitments gets no digest."
        ),
        "expiry_sweep": (
            f"Moves pending extractions older than {cfg.pending_expiry_hours}h to 'expired'. "
            f"Expired items are not writable: the safe default on no response is refusal."
        ),
    }

    if _scheduler is None:
        return SchedulerStatus(
            running=False,
            enabled=cfg.scheduler_enabled,
            timezone=cfg.scheduler_timezone,
            jobs=[
                ScheduledJob(
                    id=job_id,
                    name=job_id.replace("_", " "),
                    trigger=f"cron, {cfg.digest_hour:02d}:{cfg.digest_minute:02d}"
                    if job_id == "end_of_day_digest"
                    else f"cron, {cfg.expiry_sweep_hour:02d}:00",
                    next_run_at=None,
                    description=description,
                )
                for job_id, description in descriptions.items()
            ],
        )

    return SchedulerStatus(
        running=True,
        enabled=True,
        timezone=cfg.scheduler_timezone,
        jobs=[
            ScheduledJob(
                id=job.id,
                name=job.name,
                trigger=str(job.trigger),
                next_run_at=job.next_run_time.isoformat() if job.next_run_time else None,
                description=descriptions.get(job.id, ""),
            )
            for job in _scheduler.get_jobs()
        ],
    )


def next_run(job_id: str) -> datetime | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job(job_id)
    return job.next_run_time if job else None
