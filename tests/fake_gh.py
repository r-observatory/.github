#!/usr/bin/env python3
"""A stand-in for gh that serves one repository from the JSON file in FAKE_GH_STATE.

State keys: repo; runs (run objects); attempts ("<id>/<n>": attempt object);
logs ("<id>/<n>": {"status", "times", "message", "body"}, a zip by default);
release (null, or {"id", "assets": [{"name", "state"}]}); release_status (the
HTTP status every tag lookup answers with); create_status (the status a create
fails with) and create_race (the release exists all the same); rate ({"limit",
"remaining", "reset"}, spent by every api call); rate_error ({"status", "skip",
"times"}: after `skip` good reads the rate_limit call fails, `times` times or
always); fail_uploads (asset names whose upload fails); store (directory
holding uploaded files); calls (every argv).
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


def api(state, args):
    method, rest = "GET", []
    i = 0
    while i < len(args):
        if args[i] in ("-X", "--method"):
            method, i = args[i + 1], i + 2
        else:
            rest.append(args[i])
            i += 1
    path = rest[0]
    if path == "rate_limit":
        err = state.get("rate_error")
        if err and err.get("skip", 0) > 0:
            err["skip"] -= 1
        elif err and err.get("times", 1) > 0:
            if "times" in err:
                err["times"] -= 1
            fail(state, err["status"], err.get("message", "Server Error"))
        core =state.get("rate") or {"limit": 5000, "remaining": 5000, "reset": 0}
        reply(state, {"resources": {"core": core}})
    rate = state.get("rate")
    if rate:
        if rate["remaining"] <= 0:
            fail(state, 403, "API rate limit exceeded for installation ID 1")
        rate["remaining"] -= 1
    repo = state["repo"]
    url = urlsplit(path)
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    p = url.path
    if p == f"repos/{repo}/releases/tags/ci-logs":
        if state.get("release_status"):
            fail(state, state["release_status"], "Bad Gateway")
        if state.get("release") is None:
            fail(state, 404, "Not Found")
        reply(state, {"id": state["release"]["id"], "tag_name": "ci-logs"})
    m = re.fullmatch(rf"repos/{re.escape(repo)}/releases/(\d+)/assets", p)
    if m:
        page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
        assets = [{"name": a["name"], "state": a.get("state", "uploaded")}
                  for a in state["release"]["assets"]]
        reply(state, assets[(page - 1) * per: page * per])
    if p == f"repos/{repo}/actions/runs":
        a, b = q["created"].split("..")
        page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
        runs = sorted((r for r in state["runs"] if a <= r["created_at"] <= b),
                      key=lambda r: r["created_at"], reverse=True)
        if page * per > 1000:
            reply(state, {"total_count": 0, "workflow_runs": []})
        reply(state, {"total_count": len(runs), "workflow_runs": runs[(page - 1) * per: page * per]})
    m = re.fullmatch(rf"repos/{re.escape(repo)}/actions/runs/(\d+)/attempts/(\d+)(/logs)?", p)
    if m:
        key = f"{m.group(1)}/{m.group(2)}"
        if m.group(3):
            spec = state.get("logs", {}).get(key, {})
            if spec.get("times", 1) > 0 and spec.get("status", 200) != 200:
                if "times" in spec:
                    spec["times"] -= 1
                fail(state, spec["status"], spec.get("message", "Server Error"))
            save(state)
            body = spec.get("body")
            sys.stdout.buffer.write(body.encode() if body is not None else log_zip(key))
            sys.exit(0)
        if key not in state.get("attempts", {}):
            fail(state, 404, "Not Found")
        reply(state, state["attempts"][key])
    m = re.fullmatch(rf"repos/{re.escape(repo)}/actions/workflows/([^/]+)/enable", p)
    if m and method == "PUT":
        state.setdefault("enabled", []).append(m.group(1))
        save(state)
        sys.exit(0)
    fail(state, 404, "Not Found")


def release(state, args):
    verb = args[0]
    if verb == "create":
        if state.get("create_status"):
            if state.get("create_race"):
                state["release"] = {"id": 7, "assets": []}
            fail(state, state["create_status"], "Validation Failed")
        state["release"] = {"id": 7, "assets": []}
        reply(state, {})
    names = [a["name"] for a in state["release"]["assets"]]
    if verb == "upload":
        for path in args[2:args.index("--repo")]:
            name = os.path.basename(path)
            if name in state.get("fail_uploads", []):
                fail(state, 502, "upload failed")
            if name in names:
                fail(state, 422, "Validation Failed")
            shutil.copyfile(path, os.path.join(state["store"], name))
            state["release"]["assets"].append({"name": name, "state": "uploaded"})
        save(state)
        sys.exit(0)
    if verb == "download":
        name = args[args.index("--pattern") + 1]
        out = args[args.index("--dir") + 1]
        if name not in names:
            fail(state, 404, "no assets match the file pattern")
        shutil.copyfile(os.path.join(state["store"], name), os.path.join(out, name))
        save(state)
        sys.exit(0)
    fail(state, 400, f"fake gh has no release {verb}")


def main():
    state = load()
    state.setdefault("calls", []).append(sys.argv[1:])
    args = sys.argv[1:]
    if args[0] == "api":
        api(state, args[1:])
    if args[0] == "release":
        release(state, args[1:])
    fail(state, 400, f"fake gh has no {args[0]}")


if __name__ == "__main__":
    main()
