---
name: herdr-worktree-agents
description: "Default way to start coding agents (Claude, Codex, or any herdr agent kind) through herdr. Each agent gets its own git worktree, opened as its own herdr workspace and tab, and a watcher closes the tab 2 minutes after the agent's whole chain of work (including subagents and background tasks) is finished. Worktrees are kept until the user agrees to remove them. Use whenever the user asks to run, spawn, or start agents in herdr, or asks for something like 'review this PR with Sonnet and Codex' while running inside herdr. This replaces the herdr skill's sibling-pane default for starting agents; keep using the herdr skill for everything else. Requires HERDR_ENV=1."
---

# Agents in their own herdr worktrees

Use this instead of splitting panes whenever you start agents through herdr.
Each agent gets:

- its own branch and git worktree, so agents never share files with each other or with the user's checkout,
- its own herdr workspace with one tab, labeled with the agent's name,
- a detached watcher that closes its tab when the agent's whole chain of work is done, like Orca's worker tabs.

Worktrees are never deleted automatically. At the end, you ask the user whether to remove them (step 8).

Read the `herdr` skill too (`npx skills add herdrdev/herdr`). It explains agent states, `send-keys`, and the safety rules, and those rules still apply.

The scripts live in this skill's `scripts/` folder. Below, `<skill-dir>` means the folder this SKILL.md is in.

## Steps

1. **Check you are inside herdr:** `test "${HERDR_ENV:-}" = 1`. If not, say so and stop.

2. **Pick a run id and agent names.** The run id groups agents started together, e.g. `pr42-1305`. Names must match `[a-z][a-z0-9_-]{0,31}` and be unique among live agents, e.g. `claude-review`, `codex-review`.

3. **Write each agent's task to a file** in your scratchpad. Write only the task. The script appends a completion contract that tells the agent where to put its report and when to mark itself done. For reviews, tell the agent not to push, comment on GitHub, or change files unless the user asked for that.

4. **Start each agent** (independent agents can start in parallel):

   ```bash
   python3 -I <skill-dir>/scripts/spawn.py \
     --run <run-id> --name <name> --kind <claude|codex|...> \
     --repo <path to the main checkout> --prompt-file <task file> \
     [--pr <number> [--remote origin] | --base <ref>] [--branch <name>] \
     [--grace 120] [--yolo | --no-yolo] \
     -- <native agent args>
   ```

   - `--pr N` fetches GitHub's `pull/N/head` ref from `--remote` (default `origin`) and branches from it. It does not work for GitLab or other hosts; use `--base` there. `--base` takes any ref. The default is the repo's current `HEAD`.
   - The branch defaults to `herdr/<run>-<name>`. Two worktrees can't check out the same branch, so every agent gets its own.
   - **YOLO mode is off by default.** With `--yolo`, or `yolo = true` in `~/.config/herdr-worktree-agents/config.toml`, Codex starts with `--dangerously-bypass-approvals-and-sandbox` and Claude with `--dangerously-skip-permissions`, so they never stop to ask before running commands. `--no-yolo` overrides the config for one run. Use YOLO only when the user asked for it or has it in their config. When it is on, nothing stops the agents, so state limits such as "do not push or comment on GitHub" plainly in the task. Without YOLO, agents will often stop on approval dialogs; the watcher notifies the user, and you handle them as in step 5.
   - Model flags go after `--`. Claude: `-- --model sonnet`. Codex: `-- -m <model>`.
   - It prints JSON with `workspace_id`, `tab_id`, `pane_id`, `worktree_path`, `report_path`, `done_path`, and `saved_report`.

5. **If spawn exits with code 3**, the agent is stuck on a startup dialog. The usual ones are a folder-trust prompt for a repo that agent hasn't trusted yet, and Claude's one-time warning for its permission-skipping mode under `--yolo`. Both Claude and Codex save folder trust at the main repo root, so a trust prompt normally appears only once per repo. Show the user the `screen` text and ask what to do. Never answer it yourself. After they decide, send the keys with `herdr agent send-keys <name> ...`, then rerun:

   ```bash
   python3 -I <skill-dir>/scripts/spawn.py --run <run-id> --name <name> --continue
   ```

   **If spawn exits with code 1 and `"state": "start_failed"`**, the agent never started. The script already retried once, closed the empty tab, and removed the unused worktree, so rerun the same spawn command once. If it fails again, tell the user the `error` text. Don't use `--continue` for this; it only resumes an agent blocked on a dialog.

