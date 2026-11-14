#!/usr/bin/env python3
"""List or remove the worktrees left behind by a run, after the user agrees.

The watcher closes each agent's tab when it finishes but keeps its worktree, so
nothing on disk is deleted without the user's say-so. Use this script to show
the user what is left, including any work that removal would destroy, then
remove the ones they approve.

  cleanup.py --run <run-id>                      # list (default)
  cleanup.py --run <run-id> --remove             # remove worktrees with nothing to lose
  cleanup.py --run <run-id> --remove --name a b  # only these agents
  cleanup.py ... --remove --discard-changes      # also remove ones holding files

Output is JSON when piped (for agents) and a colored summary in a terminal.
Pass --json or --human to choose explicitly.

What counts as work at risk (removal would delete it):
  * uncommitted files: modified, staged, or untracked,
  * files git ignores, other than caches and dependency folders
    (git deletes these on `worktree remove` without asking).
Commits are never at risk: the branch is kept whenever it has new commits. The
listing still reports commits that are not pushed anywhere, as information.

Rules:
  * Never touches a worktree whose tab is still open.
  * Skips worktrees with work at risk unless --discard-changes is given, which
    the main agent passes only when the user explicitly says to discard it.
  * Deletes the branch only when it has no commits beyond where the agent started.
"""

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hwa_common import MARKER_DIR, git, herdr, now, run_dir, save_meta  # noqa: E402

# Ignored paths that are safe to lose: caches, dependencies, build tooling state.
DISPOSABLE = {
    "node_modules", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", ".gradle", ".next", ".nuxt", ".turbo", ".parcel-cache",
    ".cache", "target", ".terraform", ".DS_Store", ".eggs", ".coverage", "coverage",
    ".idea", ".vscode", MARKER_DIR,
}
DISPOSABLE_SUFFIXES = (".pyc", ".pyo", ".o", ".class", ".egg-info", ".tsbuildinfo")

CODE = {
    "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "go", "rs", "rb", "java", "kt", "swift",
    "c", "h", "cc", "cpp", "hpp", "cs", "php", "sh", "bash", "zsh", "fish", "sql", "tf",
    "lua", "scala", "ex", "exs", "dart", "vue", "svelte", "css", "scss", "dockerfile", "mk",
}
DOCS = {"md", "mdx", "markdown", "txt", "rst", "adoc", "org"}
ARTIFACTS = {
    "html", "htm", "pdf", "png", "jpg", "jpeg", "gif", "svg", "webp", "csv", "tsv", "json",
    "jsonl", "xlsx", "xls", "docx", "pptx", "ipynb", "log", "zip", "tar", "gz", "mp4", "mov",
    "wav", "mp3", "parquet", "sqlite", "db",
}
CONFIG = {"yaml", "yml", "toml", "ini", "cfg", "conf", "env", "lock", "xml"}
KIND_ORDER = ["code", "plan", "doc", "artifact", "config", "other"]
MAX_LISTED = 8


def classify(path):
    base = os.path.basename(path.rstrip("/"))
    lower = base.lower()
    ext = lower.rsplit(".", 1)[-1] if "." in lower else lower
    if ext in DOCS:
        return "plan" if "plan" in lower else "doc"
    if ext in CODE or lower in ("makefile", "dockerfile", "justfile"):
        return "code"
    if ext in ARTIFACTS:
        return "artifact"
    if ext in CONFIG:
        return "config"
    return "other"


def disposable(path):
    parts = path.rstrip("/").split("/")
    return any(p in DISPOSABLE for p in parts) or parts[-1].endswith(DISPOSABLE_SUFFIXES)


def files_at_risk(path):
    """Uncommitted and ignored files that `git worktree remove` would delete."""
    out = git(path, "status", "--porcelain", "--ignored", "-uall", check=False)
    uncommitted, ignored, skipped_ignored = [], [], 0
    for line in out.splitlines():
        code, name = line[:2], line[3:]
        if " -> " in name:  # rename: keep the new name
            name = name.split(" -> ", 1)[1]
        name = name.strip('"')
        if name.startswith(MARKER_DIR + "/"):
            continue
        if code == "!!":
            if disposable(name):
                skipped_ignored += 1
            else:
                ignored.append({"path": name, "kind": classify(name)})
        else:
            uncommitted.append({"path": name, "kind": classify(name), "status": code.strip()})
    return uncommitted, ignored, skipped_ignored


def summarize(files):
    counts = {}
    for f in files:
        counts[f["kind"]] = counts.get(f["kind"], 0) + 1
    return {k: counts[k] for k in KIND_ORDER if k in counts}


