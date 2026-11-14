"""Shared helpers for the herdr-worktree-agents scripts."""

import json
import os
import subprocess
import time

STATE_ROOT = os.path.join(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
    "herdr-agents",
    "runs",
)
MARKER_DIR = ".herdr-agent"  # inside each worktree; git-excluded


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def herdr(*args, check=True):
    """Run a herdr CLI command and return its parsed JSON result."""
    proc = subprocess.run(["herdr", *args], capture_output=True, text=True)
    out = proc.stdout.strip() or proc.stderr.strip()
    try:
        data = json.loads(out) if out else {}
    except json.JSONDecodeError:
        if check and proc.returncode != 0:
            raise HerdrError("cli_error", out)
        return {"raw": out}
    if "error" in data:
        if check:
            raise HerdrError(data["error"].get("code"), data["error"].get("message"))
        return data
    return data.get("result", data)


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def run_dir(run_id):
    path = os.path.join(STATE_ROOT, run_id)
    os.makedirs(path, exist_ok=True)
    return path


def meta_path(run_id, name):
    return os.path.join(run_dir(run_id), f"{name}.json")


def load_meta(run_id, name):
    with open(meta_path(run_id, name)) as f:
        return json.load(f)


def save_meta(meta):
    tmp = meta_path(meta["run_id"], meta["name"]) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(meta, f, indent=2)
    os.replace(tmp, meta_path(meta["run_id"], meta["name"]))


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")
