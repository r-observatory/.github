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


class BudgetUnreadable(Exception):
    """The rate limit could not be read; the run stops rather than go on unchecked."""


class Budget:
    """Leaves a reserve of the hourly API budget to the repository's own workflows."""

    def __init__(self, floor=BUDGET_FLOOR, every=BUDGET_EVERY):
        self.floor, self.every, self.calls = floor, every, 0

    def core(self):
        """The core rate limit, read up to TRIES times."""
        args = ["api", "rate_limit"]
        for n in range(1, TRIES + 1):
            try:
                core = gh_json(args)["resources"]["core"]
                return {k: int(core[k]) for k in ("limit", "remaining", "reset")}
            except GhError as e:
                why = str(e)
            except (KeyError, TypeError, ValueError):
                why = f"gh {' '.join(args)}: the reply did not give the core limit"
            if n < TRIES:
                print(f"the API budget could not be read ({why}); trying again in "
                      f"{BACKOFF_S[n - 1]} s", flush=True)
                sleep(BACKOFF_S[n - 1])
        raise BudgetUnreadable(f"the API budget could not be read in {TRIES} tries ({why}); "
                               "stopping, because the reserve cannot be checked")

    def spend(self):
        if self.calls % self.every == 0:
            core = self.core()
            if core["remaining"] < self.floor:
                self.wait_for_reset(core)
        self.calls += 1

    def wait_for_reset(self, core):
        wait = max(0, int(core["reset"] - utcnow().timestamp())) + 5
        print(f"API budget at {core['remaining']} of {core['limit']}; waiting {wait} s for the reset",
              flush=True)
        sleep(wait)

    def after_limit(self):
        core = self.core()
        if core["remaining"] < self.floor:
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


# --- fetching ---------------------------------------------------------------

def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-") or "workflow"


def member_name(row):
    created = str(row["created_at"])[:19].replace(":", "")
    return (f"{created}_{row['run_id']}_a{row['run_attempt']}_"
            f"{slug(str(row['workflow_name']))}_{row['conclusion'] or 'none'}.zip")


def attempt_meta(repo, run, n, budget):
    if n == run["run_attempt"]:
        return run
    try:
        return with_retries(
            lambda: gh_json(["api", f"repos/{repo}/actions/runs/{run['id']}/attempts/{n}"]), budget)
    except GhError:
        return dict(run, conclusion=None, run_started_at=None, updated_at=None)


def row_for(run, meta, n):
    return {"run_id": run["id"], "run_attempt": n,
            "workflow_name": run.get("name") or "", "workflow_path": run.get("path") or "",
            "event": run.get("event") or "", "conclusion": meta.get("conclusion") or "",
            "head_branch": run.get("head_branch") or "", "head_sha": run.get("head_sha") or "",
            "created_at": run.get("created_at") or "",
            "run_started_at": meta.get("run_started_at") or "",
            "updated_at": meta.get("updated_at") or "",
            "zip_bytes": "", "zip_sha256": "", "outcome": ""}


def remove(path):
    if os.path.exists(path):
        os.remove(path)


def fetch_log(repo, run_id, n, dest, budget):
    """'ok' with the zip at dest, 'gone' when GitHub no longer has it, else 'error'."""
    args = ["api", f"repos/{repo}/actions/runs/{run_id}/attempts/{n}/logs"]

    def once():
        try:
            gh(args, out=dest)
        except GhError:
            remove(dest)
            raise
        if not zipfile.is_zipfile(dest):
            remove(dest)
            raise GhError(args, None, "the response was not a zip archive")

    try:
        with_retries(once, budget)
        return "ok"
    except GhError as e:
        if e.status in (404, 410):
            return "gone"
        print(f"::warning::run {run_id} attempt {n}: {e.text.strip()[:200]}", flush=True)
        return "error"


def cache_index(cache_dir):
    """Zips an earlier local fetch saved as <created>_<run id>_<workflow>.zip, by run id."""
    out = {}
    if cache_dir:
        for name in os.listdir(cache_dir):
            m = LOCAL_NAME.match(name)
            if m:
                out[int(m.group(1))] = os.path.join(cache_dir, name)
    return out


def cached_zip(cache, run, n):
    """The saved zip, when it holds this attempt: the latest one, saved after it ended."""
    path = cache.get(run["id"])
    if not path or n != run["run_attempt"] or not run.get("updated_at"):
        return None
    if os.path.getmtime(path) < parse_time(run["updated_at"]).timestamp():
        return None
    return path if zipfile.is_zipfile(path) else None


