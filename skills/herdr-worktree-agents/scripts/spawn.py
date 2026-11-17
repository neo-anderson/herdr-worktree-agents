#!/usr/bin/env python3
"""Start one coding agent in its own herdr worktree workspace, then watch it.

Creates a git worktree (herdr opens it as its own workspace with one tab),
starts the agent there, sends the task plus a completion contract, and launches
a detached watcher (watch.py) that closes the agent's tab after its whole chain
of work is finished and a grace countdown expires. The worktree is kept;
cleanup.py removes it once the user agrees.

Exit codes: 0 started, 2 usage error, 3 agent blocked at startup (resolve the
dialog with the user, then rerun with --continue), 1 other failure.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hwa_common import (  # noqa: E402
    MARKER_DIR,
    HerdrError,
    git,
    herdr,
    load_meta,
    now,
    run_dir,
    save_meta,
)
from watch import command_shells  # noqa: E402

# YOLO mode: start agents with their approval prompts (and, for Codex, the
# sandbox) turned off. Off by default. Enable per run with --yolo, or for every
# run with `yolo = true` in the config file. --no-yolo overrides the config.
CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "herdr-worktree-agents",
    "config.toml",
)
PERMISSION_BYPASS = {
    "codex": "--dangerously-bypass-approvals-and-sandbox",
    "claude": "--dangerously-skip-permissions",
}

CONTRACT = """

---
Completion contract (required):
When your WHOLE task is finished, including every subagent, background agent,
and background command you started, and you have collected their results:
  1. Write your final report as Markdown to: {report}
  2. Then run: touch {done}
