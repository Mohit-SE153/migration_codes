"""
Works around a Windows filesystem-minifilter quirk seen repeatedly on this
dev machine (see tools/dependency_validator/report.py's module docstring
and tests/test_dependency_coverage_validator.py for the same issue): under
real-time antivirus scanning contention, pathlib.Path.stat() can raise
FileNotFoundError for a directory/file that genuinely exists -- os.scandir()
of the parent directory sees it fine. pytest's own collection walks the
rootdir (for conftest.py discovery) using stat-based checks, so this one
false negative anywhere under the repo root is enough to abort collection
entirely, regardless of `testpaths`.

This retries exactly the way report.py's read side already does: only when
os.scandir() of the parent confirms the entry is actually there (so a
genuinely-missing path still fails immediately, no added latency).
"""
from __future__ import annotations

import os
import pathlib
import time

_orig_stat = pathlib.Path.stat


def _confirmed_by_scandir(path: pathlib.Path) -> bool:
    try:
        with os.scandir(path.parent) as entries:
            return any(entry.name == path.name for entry in entries)
    except OSError:
        return False


def _retrying_stat(self, *, follow_symlinks=True):
    for attempt in range(5):
        try:
            return _orig_stat(self, follow_symlinks=follow_symlinks)
        except FileNotFoundError:
            if not _confirmed_by_scandir(self):
                raise
            time.sleep(0.05)
    return _orig_stat(self, follow_symlinks=follow_symlinks)


pathlib.Path.stat = _retrying_stat
