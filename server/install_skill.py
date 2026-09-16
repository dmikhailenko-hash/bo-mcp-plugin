#!/usr/bin/env python3
"""Install the bundled skill so Claude Code can find it.

Claude Code discovers skills in ~/.claude/skills/ and <project>/.claude/skills/.
A skill inside a repository is only picked up automatically when the repository
is installed through the plugin system, which is not available everywhere - so
this script copies it into place.

    python server/install_skill.py              install for every project
    python server/install_skill.py --project .  install for one project only
    python server/install_skill.py --uninstall  remove it again

Re-run it after pulling a new version of the repository.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys

# A colleague's console may be on cp866, cp1251 or cp437, none of which can
# represent every character. Degrade to '?' rather than raising.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except (AttributeError, OSError):  # pragma: no cover - Python < 3.7, or no console
    pass

SKILL_NAME = "traderevolution-bo"
PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(PLUGIN_ROOT, "skills", SKILL_NAME)


def target_dir(project: str | None) -> str:
    base = os.path.abspath(project) if project else os.path.expanduser("~")
    return os.path.join(base, ".claude", "skills", SKILL_NAME)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", help="install into this project instead of your home directory")
    parser.add_argument("--uninstall", action="store_true", help="remove an installed copy")
    args = parser.parse_args()

    destination = target_dir(args.project)

    if args.uninstall:
        if os.path.isdir(destination):
            shutil.rmtree(destination)
            print(f"Removed {destination}")
        else:
            print(f"Nothing to remove at {destination}")
        print("Restart Claude Code for the change to take effect.")
        return 0

    if not os.path.isdir(SOURCE):
        print(f"Skill not found at {SOURCE}", file=sys.stderr)
        return 1

    existing = os.path.isdir(destination)
    if existing:
        source_file = os.path.join(SOURCE, "SKILL.md")
        target_file = os.path.join(destination, "SKILL.md")
        if os.path.isfile(target_file) and filecmp.cmp(source_file, target_file, shallow=False):
            print(f"Already up to date: {destination}")
            return 0
        shutil.rmtree(destination)

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.copytree(SOURCE, destination)

    print(f"{'Updated' if existing else 'Installed'}: {destination}")
    print("Restart Claude Code, then ask it about a Back-Office report.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