def file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return os.path.getsize(path), h.hexdigest()


def collect_month(repo, month, work_dir, budget, cache=None):
    """Every attempt of every completed run in the month, zips in <work>/<month>/zips."""
    zips = os.path.join(work_dir, month, "zips")
    os.makedirs(zips, exist_ok=True)
    have = {}
    for name in os.listdir(zips):
        m = MEMBER_NAME.match(name)
        if m:
            have[(int(m.group(1)), int(m.group(2)))] = os.path.join(zips, name)
    rows = []
    for run in list_runs(repo, month, budget):
        for n in range(1, int(run["run_attempt"]) + 1):
            row = row_for(run, attempt_meta(repo, run, n, budget), n)
            dest = os.path.join(zips, member_name(row))
            prior = have.get((run["id"], n))
            local = cached_zip(cache or {}, run, n)
            if prior and zipfile.is_zipfile(prior):
                os.replace(prior, dest)
                row["outcome"] = "ok"
            elif local:
                shutil.copyfile(local, dest)
                row["outcome"] = "ok"
            else:
                row["outcome"] = fetch_log(repo, run["id"], n, dest, budget)
            if row["outcome"] == "ok":
                row["zip_bytes"], row["zip_sha256"] = file_digest(dest)
            rows.append(row)
    return rows


# --- the monthly pair -------------------------------------------------------

def pair_names(month):
    return f"ci-logs-{month}.tar", f"ci-logs-{month}.tsv"


def tsv_bytes(rows):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([r[c] for c in COLUMNS])
    return buf.getvalue().encode("utf-8")


def read_index(data):
    reader = csv.DictReader(io.StringIO(data.decode("utf-8")), delimiter="\t")
    if reader.fieldnames != COLUMNS:
        raise ValueError(f"index columns are {reader.fieldnames}")
    return list(reader)


