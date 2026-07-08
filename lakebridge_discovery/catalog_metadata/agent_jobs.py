"""
SQL Server Agent job inventory discovery, from SQL Server catalog metadata
only (msdb.dbo.sysjobs / sysjobsteps / sysschedules / sysjobschedules /
sysjobhistory / syscategories) -- no SQL parsing, no dependence on the
Analyzer report (it has no SQL-Server-Agent-specific inventory category at
all: sysjobs' step commands are server automation, not exported SQL/DDL
text this engine ever stages for it).

msdb is a fixed system database that always exists alongside the source
database on the same SQL Server instance -- this probe queries it directly
over the same shared connection every other probe in this package uses
(connection.py connects to config.source.database, but msdb.dbo.* is
reachable cross-database from any database on the same instance as long as
the login has msdb read access), so no separate connection/config knob is
needed.

Pure object-inventory discovery -- appends to result.agent_jobs, never
result.dependencies. Retyped independently of autovista's own
AgentJobEntity/AgentJobStepEntity (see schema.py), not shared code.
"""
from __future__ import annotations

from lakebridge_discovery.schema import AgentJobEntity, AgentJobStepEntity, LakebridgeDiscoveryResult

NAME = "agent_jobs"

_QUERY_JOBS = """
SELECT j.job_id, j.name, j.enabled, j.date_created, j.date_modified,
       j.description, SUSER_SNAME(j.owner_sid) AS owner_name, c.name AS category_name
FROM msdb.dbo.sysjobs j
LEFT JOIN msdb.dbo.syscategories c ON c.category_id = j.category_id
ORDER BY j.name
"""

_QUERY_STEPS = """
SELECT s.job_id, s.step_id, s.step_name, s.subsystem, s.database_name, s.command,
       s.on_success_action, s.on_fail_action, s.retry_attempts, s.retry_interval
FROM msdb.dbo.sysjobsteps s
ORDER BY s.job_id, s.step_id
"""

_QUERY_SCHEDULES = """
SELECT js.job_id, sc.name AS schedule_name
FROM msdb.dbo.sysjobschedules js
JOIN msdb.dbo.sysschedules sc ON sc.schedule_id = js.schedule_id
"""

# Same instance_id-is-monotonic reasoning autovista.sql_metadata_extractor's
# QUERY_AGENT_JOB_LAST_RUN documents: MAX(instance_id) per job reliably
# identifies its most recent run; step_id = 0 is the job-level outcome row.
_QUERY_LAST_RUN = """
SELECT h.job_id, h.run_date, h.run_time, h.run_status
FROM msdb.dbo.sysjobhistory h
WHERE h.step_id = 0
  AND h.instance_id = (
      SELECT MAX(h2.instance_id) FROM msdb.dbo.sysjobhistory h2
      WHERE h2.job_id = h.job_id AND h2.step_id = 0
  )
"""

_RUN_STATUS = {0: "Failed", 1: "Succeeded", 2: "Retry", 3: "Canceled", 4: "In Progress"}


def _decode_int_date(value) -> str | None:
    if not value:
        return None
    text = str(int(value))
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"


def _decode_int_time(value) -> str | None:
    if value is None:
        return None
    text = str(int(value)).zfill(6)
    return f"{text[0:2]}:{text[2:4]}:{text[4:6]}"


def discover(connection, result: LakebridgeDiscoveryResult, seen_edges: set[tuple]) -> None:
    cursor = connection.cursor()
    cursor.execute(_QUERY_JOBS)
    job_rows = cursor.fetchall()
    if not job_rows:
        return

    cursor = connection.cursor()
    cursor.execute(_QUERY_STEPS)
    steps_by_job: dict = {}
    for job_id, step_id, step_name, subsystem, database_name, command, on_success, on_fail, retry_attempts, retry_interval in cursor.fetchall():
        steps_by_job.setdefault(job_id, []).append(AgentJobStepEntity(
            step_id=step_id, name=step_name, subsystem=subsystem, database_name=database_name,
            command=command, on_success_action=on_success, on_fail_action=on_fail,
            retry_attempts=retry_attempts, retry_interval=retry_interval,
        ))

    cursor = connection.cursor()
    cursor.execute(_QUERY_SCHEDULES)
    schedules_by_job: dict = {}
    for job_id, schedule_name in cursor.fetchall():
        schedules_by_job.setdefault(job_id, []).append(schedule_name)

    cursor = connection.cursor()
    cursor.execute(_QUERY_LAST_RUN)
    last_run_by_job: dict = {}
    for job_id, run_date, run_time, run_status in cursor.fetchall():
        last_run_by_job[job_id] = (run_date, run_time, run_status)

    for job_id, name, enabled, date_created, date_modified, description, owner_name, category_name in job_rows:
        last_run = last_run_by_job.get(job_id)
        steps = steps_by_job.get(job_id, [])
        result.agent_jobs.append(AgentJobEntity(
            name=name,
            enabled=bool(enabled),
            owner=owner_name,
            category=category_name,
            description=description,
            date_created=str(date_created) if date_created is not None else None,
            date_modified=str(date_modified) if date_modified is not None else None,
            last_run_date=_decode_int_date(last_run[0]) if last_run else None,
            last_run_time=_decode_int_time(last_run[1]) if last_run else None,
            last_run_status=_RUN_STATUS.get(last_run[2]) if last_run else None,
            step_count=len(steps),
            schedule_names=schedules_by_job.get(job_id, []),
            steps=steps,
        ))
