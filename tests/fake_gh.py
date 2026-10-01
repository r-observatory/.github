#!/usr/bin/env python3
"""A stand-in for gh that serves the JSON file in FAKE_GH_STATE.

State keys: repo (the source repository, and the one holding the release unless
release_repo names another); runs (run objects); partial (the runs a listing
filtered by status and not bounded by created answers with, in place of the
real ones, as GitHub has done); attempts ("<id>/<n>": attempt
object); logs ("<id>/<n>": {"status", "times", "message", "body"}, a zip by
default); sources ({"owner/name": {"runs", "attempts", "logs"}}: further source
repositories); release (the ci-logs release: null, or {"id", "assets": [{"name",
"state"}]}); releases ({tag: the same}, for every other tag); release_status (the
HTTP status every tag lookup answers with); create_status (the status a create
fails with) and create_race (the release exists all the same); rate ({"limit",
"remaining", "reset"}, spent by every api call); rates ({token: the same}, the
rate limit of that token in place of rate); rate_error ({"status", "skip",
"times"}: after `skip` good reads the rate_limit call fails, `times` times or
always); fail_uploads (asset names whose upload fails); store (directory
holding uploaded files, those of another tag in a folder named after it);
private (false makes the repository holding the release public) and repo_status
(the status its own lookup fails with); owner_repos (what users/<owner>/repos
lists) and owner_repos_status (the status that listing fails with); calls
(every argv) and tokens (the GH_TOKEN each call carried).

auth, when set, makes the tokens matter: {"read", "write", "dead": [tokens
answered 401], "expires": the expiry header of the read token, "unselected":
{"owner/name": status its logs answer the read token with}}. Logs are served
only to the read token; releases and the workflow enable only to the write one.
"""
import io
import json
import os
import re
import shutil
import sys
import zipfile
from urllib.parse import parse_qs, urlsplit

PATH = os.environ["FAKE_GH_STATE"]
TOKEN = os.environ.get("GH_TOKEN", "")
NOT_ALLOWED = "Resource not accessible by personal access token"


def load():
    with open(PATH) as f:
        return json.load(f)


def save(state):
    with open(PATH, "w") as f:
        json.dump(state, f, indent=1)


def fail(state, status, message):
    save(state)
    sys.stderr.write(f"gh: {message} (HTTP {status})\n")
    sys.exit(1)


def reply(state, body):
    save(state)
    sys.stdout.write(json.dumps(body))
    sys.exit(0)


def log_zip(key):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("1_build.txt", f"log of {key}\n")
    return buf.getvalue()


def source(state, repo):
    """The runs, attempt records and logs of one source repository, or None."""
    if repo == state["repo"]:
        return state
    return state.get("sources", {}).get(repo)


def release_repo(state):
    return state.get("release_repo") or state["repo"]


def get_release(state, tag):
    return state.get("release") if tag == "ci-logs" else state.get("releases", {}).get(tag)


def set_release(state, tag, value):
    if tag == "ci-logs":
        state["release"] = value
    else:
        state.setdefault("releases", {})[tag] = value


def all_releases(state):
    found = dict(state.get("releases", {}))
    found["ci-logs"] = state.get("release")
    return {tag: rel for tag, rel in found.items() if rel is not None}


def store_dir(state, tag):
    path = state["store"] if tag == "ci-logs" else os.path.join(state["store"], tag)
    os.makedirs(path, exist_ok=True)
    return path


def rate_of(state):
    return state.get("rates", {}).get(TOKEN) or state.get("rate")


def writes_only(state):
    """Releases and the workflow enable answer only the write token."""
    auth = state.get("auth")
    if auth and TOKEN != auth["write"]:
        fail(state, 403, NOT_ALLOWED)


