#!/usr/bin/env python3
"""Watch one agent started by spawn.py and clean up after its whole chain finishes.

The chain counts as finished only while ALL of these hold:
  * the agent created its done file (it promises to do so only after every
    subagent, background agent, and background command has finished),
  * herdr reports the agent as idle or done,
  * no command shells (e.g. `zsh -c ...`, `bash -lc ...`) are running under the
    agent, other than ones that existed before it got its task.
When they first all hold, a countdown starts (default 120 s) and is shown in the
tab label. Any change resets it. When it expires, the watcher saves the report
and closes the agent's workspace and tab, which ends its terminal session. The
worktree and branch stay on disk; cleanup.py removes them once the user agrees.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hwa_common import HerdrError, herdr, load_meta, now, save_meta  # noqa: E402

POLL = 5
SHELL_CMD = re.compile(r"^(\S*/)?(ba|z|da|k|fi)?sh\s+(-\w*c\w*)\s")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def notify(title, body, sound="done"):
    herdr("notification", "show", title, "--body", body, "--sound", sound, check=False)


def set_label(meta, label):
    herdr("tab", "rename", meta["tab_id"], label, check=False)


def command_shells(pane_id):
    """PIDs of shell `-c` processes descended from the pane's shell."""
    info = herdr("pane", "process-info", "--pane", pane_id, check=False)
    root = info.get("process_info", {}).get("shell_pid")
    if not root:
        return set()
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="],
                         capture_output=True, text=True).stdout
    children, cmds = {}, {}
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, ppid, cmd = int(parts[0]), int(parts[1]), parts[2]
        children.setdefault(ppid, []).append(pid)
        cmds[pid] = cmd
    found, stack = set(), list(children.get(root, []))
    while stack:
        pid = stack.pop()
        if SHELL_CMD.match(cmds.get(pid, "")):
            found.add(pid)
        stack.extend(children.get(pid, []))
    return found


def workspace_exists(ws_id):
    res = herdr("workspace", "get", ws_id, check=False)
    return "error" not in res


def agent_status(name):
    try:
        return herdr("agent", "get", name)["agent"]["agent_status"]
    except HerdrError:
        return None  # agent exited or was replaced


def close_tab(meta):
    """Save the report and close the agent's workspace. The worktree stays on disk."""
    name, path = meta["name"], meta["worktree_path"]
    marker = os.path.dirname(meta["done_path"])
    if os.path.exists(meta["report_path"]):
        shutil.copyfile(meta["report_path"], meta["saved_report"])
        log(f"saved report to {meta['saved_report']}")
    shutil.rmtree(marker, ignore_errors=True)

    try:
        herdr("workspace", "close", meta["workspace_id"])
    except HerdrError as e:
        meta["state"] = f"close_failed:{e.code}"
        save_meta(meta)
        set_label(meta, f"{name} · close failed")
        notify(f"{name}: could not close tab", e.message, "request")
        log(f"workspace close failed: {e}")
        return
    meta["state"] = "tab_closed"
    meta["tab_closed_at"] = now()
    save_meta(meta)
    notify(f"{name}: finished", f"Tab closed. Worktree kept at {path}.")
    log(f"closed tab; worktree kept at {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--max-hours", type=float, default=12.0)
    a = ap.parse_args()

    meta = load_meta(a.run, a.name)
    name, grace = meta["name"], meta["grace"]
    baseline = set(meta.get("baseline_shells", []))
    deadline = time.time() + a.max_hours * 3600
    countdown_end = None
    was_blocked = False
    log(f"watching {name} in {meta['workspace_id']} ({meta['worktree_path']})")

    while True:
        if time.time() > deadline:
            meta["state"] = "watch_timeout"
            save_meta(meta)
            notify(f"{name}: still not finished", "Stopped watching; nothing was removed.",
                   "request")
            log("max watch time reached; leaving everything in place")
            return
        if not workspace_exists(meta["workspace_id"]):
            meta["state"] = "workspace_closed_externally"
            save_meta(meta)
            log("workspace is gone (closed by someone else); stopping")
            return

        status = agent_status(name)
        done = os.path.exists(meta["done_path"])
        busy = command_shells(meta["pane_id"]) - baseline

        if status is None and not done:
            meta["state"] = "agent_exited_unfinished"
            save_meta(meta)
            set_label(meta, f"{name} · exited")
            notify(f"{name}: agent exited", "It never marked its work done. Nothing removed.",
                   "request")
            log("agent exited without the done file; leaving everything in place")
            return

        if status == "blocked" and not was_blocked:
            notify(f"{name} needs input", "The agent is waiting on a dialog.", "request")
            log("agent blocked")
        was_blocked = status == "blocked"

        complete = done and status in ("idle", "done", None) and not busy
        if complete:
            if countdown_end is None:
                countdown_end = time.time() + grace
                notify(f"{name}: work finished", f"Closing its tab in {grace // 60}:{grace % 60:02d}. The worktree stays.")
                log("chain complete; countdown started")
            left = int(countdown_end - time.time())
            if left <= 0:
                close_tab(meta)
                return
            set_label(meta, f"{name} · closing {left // 60}:{left % 60:02d}")
        elif countdown_end is not None:
            countdown_end = None
            set_label(meta, name)
            log(f"countdown reset (done={done} status={status} busy_shells={len(busy)})")

        # Tick every second during the countdown so the tab label counts down smoothly.
        time.sleep(1 if countdown_end is not None else POLL)


if __name__ == "__main__":
    main()
