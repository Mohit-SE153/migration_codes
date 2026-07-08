"""
Tests for lakebridge_discovery.catalog_metadata.agent_jobs -- SQL Server
Agent job inventory discovery from msdb.dbo.sysjobs/sysjobsteps/
sysschedules/sysjobschedules/sysjobhistory only. Exercised against a stub
connection/cursor (no real SQL Server).
"""
from __future__ import annotations

from lakebridge_discovery import catalog_metadata
from lakebridge_discovery.catalog_metadata import agent_jobs
from lakebridge_discovery.schema import LakebridgeDiscoveryResult


class _FakeCursor:
    def __init__(self, jobs, steps, schedules, last_run):
        self._jobs = jobs
        self._steps = steps
        self._schedules = schedules
        self._last_run = last_run
        self._rows: list[tuple] = []

    def execute(self, sql: str):
        if "sysjobsteps" in sql and "sysjobschedules" not in sql:
            self._rows = self._steps
        elif "sysjobschedules" in sql and "sysjobhistory" not in sql:
            self._rows = self._schedules
        elif "sysjobhistory" in sql:
            self._rows = self._last_run
        else:
            self._rows = self._jobs
        return self

    def fetchall(self):
        return self._rows


class _FakeConnection:
    def __init__(self, jobs=(), steps=(), schedules=(), last_run=()):
        self._jobs = jobs
        self._steps = steps
        self._schedules = schedules
        self._last_run = last_run

    def cursor(self):
        return _FakeCursor(self._jobs, self._steps, self._schedules, self._last_run)


def test_discover_emits_one_job_with_step_count_and_schedule():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(
        jobs=[(1, "Nightly ETL", True, 20240101, 20240102, "Runs ETL", "sa", "Data Collector")],
        steps=[(1, 1, "Step1", "TSQL", "AdventureWorks2022", "EXEC dbo.usp_Load", "QuitWithSuccess", "QuitWithFailure", 0, 0)],
        schedules=[(1, "NightlySchedule")],
        last_run=[(1, 20240102, 10000, 1)],
    )

    agent_jobs.discover(connection, result, seen_edges=set())

    assert len(result.agent_jobs) == 1
    job = result.agent_jobs[0]
    assert job.name == "Nightly ETL"
    assert job.enabled is True
    assert job.owner == "sa"
    assert job.category == "Data Collector"
    assert job.step_count == 1
    assert job.schedule_names == ["NightlySchedule"]
    assert job.last_run_status == "Succeeded"
    assert job.last_run_date == "2024-01-02"
    assert job.last_run_time == "01:00:00"
    assert job.steps[0].name == "Step1"
    assert job.steps[0].subsystem == "TSQL"


def test_discover_handles_job_with_no_schedule_or_run_history():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(
        jobs=[(2, "Ad-hoc Job", False, None, None, None, None, None)],
        steps=[(2, 1, "Step1", "CmdExec", None, "dir", "QuitWithSuccess", "QuitWithFailure", 0, 0)],
    )

    agent_jobs.discover(connection, result, seen_edges=set())

    job = result.agent_jobs[0]
    assert job.enabled is False
    assert job.schedule_names == []
    assert job.last_run_status is None


def test_discover_no_jobs_is_a_noop():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(jobs=[])

    agent_jobs.discover(connection, result, seen_edges=set())

    assert result.agent_jobs == []


def test_discover_does_not_touch_dependencies():
    result = LakebridgeDiscoveryResult()
    connection = _FakeConnection(jobs=[(1, "Job", True, None, None, None, None, None)])

    agent_jobs.discover(connection, result, seen_edges=set())

    assert result.dependencies == []


def test_agent_jobs_probe_is_registered():
    names = [name for name, _ in catalog_metadata._REGISTRY]
    assert "agent_jobs" in names
