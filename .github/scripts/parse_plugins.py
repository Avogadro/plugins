#!/usr/bin/env python3
"""Parse repositories.toml and provide plugin information for GitHub Actions."""

import argparse
import json
import subprocess
import sys

# Python 3.11+ has tomllib in stdlib; fall back to tomli
try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        print("ERROR: Need Python 3.11+ or 'pip install tomli'", file=sys.stderr)
        sys.exit(1)


TOML_PATH = "repositories.toml"

# Keys that are file-level guidance comments, not plugin entries
NON_PLUGIN_KEYS: set[str] = set()


def load_toml(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def extract_plugins(data: dict) -> dict:
    """Return only the plugin tables from the parsed TOML."""
    plugins = {}
    for key, value in data.items():
        if isinstance(value, dict) and key not in NON_PLUGIN_KEYS:
            plugins[key] = value
    return plugins


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


def cmd_validate(path: str):
    """Validate the structure of a repositories.toml file."""
    errors = []
    warnings = []

    try:
        data = load_toml(path)
    except Exception as e:
        errors.append(f"Failed to parse TOML: {e}")
        print(json.dumps({"valid": False, "errors": errors, "warnings": warnings}))
        return

    plugins = extract_plugins(data)

    for name, info in plugins.items():
        prefix = f"[{name}]"
        git = info.get("git", {})
        src = info.get("src", {})

        # Must have exactly one of git or src
        has_git = bool(git)
        has_src = bool(src)
        if not has_git and not has_src:
            errors.append(f"{prefix}: Must have either 'git' or 'src' section")
        elif has_git and has_src:
            errors.append(f"{prefix}: Cannot have both 'git' and 'src' sections")

        if has_git:
            if not git.get("repo"):
                errors.append(f"{prefix}: Missing git.repo")
            elif not git["repo"].endswith(".git"):
                warnings.append(f"{prefix}: git.repo should end with '.git'")

            commit = git.get("commit", "")
            if not commit:
                errors.append(f"{prefix}: Missing git.commit")
            elif len(commit) != 40:
                errors.append(
                    f"{prefix}: git.commit should be a full 40-char SHA, "
                    f"got {len(commit)} chars"
                )

        if has_src:
            if not src.get("url"):
                errors.append(f"{prefix}: Missing src.url")
            if not src.get("sha256"):
                errors.append(f"{prefix}: Missing src.sha256")

        # Validate optional fields
        plugin_type = info.get("plugin-type", "pypkg")
        if plugin_type not in ("pypkg", "pyscript"):
            errors.append(
                f"{prefix}: plugin-type must be 'pypkg' or 'pyscript', "
                f"got '{plugin_type}'"
            )

        metadata = info.get("metadata", "pyproject.toml")
        if metadata not in ("pyproject.toml", "avogadro.toml"):
            errors.append(
                f"{prefix}: metadata must be 'pyproject.toml' or 'avogadro.toml', "
                f"got '{metadata}'"
            )

        path_val = info.get("path", ".")
        if "\\" in path_val:
            errors.append(f"{prefix}: path should use '/' separators, not '\\'")

    result = {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "plugin_count": len(plugins),
    }
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Parse repositories.toml and provide plugin information "
        "for GitHub Actions."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List all plugins as JSON")

    subparsers.add_parser(
        "check-updates",
        help="Check for upstream updates (new commits on default branch)",
    )

    diff_parser = subparsers.add_parser(
        "diff", help="Diff two versions of repositories.toml to find changes"
    )
    diff_parser.add_argument("base_file", help="Base repositories.toml file")
    diff_parser.add_argument("head_file", help="Head repositories.toml file")

    validate_parser = subparsers.add_parser(
        "validate", help="Validate the TOML structure"
    )
    validate_parser.add_argument(
        "file", nargs="?", default=TOML_PATH, help="Path to TOML file to validate"
    )

    args = parser.parse_args()

    if args.command == "list":
        cmd_list()
    elif args.command == "check-updates":
        cmd_check_updates()
    elif args.command == "diff":
        cmd_diff(args.base_file, args.head_file)
    elif args.command == "validate":
        cmd_validate(args.file)


if __name__ == "__main__":
    main()