def plain(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mode = 0o644
    return info


def write_pair(month, rows, work_dir):
    base = os.path.join(work_dir, month)
    tar_path, tsv_path = (os.path.join(base, n) for n in pair_names(month))
    index = tsv_bytes(rows)
    with open(tsv_path, "wb") as f:
        f.write(index)
    with tarfile.open(tar_path + ".part", "w", format=tarfile.PAX_FORMAT) as tar:
        info = plain(tarfile.TarInfo("index.tsv"))
        info.size, info.mtime = len(index), int(utcnow().timestamp())
        tar.addfile(info, io.BytesIO(index))
        for r in rows:
            if r["outcome"] == "ok":
                name = member_name(r)
                tar.add(os.path.join(base, "zips", name), arcname=name, filter=plain)
    os.replace(tar_path + ".part", tar_path)
    return tar_path, tsv_path


def file_bytes(tar, member):
    """What a regular file in the tar holds; None for a missing member or any other entry."""
    if member is None or not member.isfile():
        return None
    return tar.extractfile(member).read()


def verify_pair(tar_path, tsv_path):
    """The rows of a pair whose tar holds the same index and exactly the zips it lists."""
    with open(tsv_path, "rb") as f:
        index = f.read()
    rows = read_index(index)
    with tarfile.open(tar_path) as tar:
        members = {m.name: m for m in tar.getmembers()}
        if file_bytes(tar, members.get("index.tsv")) != index:
            raise ValueError(f"{os.path.basename(tar_path)} does not carry this index")
        listed = {"index.tsv"}
        for r in rows:
            if r["outcome"] != "ok":
                continue
            name = member_name(r)
            if name not in members:
                raise ValueError(f"{name} is in the index but not in the tar")
            data = file_bytes(tar, members[name])
            if data is None:
                raise ValueError(f"{name} is not a regular file in the tar")
            if len(data) != int(r["zip_bytes"]) or hashlib.sha256(data).hexdigest() != r["zip_sha256"]:
                raise ValueError(f"{name} does not match its size and sha256")
            listed.add(name)
        extra = sorted(set(members) - listed)
        if extra:
            raise ValueError(f"the tar holds files the index does not list: {extra[:3]}")
    return rows


def summary(month, rows):
    n = {k: sum(r["outcome"] == k for r in rows) for k in ("ok", "gone", "error")}
    runs = len({r["run_id"] for r in rows})
    mb = sum(int(r["zip_bytes"] or 0) for r in rows) / 1e6
    return (f"{month}: {runs} runs, {len(rows)} attempts: {n['ok']} ok, {n['gone']} gone, "
            f"{n['error']} error, {mb:.1f} MB")


# --- the release ------------------------------------------------------------

def release_state(repo, budget):
    """{'id', 'assets': {name: state}} for the ci-logs release, or None on a 404."""
    try:
        rel = with_retries(lambda: gh_json(["api", f"repos/{repo}/releases/tags/{TAG}"]), budget)
    except GhError as e:
        if e.status == 404:
            return None
        raise
    assets, page = {}, 1
    while True:
        batch = with_retries(lambda: gh_json(
            ["api", f"repos/{repo}/releases/{rel['id']}/assets?per_page=100&page={page}"]), budget)
        assets.update({a["name"]: a.get("state", "uploaded") for a in batch})
        if len(batch) < 100:
            return {"id": rel["id"], "assets": assets}
        page += 1


def ensure_release(repo, budget):
    """The release state, creating the release only when the lookup answered 404."""
    state = release_state(repo, budget)
    if state is not None:
        return state
    print(f"creating release {TAG} in {repo}", flush=True)
    budget.spend()
    try:
        gh(["release", "create", TAG, "--repo", repo, "--prerelease", "--latest=false",
            "--title", "CI logs", "--notes", RELEASE_NOTES])
    except GhError as e:
        # Another run may have created it first; the lookup below decides.
        print(f"create failed ({e.text.strip()[:120]}); looking the release up again", flush=True)
    state = release_state(repo, budget)
    if state is None:
        raise RuntimeError(f"release {TAG} does not exist after the attempt to create it")
    return state


def release_assets(repo, budget):
    """The assets of the release the run found or created when it began."""
    state = release_state(repo, budget)
    if state is None:
        raise RuntimeError(f"release {TAG} was not found; it was there when the run began")
    return state["assets"]


def month_status(assets, month):
    names = pair_names(month)
    if any(n in assets and assets[n] != "uploaded" for n in names):
        return "stuck"
    has_tar, has_tsv = (n in assets for n in names)
    if has_tar and has_tsv:
        return "archived"
    return "lone_tar" if has_tar else "lone_tsv" if has_tsv else "absent"


def upload_asset(repo, path, budget):
    name = os.path.basename(path)
    for n in range(1, TRIES + 1):
        budget.spend()
        try:
            gh(["release", "upload", TAG, path, "--repo", repo])
            return
        except GhError as e:
            state = release_state(repo, budget)
            if state and state["assets"].get(name) == "uploaded":
                return
            if n == TRIES:
                raise
            print(f"upload of {name} failed ({e.text.strip()[:120]}); retrying", flush=True)
            sleep(BACKOFF_S[n - 1])


def repair_tsv(repo, month, work_dir, budget):
    """Upload the index a lone tar carries, after an upload that stopped between the files."""
    tar_name, tsv_name = pair_names(month)
    base = os.path.join(work_dir, month, "repair")
    os.makedirs(base, exist_ok=True)
    with_retries(lambda: gh(["release", "download", TAG, "--repo", repo, "--pattern", tar_name,
                             "--dir", base, "--clobber"]), budget)
    tar_path, tsv_path = os.path.join(base, tar_name), os.path.join(base, tsv_name)
    with tarfile.open(tar_path) as tar:
        index = file_bytes(tar, {m.name: m for m in tar.getmembers()}.get("index.tsv"))
    if index is None:
        raise ValueError(f"{tar_name} on the release has no index.tsv file")
    with open(tsv_path, "wb") as f:
        f.write(index)
    verify_pair(tar_path, tsv_path)
    upload_asset(repo, tsv_path, budget)


def publish_month(repo, month, tar_path, tsv_path, now, budget):
    """'uploaded', 'uploaded_with_errors', 'held', or 'archived' when another upload won."""
    rows = verify_pair(tar_path, tsv_path)
    errors = sum(r["outcome"] == "error" for r in rows)
    if errors and not upload_anyway(month, now):
        print(f"::error::{month}: {errors} attempts failed to download; the month waits until "
              f"they do or until day {UPLOAD_ANYWAY_DAY}", flush=True)
        return "held"
    if month_status(release_assets(repo, budget), month) != "absent":
        print(f"{month}: archived by another upload meanwhile; this pair is not uploaded", flush=True)
        return "archived"
    upload_asset(repo, tar_path, budget)
    upload_asset(repo, tsv_path, budget)
    print(f"{month}: uploaded {os.path.basename(tar_path)} and {os.path.basename(tsv_path)}", flush=True)
    if errors:
        print(f"::error::{month}: uploaded with {errors} attempts recorded as error", flush=True)
        return "uploaded_with_errors"
    return "uploaded"


def settle_month(repo, month, work_dir, budget, now, produce):
    """Bring one month to archived; False when it needs another run or a hand."""
    status = month_status(release_assets(repo, budget), month)
    if status == "archived":
        print(f"{month}: already archived", flush=True)
        return True
    if status == "lone_tar":
        repair_tsv(repo, month, work_dir, budget)
        print(f"{month}: uploaded the index its tar carries", flush=True)
        return True
    if status != "absent":
        print(f"::error::{month}: the release holds an incomplete pair ({status}); "
              "check the assets by hand", flush=True)
        return False
    tar_path, tsv_path = produce(month)
    return publish_month(repo, month, tar_path, tsv_path, now, budget) in ("uploaded", "archived")


# --- commands ---------------------------------------------------------------

def cmd_archive(a):
    now = utcnow()
    months = check_months(a.month, now) if a.month else months_due(now)
    late = sorted(set(months) - set(months_due(now)))
    if late:
        # A month whose first days are already gone is built from a local copy instead.
        raise UsageError(f"{', '.join(late)}: its earliest logs are past retention; "
                         "use build with a local copy, then upload")
    budget = Budget()
    ensure_release(a.repo, budget)

    def produce(month):
        rows = collect_month(a.repo, month, a.work_dir, budget)
        print(summary(month, rows), flush=True)
        return write_pair(month, rows, a.work_dir)

    ok = True
    for month in months:
        try:
            ok &= settle_month(a.repo, month, a.work_dir, budget, now, produce)
        except (GhError, RuntimeError, ValueError, OSError, tarfile.TarError) as e:
            print(f"::error::{month}: {e}", flush=True)
            ok = False
    return 0 if ok else 1


def cmd_build(a):
    now = utcnow()
    months = check_months(a.month, now)
    if not months:
        raise UsageError("build needs at least one --month")
    budget, cache, errors = Budget(), cache_index(a.cache_dir), 0
    for month in months:
        rows = collect_month(a.repo, month, a.work_dir, budget, cache)
        verify_pair(*write_pair(month, rows, a.work_dir))
        print(summary(month, rows), flush=True)
        errors += sum(r["outcome"] == "error" for r in rows)
    return 1 if errors else 0


def cmd_upload(a):
    now = utcnow()
    months = check_months(a.month, now)
    if not months:
        raise UsageError("upload needs at least one --month")
    budget = Budget()
    ensure_release(a.repo, budget)

    def produce(month):
        paths = [os.path.join(a.work_dir, month, n) for n in pair_names(month)]
        if not all(os.path.exists(p) for p in paths):
            raise RuntimeError(f"no built pair for {month} in {a.work_dir}")
        return paths

    ok = True
    for month in months:
        try:
            ok &= settle_month(a.repo, month, a.work_dir, budget, now, produce)
        except (GhError, RuntimeError, ValueError, OSError, tarfile.TarError) as e:
            print(f"::error::{month}: {e}", flush=True)
            ok = False
    return 0 if ok else 1


def cmd_keepalive(a):
    """Re-enabling a workflow restarts GitHub's 60-day inactivity clock for it."""
    path = a.workflow_ref.split("@", 1)[0]
    prefix = f"{a.repo}/.github/workflows/"
    name = path[len(prefix):] if path.startswith(prefix) else ""
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.ya?ml", name):
        raise UsageError(f"{a.workflow_ref!r} is not a workflow of {a.repo}")
    with_retries(lambda: gh(["api", "-X", "PUT", f"repos/{a.repo}/actions/workflows/{name}/enable"]),
                 Budget())
    print(f"re-enabled {name}", flush=True)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("archive", "build", "upload"):
        s = sub.add_parser(name)
        s.add_argument("--repo", required=True, help="owner/name")
        s.add_argument("--month", action="append", default=[], help="YYYY-MM; repeatable")
        s.add_argument("--work-dir", required=True)
        if name == "build":
            s.add_argument("--cache-dir", help="zips saved earlier as <created>_<run id>_<workflow>.zip")
    k = sub.add_parser("keepalive")
    k.add_argument("--repo", required=True)
    k.add_argument("--workflow-ref", required=True)
    a = p.parse_args(argv)
    commands = {"archive": cmd_archive, "build": cmd_build, "upload": cmd_upload,
                "keepalive": cmd_keepalive}
    try:
        return commands[a.command](a)
    except UsageError as e:
        print(f"::error::{e}", flush=True)
        return 2
    except (GhError, BudgetUnreadable, RuntimeError, ValueError, OSError) as e:
        print(f"::error::{e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