def inspect(meta):
    path = meta["worktree_path"]
    info = {
        "name": meta["name"],
        "state": meta.get("state"),
        "worktree_path": path,
        "branch": meta["branch"],
        "saved_report": meta["saved_report"] if os.path.exists(meta["saved_report"]) else None,
        "exists": os.path.isdir(path),
    }
    ws = herdr("workspace", "get", meta["workspace_id"], check=False)
    info["tab_open"] = "error" not in ws

    tip = git(meta["repo"], "rev-parse", "--verify", "--quiet", meta["branch"], check=False)
    info["branch_exists"] = bool(tip)
    info["new_commits"] = []
    if tip:
        log = git(meta["repo"], "log", "--format=%h %s", f"{meta['start_commit']}..{tip}", check=False)
        info["new_commits"] = log.splitlines() if log else []
        pushed = git(meta["repo"], "branch", "-r", "--contains", tip, check=False) if info["new_commits"] else ""
        info["commits_pushed"] = bool(pushed.strip())

    if info["exists"]:
        unc, ign, skipped = files_at_risk(path)
        info["uncommitted"] = unc
        info["ignored"] = ign
        info["disposable_ignored"] = skipped
    else:
        info["uncommitted"], info["ignored"], info["disposable_ignored"] = [], [], 0

    at_risk = info["uncommitted"] + info["ignored"]
    info["at_risk_count"] = len(at_risk)
    info["at_risk_by_kind"] = summarize(at_risk)
    info["has_work_at_risk"] = bool(at_risk)
    return info


def remove(meta, info, discard):
    if not info["exists"]:
        return "already removed"
    if info["tab_open"]:
        return "skipped: its tab is still open"
    if info["has_work_at_risk"] and not discard:
        return f"skipped: {info['at_risk_count']} file(s) would be lost"
    args = ["worktree", "remove"] + (["--force"] if discard else []) + [meta["worktree_path"]]
    git(meta["repo"], *args)
    result = "worktree removed"
    if info["branch_exists"]:
        if not info["new_commits"]:
            git(meta["repo"], "branch", "-D", meta["branch"], check=False)
            result += ", branch deleted (no new commits)"
        else:
            result += f", branch kept ({len(info['new_commits'])} new commit(s))"
    meta["state"] = "worktree_removed"
    meta["worktree_removed_at"] = now()
    save_meta(meta)
    return result


def human(run, agents, color):
    def c(code, text):
        return f"\033[{code}m{text}\033[0m" if color else text

    lines = [c("1", f"Worktrees for run {run}"), ""]
    for a in agents:
        head = f"{a['name']}  {a['worktree_path']}  ({a['branch']})"
        if not a["exists"]:
            lines.append(c("2", f"  ✓ {head}: already removed"))
            continue
        if a["has_work_at_risk"]:
            kinds = ", ".join(f"{n} {k}" for k, n in a["at_risk_by_kind"].items())
            lines.append(c("1;33", f"  ⚠ {head}"))
            lines.append(c("33", f"    Removing it would delete {a['at_risk_count']} file(s): {kinds}"))
            for f in (a["uncommitted"] + a["ignored"])[:MAX_LISTED]:
                tag = "ignored" if f in a["ignored"] else "uncommitted"
                lines.append(c("33", f"      - {f['path']}  [{f['kind']}, {tag}]"))
            if a["at_risk_count"] > MAX_LISTED:
                lines.append(c("33", f"      ... and {a['at_risk_count'] - MAX_LISTED} more"))
        else:
            lines.append(c("32", f"  ✓ {head}: nothing would be lost"))
        if a["new_commits"]:
            where = "pushed" if a.get("commits_pushed") else "not pushed anywhere"
            lines.append(f"    {len(a['new_commits'])} commit(s), {where}; the branch is kept either way")
        if a["tab_open"]:
            lines.append("    tab still open; it will not be removed")
        if "result" in a:
            lines.append(c("1", f"    → {a['result']}"))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True)
    ap.add_argument("--name", nargs="*", help="limit to these agents")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--discard-changes", action="store_true")
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="always print JSON")
    fmt.add_argument("--human", action="store_true", help="always print the readable summary")
    a = ap.parse_args()

    metas = []
    for f in sorted(glob.glob(os.path.join(run_dir(a.run), "*.json"))):
        with open(f) as fh:
            m = json.load(fh)
        if not a.name or m["name"] in a.name:
            metas.append(m)
    if not metas:
        print(json.dumps({"ok": False, "error": f"no agents found for run {a.run}"}))
        sys.exit(1)

    out = []
    for m in metas:
        info = inspect(m)
        if a.remove:
            info["result"] = remove(m, info, a.discard_changes)
        out.append(info)

    tty = sys.stdout.isatty()
    if a.human or (tty and not a.json):
        print(human(a.run, out, color=tty and not os.environ.get("NO_COLOR")))
    else:
        print(json.dumps({"ok": True, "run": a.run, "agents": out}, indent=2))


if __name__ == "__main__":
    main()