6. **Wait for results** without polling in the foreground. Run this with Bash `run_in_background`. It exits once every agent has created its done file or left the `running` state:

   ```bash
   R=~/.local/state/herdr-agents/runs/<run>
   pending() { for n in "$@"; do f=$R/$n.json
     [ "$(jq -r .state $f)" = running ] && [ ! -e "$(jq -r .done_path $f)" ] && return 0; done; return 1; }
   while pending <name1> <name2>; do sleep 5; done
   ```

   Read each report from `report_path` right away. Once the tab has closed, read `saved_report`; the watcher copies the report there before closing the tab.

7. **Synthesize** the agents' findings for the user. Verify any claim that the recommendation depends on.

8. **Ask the user about the worktrees.** Once the tabs have closed, list what's left:

   ```bash
   python3 -I <skill-dir>/scripts/cleanup.py --run <run-id> --json
   ```

   For each worktree it reports the path, the branch, new commits (and whether they're pushed anywhere), and the files that removal would delete: uncommitted files plus files git ignores, other than caches and dependency folders. Git deletes ignored files on `worktree remove` without asking, so they count. Each file is labeled `code`, `plan`, `doc`, `artifact`, `config`, or `other`.

   Put the question at the end of the same message as your findings. **Every worktree with `has_work_at_risk: true` gets a warning callout before the question**, so the user can't miss it. Terminal chat can't set colors, so use the strongest Markdown signals available: a blockquote, a warning emoji, and bold. Use this shape:

   > ⚠️ **`codex-review` has 3 files that removal would delete**
   > - `docs/migration-plan.md`: plan, uncommitted
   > - `src/retry.py`: code, uncommitted
   > - `out/report.html`: artifact, ignored by git
   >
   > Its 2 commits are safe either way; the branch `herdr/pr42-codex-review` is kept.

   List up to 8 files per worktree, then "and N more." Mention commits only as reassurance; they are never deleted. Worktrees with nothing at risk go in one plain line, for example "`claude-review`: nothing would be lost."

   Then ask one question with clear choices, such as: remove only the ones with nothing at risk, remove all including the flagged files, or keep everything. If your harness has a question tool, repeat the warning in the question text, because the dialog may hide your earlier message.

   Only after the user answers:

   ```bash
   python3 -I <skill-dir>/scripts/cleanup.py --run <run-id> --remove --json [--name <agent> ...]
   ```

   Without `--discard-changes`, it skips worktrees with files at risk and ones whose tab is still open. Add `--discard-changes` only for worktrees the user explicitly agreed to lose files from, and pass their names with `--name`. Branches with new commits are always kept. If the user says no, leave everything; they can run the same command later. Run in a terminal without `--json`, the script prints the same summary with the warnings in color.

## How tab closing works

The watcher (`scripts/watch.py`, one per agent, detached) treats the chain as finished only while all three hold:

1. The agent created its done file. The contract tells it to do this only after every subagent, background agent, and background command has finished.
2. herdr reports the agent as `idle` or `done`.
3. No command shells (`zsh -c`, `bash -lc`, ...) are running under the agent, other than ones that existed before it got its task. This catches background tasks while herdr already shows `idle`.

When all three first hold, it notifies the user and starts the countdown (default 120 s, `--grace`), showing it in the tab label (`name · closing 1:45`). Any change resets the countdown. When the countdown ends, the watcher:

- copies the report to `~/.local/state/herdr-agents/runs/<run>/<name>.md`,
- closes the agent's workspace and tab, which ends its terminal session,
- keeps the worktree and branch on disk, including any uncommitted changes,
- notifies the user.

It leaves the tab open and notifies the user when:

- the agent exits without marking itself done (`name · exited`),
- 12 hours pass without the chain finishing.

It also notifies the user once each time the agent gets blocked on a dialog, and stops watching if someone else closes the workspace.

## Checking on or stopping a run

- State: `jq .state ~/.local/state/herdr-agents/runs/<run>/<name>.json`. Values: `running`, `tab_closed`, `worktree_removed`, `agent_exited_unfinished`, `workspace_closed_externally`, `watch_timeout`, `close_failed:<code>`, `start_failed`.
- Watcher log: `~/.local/state/herdr-agents/runs/<run>/<name>.log`.
- To keep a tab open, kill its watcher (`watcher_pid` in the JSON).
- To talk to a running agent, use the herdr skill's `agent prompt`, `agent read`, and `agent wait` with its name.

## Rules

- Never remove a worktree without the user's yes. Never pass `--discard-changes` (or `--force` to any remove command) unless the user explicitly agreed to lose that worktree's flagged files, after seeing the warning.
- Never answer an agent's approval, trust, or question dialog without the user's decision.
- Only clean up workspaces and worktrees this skill created (the run's JSON files list them).
- Creating the first worktree for a repo can make herdr open a workspace for the main checkout too. Leave it; it's the user's.
- The script adds `/.herdr-agent/` to the repo's `.git/info/exclude` so the report and done files never show up in `git status` or get committed.
