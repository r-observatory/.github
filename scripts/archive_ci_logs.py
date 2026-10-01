#!/usr/bin/env python3
"""Keep a repository's GitHub Actions logs past GitHub's 90-day retention.

Each closed month becomes two assets on the repository's ci-logs prerelease:
ci-logs-YYYY-MM.tar (index.tsv plus one log zip per run attempt) and
ci-logs-YYYY-MM.tsv (the same index). Assets are added once and never replaced.

  archive    list, fetch and upload every month that is due (the workflow)
  build      fetch months into a work directory without touching the release
  upload     upload months that build left in a work directory
  keepalive  re-enable the calling workflow so its schedule never lapses
"""
import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile

TAG = "ci-logs"
RETENTION_DAYS = 90
CLOSE_GRACE_DAYS = 2
UPLOAD_ANYWAY_DAY = 80
SLICE_DAYS = 7
LIST_CAP = 1000
TRIES = 3
BACKOFF_S = (10, 30)
BUDGET_FLOOR = 300
BUDGET_EVERY = 25
COLUMNS = ["run_id", "run_attempt", "workflow_name", "workflow_path", "event",
           "conclusion", "head_branch", "head_sha", "created_at",
           "run_started_at", "updated_at", "zip_bytes", "zip_sha256", "outcome"]
RELEASE_NOTES = (
    "GitHub Actions logs of this repository, kept past GitHub's 90-day retention. "
    "Each closed month has two files: ci-logs-YYYY-MM.tar holds index.tsv and one log "
    "zip per run attempt, and ci-logs-YYYY-MM.tsv is the same index. The index gives "
    "each attempt's workflow, event, conclusion, commit, times, zip size and sha256, "
    "and whether its log was saved (ok), already deleted by GitHub (gone) or failed "
    "to download (error). Files are added once and never replaced. This is a "
    "prerelease so that it never becomes the repository's latest release.")
LOCAL_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}_(\d+)_.*\.zip$")
MEMBER_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}_(\d+)_a(\d+)_.*\.zip$")


class UsageError(Exception):
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def sleep(seconds):
    time.sleep(seconds)


def parse_time(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- months -----------------------------------------------------------------

def month_bounds(month):
    m = re.fullmatch(r"(\d{4})-(\d{2})", month or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise UsageError(f"not a month: {month!r} (want YYYY-MM)")
    y, mo = int(m.group(1)), int(m.group(2))
    start = dt.datetime(y, mo, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=dt.timezone.utc)
    return start, end


def is_closed(month, now):
    """Two days after its last day, when no run created in it can still be going."""
    return month_bounds(month)[1] + dt.timedelta(days=CLOSE_GRACE_DAYS) <= now


def months_due(now):
    """Closed months whose first day is still inside retention, oldest first."""
    out, y, m = [], now.year, now.month
    for _ in range(4):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        month = f"{y:04d}-{m:02d}"
        if is_closed(month, now) and month_bounds(month)[0] + dt.timedelta(days=RETENTION_DAYS) > now:
            out.append(month)
    return sorted(out)


def check_months(months, now):
    """The requested months, oldest first; each must be closed."""
    for month in months:
        if not is_closed(month, now):
            raise UsageError(f"{month} is not closed yet; a month is archived from two days after it ends")
    return sorted(set(months))


def upload_anyway(month, now):
    return now >= month_bounds(month)[0] + dt.timedelta(days=UPLOAD_ANYWAY_DAY)


def day_slices(month, days=SLICE_DAYS):
    start, end = month_bounds(month)
    a, last, out = start.date(), (end - dt.timedelta(days=1)).date(), []
    while a <= last:
        b = min(a + dt.timedelta(days=days - 1), last)
        out.append((a, b))
        a = b + dt.timedelta(days=1)
    return out