def api(state, args):
    method, include, rest = "GET", False, []
    i = 0
    while i < len(args):
        if args[i] in ("-X", "--method"):
            method, i = args[i + 1], i + 2
        elif args[i] in ("-i", "--include"):
            include, i = True, i + 1
        else:
            rest.append(args[i])
            i += 1
    path = rest[0]
    auth = state.get("auth") or {}
    if TOKEN in auth.get("dead", []):
        fail(state, 401, "Bad credentials")
    if path == "rate_limit":
        err = state.get("rate_error")
        if err and err.get("skip", 0) > 0:
            err["skip"] -= 1
        elif err and err.get("times", 1) > 0:
            if "times" in err:
                err["times"] -= 1
            fail(state, err["status"], err.get("message", "Server Error"))
        core = rate_of(state) or {"limit": 5000, "remaining": 5000, "reset": 0}
        if include:
            sys.stdout.write("HTTP/2.0 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n")
            if auth.get("expires") and TOKEN == auth.get("read"):
                sys.stdout.write(f"Github-Authentication-Token-Expiration: {auth['expires']}\r\n")
            sys.stdout.write("X-Ratelimit-Limit: 5000\r\n\r\n")
        reply(state, {"resources": {"core": core}})
    rate = rate_of(state)
    if rate:
        if rate["remaining"] <= 0:
            fail(state, 403, "API rate limit exceeded for installation ID 1")
        rate["remaining"] -= 1
    url = urlsplit(path)
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    p = url.path
    page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
    home = re.escape(release_repo(state))
    if p == f"repos/{release_repo(state)}":
        writes_only(state)
        if state.get("repo_status"):
            fail(state, state["repo_status"], "Not Found" if state["repo_status"] == 404 else "Bad Gateway")
        reply(state, {"full_name": release_repo(state), "private": state.get("private", True)})
    m = re.fullmatch(r"users/[^/]+/repos", p)
    if m:
        if state.get("owner_repos_status"):
            fail(state, state["owner_repos_status"], "Bad Gateway")
        reply(state, state.get("owner_repos", [])[(page - 1) * per: page * per])
    m = re.fullmatch(rf"repos/{home}/releases/tags/([^/]+)", p)
    if m:
        writes_only(state)
        if state.get("release_status"):
            fail(state, state["release_status"],
                 NOT_ALLOWED if state["release_status"] == 403 else "Bad Gateway")
        rel = get_release(state, m.group(1))
        if rel is None:
            fail(state, 404, "Not Found")
        reply(state, {"id": rel["id"], "tag_name": m.group(1)})
    m = re.fullmatch(rf"repos/{home}/releases/(\d+)/assets", p)
    if m:
        writes_only(state)
        rel = [r for r in all_releases(state).values() if r["id"] == int(m.group(1))]
        if not rel:
            fail(state, 404, "Not Found")
        assets = [{"name": a["name"], "state": a.get("state", "uploaded")} for a in rel[0]["assets"]]
        reply(state, assets[(page - 1) * per: page * per])
    m = re.fullmatch(r"repos/([^/]+/[^/]+)/actions/runs", p)
    if m:
        src = source(state, m.group(1))
        if src is None:
            fail(state, 404, "Not Found")
        runs = src.get("runs", [])
        if "status" in q:
            if "created" not in q:
                runs = src.get("partial", runs)
            runs = [r for r in runs if r["status"] == q["status"]]
        runs = sorted(runs, key=lambda r: r["created_at"], reverse=True)
        if "created" in q:
            a, b = q["created"].split("..")
            runs = [r for r in runs if a <= r["created_at"] <= b]
        if page * per > 1000:
            reply(state, {"total_count": 0, "workflow_runs": []})
        reply(state, {"total_count": len(runs), "workflow_runs": runs[(page - 1) * per: page * per]})
    m = re.fullmatch(r"repos/([^/]+/[^/]+)/actions/runs/(\d+)/attempts/(\d+)(/logs)?", p)
    if m:
        src = source(state, m.group(1))
        if src is None:
            fail(state, 404, "Not Found")
        key = f"{m.group(2)}/{m.group(3)}"
        if m.group(4):
            if auth and TOKEN != auth["read"]:
                fail(state, 403, "Must have admin rights to Repository.")
            refused = auth.get("unselected", {}).get(m.group(1))
            if refused:
                fail(state, refused, "Not Found" if refused == 404 else NOT_ALLOWED)
            spec = src.get("logs", {}).get(key, {})
            if spec.get("times", 1) > 0 and spec.get("status", 200) != 200:
                if "times" in spec:
                    spec["times"] -= 1
                fail(state, spec["status"], spec.get("message", "Server Error"))
            save(state)
            body = spec.get("body")
            sys.stdout.buffer.write(body.encode() if body is not None else log_zip(key))
            sys.exit(0)
        spec = src.get("attempts", {}).get(key)
        if spec is None:
            fail(state, 404, "Not Found")
        if spec.get("status"):
            fail(state, spec["status"], spec.get("message", "Server Error"))
        reply(state, spec)
    m = re.fullmatch(rf"repos/{home}/actions/workflows/([^/]+)/enable", p)
    if m and method == "PUT":
        writes_only(state)
        state.setdefault("enabled", []).append(m.group(1))
        save(state)
        sys.exit(0)
    fail(state, 404, "Not Found")


def release(state, args):
    verb, tag = args[0], args[1]
    auth = state.get("auth") or {}
    if TOKEN in auth.get("dead", []):
        fail(state, 401, "Bad credentials")
    writes_only(state)
    if args[args.index("--repo") + 1] != release_repo(state):
        fail(state, 404, "Not Found")
    if verb == "create":
        new = {"id": 7 if tag == "ci-logs" else 100 + len(all_releases(state)), "assets": []}
        if state.get("create_status"):
            if state.get("create_race"):
                set_release(state, tag, new)
            fail(state, state["create_status"], "Validation Failed")
        set_release(state, tag, new)
        reply(state, {})
    rel = get_release(state, tag)
    if rel is None:
        fail(state, 404, "release not found")
    names = [a["name"] for a in rel["assets"]]
    if verb == "upload":
        for path in args[2:args.index("--repo")]:
            name = os.path.basename(path)
            if name in state.get("fail_uploads", []):
                fail(state, 502, "upload failed")
            if name in names:
                fail(state, 422, "Validation Failed")
            shutil.copyfile(path, os.path.join(store_dir(state, tag), name))
            rel["assets"].append({"name": name, "state": "uploaded"})
        save(state)
        sys.exit(0)
    if verb == "download":
        name = args[args.index("--pattern") + 1]
        out = args[args.index("--dir") + 1]
        if name not in names:
            fail(state, 404, "no assets match the file pattern")
        shutil.copyfile(os.path.join(store_dir(state, tag), name), os.path.join(out, name))
        save(state)
        sys.exit(0)
    fail(state, 400, f"fake gh has no release {verb}")


def main():
    state = load()
    state.setdefault("calls", []).append(sys.argv[1:])
    state.setdefault("tokens", []).append(TOKEN)
    args = sys.argv[1:]
    if args[0] == "api":
        api(state, args[1:])
    if args[0] == "release":
        release(state, args[1:])
    fail(state, 400, f"fake gh has no {args[0]}")


if __name__ == "__main__":
    main()