Do not create the done file while anything you started is still running. If you
start new work after creating it, delete it first and recreate it when finished.
About {grace} seconds after the done file exists and you are idle, this tab
closes and your session ends. Your worktree stays on disk until the user decides
to remove it.
"""


def fail(msg, code=1, **extra):
    print(json.dumps({"ok": False, "error": msg, **extra}, indent=2))
    sys.exit(code)


def ensure_excluded(repo):
    common = git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    exclude = os.path.join(common, "info", "exclude")
    os.makedirs(os.path.dirname(exclude), exist_ok=True)
    line = f"/{MARKER_DIR}/"
    existing = open(exclude).read() if os.path.exists(exclude) else ""
    if line not in existing.splitlines():
        with open(exclude, "a") as f:
            f.write(("" if existing.endswith("\n") or not existing else "\n") + line + "\n")


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        return {}
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def resolve_base(repo, base, pr, remote):
    if pr:
        # GitHub-style pull request refs.
        git(repo, "fetch", "--quiet", remote, f"pull/{pr}/head")
        return git(repo, "rev-parse", "FETCH_HEAD")
    return git(repo, "rev-parse", "--verify", f"{base or 'HEAD'}^{{commit}}")


def agent_status(name):
    try:
        return herdr("agent", "get", name)["agent"]["agent_status"]
    except HerdrError:
        return None


def start_agent(meta, start):
    """Start the agent, retrying once if the first attempt fails for a reason
    other than a startup dialog (for example, a transient herdr error)."""
    try:
        herdr(*start)
        return
    except HerdrError as e:
        if e.code == "agent_not_ready":
            raise
        meta.setdefault("start_errors", []).append(f"{e.code}: {e.message}")
        save_meta(meta)
    time.sleep(3)
    status = agent_status(meta["name"])
    if status in ("idle", "done"):
        return  # it started after all; only the readiness check failed
    if status == "blocked":
        raise HerdrError("agent_not_ready", "agent is blocked during startup")
    if status is None:
        herdr(*start)  # the pane is still a plain shell: try once more
        return
    # The agent is present but neither ready nor blocked (e.g. still loading).
    herdr("agent", "wait", meta["name"], "--until", "idle", "--until", "blocked",
          "--timeout", "30000")
    if agent_status(meta["name"]) == "blocked":
        raise HerdrError("agent_not_ready", "agent is blocked during startup")


def undo_failed_start(meta, err):
    """No agent ever ran here, so the tab and worktree hold nothing of value.
    Close the tab and remove the fresh worktree and branch so the same spawn
    command can be rerun. Anything unexpected on disk is left in place."""
    meta["state"] = "start_failed"
    meta["start_error"] = f"{err.code}: {err.message}"
    herdr("workspace", "close", meta["workspace_id"], check=False)
    path, repo = meta["worktree_path"], meta["repo"]
    shutil.rmtree(os.path.join(path, MARKER_DIR), ignore_errors=True)
    if os.path.isdir(path) and not git(path, "status", "--porcelain", "--ignored", check=False):
        git(repo, "worktree", "remove", path, check=False)
    if not os.path.isdir(path):
        tip = git(repo, "rev-parse", "--verify", "--quiet", meta["branch"], check=False)
        if tip and tip == meta["start_commit"]:
            git(repo, "branch", "-D", meta["branch"], check=False)
    save_meta(meta)


def send_prompt_and_watch(meta):
    with open(meta["prompt_file"]) as f:
        task = f.read().strip()
    # Shells that exist before the task (e.g. MCP launchers) never count as work.
    meta["baseline_shells"] = sorted(command_shells(meta["pane_id"]))
    text = task + CONTRACT.format(
        report=meta["report_path"], done=meta["done_path"], grace=meta["grace"]
    )
    herdr("agent", "prompt", meta["name"], text)
    # Confirm the agent picked the prompt up; not fatal if it was very fast.
    herdr("agent", "wait", meta["name"], "--until", "working", "--until", "blocked",
          "--timeout", "20000", check=False)

    log = open(os.path.join(run_dir(meta["run_id"]), f"{meta['name']}.log"), "a")
    watcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watch.py")
    proc = subprocess.Popen(
        [sys.executable, "-I", watcher, "--run", meta["run_id"], "--name", meta["name"]],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True, env=os.environ.copy(),
    )
    meta["watcher_pid"] = proc.pid
    meta["state"] = "running"
    meta["prompted_at"] = now()
    save_meta(meta)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run", required=True, help="run id shared by agents started together")
    ap.add_argument("--name", required=True, help="unique agent name, [a-z][a-z0-9_-]{0,31}")
    ap.add_argument("--continue", dest="cont", action="store_true",
                    help="after resolving a startup dialog: send the prompt and start watching")
    ap.add_argument("--repo", help="path to the main git checkout")
    ap.add_argument("--kind", help="herdr agent kind, e.g. claude, codex")
    ap.add_argument("--prompt-file", help="file holding the task for the agent")
    ap.add_argument("--base", help="git ref to branch from (default: repo HEAD)")
    ap.add_argument("--pr", help="GitHub PR number; branch from its head commit")
    ap.add_argument("--branch", help="worktree branch name (default herdr/<run>-<name>)")
    ap.add_argument("--grace", type=int, default=120, help="countdown seconds before cleanup")
    ap.add_argument("--remote", default="origin", help="git remote that --pr fetches from")
    yolo = ap.add_mutually_exclusive_group()
    yolo.add_argument("--yolo", dest="yolo", action="store_true", default=None,
                      help="start claude/codex with approval prompts turned off")
    yolo.add_argument("--no-yolo", dest="yolo", action="store_false",
                      help="keep normal approval prompts even if the config enables YOLO")
    ap.add_argument("agent_args", nargs="*", help="native agent args, after --")
    a = ap.parse_args()

    if os.environ.get("HERDR_ENV") != "1":
        fail("not running inside a herdr pane (HERDR_ENV != 1)", 2)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", a.name):
        fail("name must match [a-z][a-z0-9_-]{0,31}", 2)
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", a.run):
        fail("run id must match [A-Za-z0-9._-]{1,64}", 2)

    if a.cont:
        meta = load_meta(a.run, a.name)
        if meta.get("state") != "blocked_at_startup":
            fail("--continue only resumes an agent that was blocked at startup; "
                 f"this one is '{meta.get('state')}'", 2)
        status = agent_status(a.name)
        if status is None:
            fail("no agent is running in its pane any more. Close its tab with "
                 f"`herdr workspace close {meta['workspace_id']}`, then spawn again "
                 "with a new name.", 1, workspace_id=meta["workspace_id"])
        if status == "blocked":
            fail("agent is still blocked; resolve the dialog first", 3, pane_id=meta["pane_id"])
        send_prompt_and_watch(meta)
        print(json.dumps({"ok": True, **meta}, indent=2))
        return

    for flag in ("repo", "kind", "prompt_file"):
        if not getattr(a, flag):
            fail(f"--{flag.replace('_', '-')} is required", 2)
    repo = git(os.path.abspath(os.path.expanduser(a.repo)), "rev-parse", "--show-toplevel")
    prompt_file = os.path.abspath(a.prompt_file)
    if not os.path.isfile(prompt_file):
        fail(f"prompt file not found: {prompt_file}", 2)

    base_sha = resolve_base(repo, a.base, a.pr, a.remote)
    branch = a.branch or f"herdr/{a.run}-{a.name}"
    ensure_excluded(repo)

    res = herdr("worktree", "create", "--cwd", repo, "--branch", branch, "--base", base_sha,
                "--label", a.name, "--no-focus")
    ws = res["workspace"]
    path = res["worktree"]["path"]
    meta = {
        "run_id": a.run, "name": a.name, "kind": a.kind, "repo": repo, "branch": branch,
        "base_sha": base_sha, "pr": a.pr, "worktree_path": path,
        "workspace_id": ws["workspace_id"], "tab_id": res["tab"]["tab_id"],
        "pane_id": res["root_pane"]["pane_id"], "grace": a.grace,
        "prompt_file": prompt_file,
        "report_path": os.path.join(path, MARKER_DIR, "report.md"),
        "done_path": os.path.join(path, MARKER_DIR, "done"),
        "saved_report": os.path.join(run_dir(a.run), f"{a.name}.md"),
        "start_commit": git(path, "rev-parse", "HEAD"),
        "created_at": now(), "state": "starting",
    }
    os.makedirs(os.path.join(path, MARKER_DIR), exist_ok=True)
    save_meta(meta)
    herdr("tab", "rename", meta["tab_id"], a.name, check=False)

    try:
        start = ["agent", "start", a.name, "--kind", a.kind, "--pane", meta["pane_id"],
                 "--timeout", "60000"]
        agent_args = list(a.agent_args)
        use_yolo = a.yolo if a.yolo is not None else bool(load_config().get("yolo", False))
        meta["yolo"] = use_yolo
        if use_yolo:
            flag = PERMISSION_BYPASS.get(a.kind)
            if flag and flag not in agent_args:
                agent_args.insert(0, flag)
        meta["agent_args"] = agent_args
        save_meta(meta)
        if agent_args:
            start += ["--", *agent_args]
        start_agent(meta, start)
    except HerdrError as e:
        if e.code == "agent_not_ready":
            meta["state"] = "blocked_at_startup"
            save_meta(meta)
            screen = herdr("agent", "read", a.name, "--source", "visible", check=False)
            fail("agent is blocked at startup; show the user this dialog, resolve it with "
                 "`herdr agent send-keys`, then rerun with --continue", 3,
                 pane_id=meta["pane_id"], workspace_id=meta["workspace_id"],
                 screen=screen.get("raw", screen))
        undo_failed_start(meta, e)
        kept = os.path.isdir(meta["worktree_path"])
        fail(f"agent did not start ({e.code}: {e.message}). Its tab was closed"
             + ("; its worktree was kept because it is not empty." if kept else
                " and its unused worktree removed, so the same spawn command can be rerun."),
             1, state=meta["state"], worktree_kept=kept)

    send_prompt_and_watch(meta)
    print(json.dumps({"ok": True, **meta}, indent=2))


if __name__ == "__main__":
    main()
