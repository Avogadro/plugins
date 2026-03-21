#!/usr/bin/env python3
"""Parse repositories.toml and provide plugin information for GitHub Actions."""

import argparse
import json
import subprocess
import sys

from plugin_validation import (
    extract_plugins,
    load_toml,
    validate_all_plugins,
)


TOML_PATH = "repositories.toml"


def cmd_list():
    """Print all plugins as a JSON array."""
    data = load_toml(TOML_PATH)
    plugins = extract_plugins(data)
    result = []
    for name, info in plugins.items():
        entry = {"name": name}
        git = info.get("git", {})
        if git:
            entry["repo"] = git.get("repo", "")
            entry["commit"] = git.get("commit", "")
        src = info.get("src", {})
        if src:
            entry["src_url"] = src.get("url", "")
            entry["src_sha256"] = src.get("sha256", "")
        entry["path"] = info.get("path", ".")
        entry["release_tag"] = info.get("release-tag", "")
        entry["plugin_type"] = info.get("plugin-type", "pypkg")
        entry["metadata"] = info.get("metadata", "pyproject.toml")
        result.append(entry)
    print(json.dumps(result, indent=2))


def cmd_check_updates():
    """
    For each git-based plugin, check if the upstream default branch
    has moved past the pinned commit. Output JSON with update info.
    """
    data = load_toml(TOML_PATH)
    plugins = extract_plugins(data)
    updates = []

    for name, info in plugins.items():
        git = info.get("git", {})
        repo_url = git.get("repo", "")
        pinned_commit = git.get("commit", "")

        if not repo_url or not pinned_commit:
            continue

        # Use git ls-remote to get the current HEAD of the default branch
        try:
            result = subprocess.run(
                ["git", "ls-remote", repo_url, "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                print(
                    f"WARNING: Could not reach {repo_url}: {result.stderr.strip()}",
                    file=sys.stderr,
                )
                continue

            lines = result.stdout.strip().split("\n")
            if not lines or not lines[0]:
                continue

            remote_head = lines[0].split()[0]

            if remote_head != pinned_commit:
                updates.append(
                    {
                        "name": name,
                        "repo": repo_url,
                        "pinned_commit": pinned_commit,
                        "latest_commit": remote_head,
                        "release_tag": info.get("release-tag", ""),
                    }
                )
        except subprocess.TimeoutExpired:
            print(f"WARNING: Timeout reaching {repo_url}", file=sys.stderr)
        except Exception as e:
            print(f"WARNING: Error checking {repo_url}: {e}", file=sys.stderr)

    print(json.dumps(updates, indent=2))


def cmd_diff(base_path: str, head_path: str):
    """
    Compare two versions of repositories.toml.
    Output JSON describing added, removed, and modified plugins.
    """
    base_data = extract_plugins(load_toml(base_path))
    head_data = extract_plugins(load_toml(head_path))

    base_names = set(base_data.keys())
    head_names = set(head_data.keys())

    added = []
    removed = []
    modified = []

    for name in head_names - base_names:
        info = head_data[name]
        git = info.get("git", {})
        added.append(
            {
                "name": name,
                "repo": git.get("repo", ""),
                "commit": git.get("commit", ""),
                "path": info.get("path", "."),
                "plugin_type": info.get("plugin-type", "pypkg"),
            }
        )

    for name in base_names - head_names:
        removed.append({"name": name})

    for name in base_names & head_names:
        if base_data[name] != head_data[name]:
            old_git = base_data[name].get("git", {})
            new_git = head_data[name].get("git", {})
            modified.append(
                {
                    "name": name,
                    "repo": new_git.get("repo", old_git.get("repo", "")),
                    "old_commit": old_git.get("commit", ""),
                    "new_commit": new_git.get("commit", ""),
                    "path": head_data[name].get("path", "."),
                    "plugin_type": head_data[name].get("plugin-type", "pypkg"),
                    "changed_fields": [
                        k
                        for k in set(
                            list(base_data[name].keys()) + list(head_data[name].keys())
                        )
                        if base_data[name].get(k) != head_data[name].get(k)
                    ],
                }
            )

    result = {
        "added": added,
        "removed": removed,
        "modified": modified,
        "total_changes": len(added) + len(removed) + len(modified),
    }
    print(json.dumps(result, indent=2))

def main():
    parser = argparse.ArgumentParser(
        description="Parse repositories.toml and provide plugin information "
        "for GitHub Actions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "check-updates",
        help="Check for upstream updates (new commits on default branch)",
    )

    diff_parser = subparsers.add_parser(
        "diff", help="Diff two versions of repositories.toml to find changes"
    )
    diff_parser.add_argument("base_file", help="Base repositories.toml file")
    diff_parser.add_argument("head_file", help="Head repositories.toml file")

    args = parser.parse_args()

    if args.command == "check-updates":
        cmd_check_updates()
    elif args.command == "diff":
        cmd_diff(args.base_file, args.head_file)


if __name__ == "__main__":
    main()
