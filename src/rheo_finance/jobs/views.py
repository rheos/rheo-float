"""Only fixed local job details and success timestamps reach the reader."""

from sqlalchemy import Select, func, select

from rheo_finance.storage.schema import job_run


def report_job_select() -> Select[tuple[object, ...]]:
    latest = (
        select(job_run.c.job, job_run.c.detail_code)
        .distinct(job_run.c.job)
        .order_by(job_run.c.job, job_run.c.finished_at.desc(), job_run.c.seq.desc())
        .subquery("latest")
    )
    success = (
        select(func.max(job_run.c.finished_at))
        .where(job_run.c.job == latest.c.job, job_run.c.ok.is_(True))
        .correlate(latest)
        .scalar_subquery()
    )
    return select(
        latest.c.job,
        success.label("last_success"),
        latest.c.detail_code.label("last_detail"),
    )
