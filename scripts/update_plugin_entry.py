#!/usr/bin/env python3
"""Update a single plugin section in repositories.toml."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


def _find_section_bounds(lines: list[str], plugin_name: str) -> tuple[int, int]:
    header = f"[{plugin_name}]"
    start = None

    for idx, line in enumerate(lines):
        if line.strip() == header:
            start = idx
            break

    if start is None:
        raise ValueError(f"Plugin section [{plugin_name}] not found")

    end = len(lines)
    for idx in range(start + 1, len(lines)):
        stripped = lines[idx].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = idx
            break

    return start, end


def update_plugin_entry(
    file_path: Path,
    plugin_name: str,
    old_commit: str,
    new_commit: str,
    latest_tag: str | None,
) -> None:
    lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = _find_section_bounds(lines, plugin_name)
    section = lines[start:end]

    commit_pattern = re.compile(r'^(\s*git\.commit\s*=\s*")([0-9a-f]{40})(".*)$')
    commit_index = None

    for idx, line in enumerate(section):
        match = commit_pattern.match(line)
        if not match:
            continue
        if match.group(2) != old_commit:
            raise ValueError(
                f"Plugin [{plugin_name}] commit did not match expected value {old_commit}"
            )
        section[idx] = f'{match.group(1)}{new_commit}{match.group(3)}\n'
        commit_index = idx
        break

    if commit_index is None:
        raise ValueError(f"Plugin [{plugin_name}] does not contain git.commit")

    release_pattern = re.compile(r'^(\s*release-tag\s*=\s*")([^"]*)(".*)$')
    release_index = None
    for idx, line in enumerate(section):
        if release_pattern.match(line):
            release_index = idx
            break

    if latest_tag:
        new_release_line = f'release-tag = "{latest_tag}"\n'
        if release_index is not None:
            section[release_index] = new_release_line
        else:
            insert_at = commit_index + 1
            if insert_at < len(section) and section[insert_at].strip():
                section.insert(insert_at, new_release_line)
            else:
                section.insert(insert_at, new_release_line)
    elif release_index is not None:
        del section[release_index]

    lines[start:end] = section
    file_path.write_text("".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Update a single plugin entry.")
    parser.add_argument("file_path", help="Path to repositories.toml")
    parser.add_argument("plugin_name", help="Plugin table name")
    parser.add_argument("old_commit", help="Expected current commit SHA")
    parser.add_argument("new_commit", help="New commit SHA")
    parser.add_argument(
        "--latest-tag",
        default="",
        help="Optional release tag to set; omit or pass empty string to remove it",
    )
    args = parser.parse_args()

    update_plugin_entry(
        Path(args.file_path),
        args.plugin_name,
        args.old_commit,
        args.new_commit,
        args.latest_tag or None,
    )


if __name__ == "__main__":
    main()
