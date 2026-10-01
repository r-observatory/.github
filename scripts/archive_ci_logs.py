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


# --- gh, the API budget and retries ----------------------------------------

class GhError(Exception):
    def __init__(self, args, status, text):
        super().__init__(f"gh {' '.join(args)}: {text.strip()[:300]}")
        self.status = status
        self.text = text


def http_status(text):
    m = re.search(r"\(HTTP (\d{3})\)", text)
    return int(m.group(1)) if m else None


def gh(args, out=None):
    """Run gh and return its stdout, or write stdout to the path `out`."""
    if out is None:
        p = subprocess.run(["gh"] + args, capture_output=True)
    else:
        with open(out, "wb") as f:
            p = subprocess.run(["gh"] + args, stdout=f, stderr=subprocess.PIPE)
    if p.returncode != 0:
        text = p.stderr.decode("utf-8", "replace")
        raise GhError(args, http_status(text), text)
    return p.stdout or b""


def gh_json(args):
    return json.loads(gh(args) or b"null")


class Budget:
    """Leaves a reserve of the hourly API budget to the repository's own workflows."""

    def __init__(self, floor=BUDGET_FLOOR, every=BUDGET_EVERY):
        self.floor, self.every, self.calls = floor, every, 0

    def core(self):
        try:
            return gh_json(["api", "rate_limit"])["resources"]["core"]
        except (GhError, KeyError, TypeError, ValueError):
            return None

    def spend(self):
        if self.calls % self.every == 0:
            core = self.core()
            if core and core["remaining"] < self.floor:
                self.wait_for_reset(core)
        self.calls += 1

    def wait_for_reset(self, core):
        wait = max(0, int(core["reset"] - utcnow().timestamp())) + 5
        print(f"API budget at {core['remaining']} of {core['limit']}; waiting {wait} s for the reset",
              flush=True)
        sleep(wait)

    def after_limit(self):
        core = self.core()
        if core and core["remaining"] < self.floor:
            self.wait_for_reset(core)
        else:
            sleep(60)


def with_retries(fn, budget):
    """fn() through transient failures; 404 and 410 are raised at once."""
    for n in range(1, TRIES + 1):
        budget.spend()
        try:
            return fn()
        except GhError as e:
            if e.status in (404, 410) or n == TRIES:
                raise
            if e.status == 429 or "rate limit" in e.text.lower():
                budget.after_limit()
            else:
                sleep(BACKOFF_S[n - 1])


# --- listing ----------------------------------------------------------------

def list_slice(repo, a, b, budget):
    q = (f"repos/{repo}/actions/runs?status=completed&exclude_pull_requests=true"
         f"&per_page=100&created={a.isoformat()}T00:00:00Z..{b.isoformat()}T23:59:59Z")
    got, total, page = [], 0, 1
    while True:
        body = with_retries(lambda: gh_json(["api", f"{q}&page={page}"]), budget)
        if page == 1:
            total = body.get("total_count", 0)
        batch = body.get("workflow_runs", [])
        got.extend(batch)
        if total > LIST_CAP or len(batch) < 100 or len(got) >= LIST_CAP:
            return got, total
        page += 1


def list_runs(repo, month, budget):
    """Every completed run created in the month, each once, oldest first."""
    start, end = month_bounds(month)
    runs, pending = {}, day_slices(month)
    while pending:
        a, b = pending.pop(0)
        got, total = list_slice(repo, a, b, budget)
        if total > LIST_CAP or len(got) >= LIST_CAP:
            if a == b:
                raise RuntimeError(f"more than {LIST_CAP} runs on {a}; the listing cannot reach them all")
            days = [(a + dt.timedelta(days=i),) * 2 for i in range((b - a).days + 1)]
            pending = days + pending
            continue
        for r in got:
            if start <= parse_time(r["created_at"]) < end:
                runs[r["id"]] = r
    return sorted(runs.values(), key=lambda r: (r["created_at"], r["id"]))
