#!/usr/bin/env python3
"""Print the user's settings for this skill as JSON.

  default_for_agents  true: use this skill whenever agents are started through
                      herdr. false (default): use it only when the user asks for
                      agents in their own worktrees or tabs; otherwise the herdr
                      skill's sibling panes apply.
  yolo                start agents without approval prompts (default false).
  worktree_root       where worktrees go (default: herdr's ~/.herdr/worktrees).

Settings live in ~/.config/herdr-worktree-agents/config.toml. Reading it needs
Python 3.11+; on older Python every setting is at its default.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hwa_common import CONFIG_PATH, load_config  # noqa: E402

try:
    cfg = load_config()
    error = None
except Exception as e:  # malformed TOML: report it, fall back to defaults
    cfg, error = {}, str(e)

out = {
    "default_for_agents": bool(cfg.get("default_for_agents", False)),
    "yolo": bool(cfg.get("yolo", False)),
    "worktree_root": os.environ.get("HWA_WORKTREE_ROOT") or cfg.get("worktree_root"),
    "config_path": CONFIG_PATH,
    "config_exists": os.path.exists(CONFIG_PATH),
}
if error:
    out["config_error"] = error
print(json.dumps(out, indent=2))
