#!/usr/bin/env python3
"""Shared validation logic and utilities for plugin repository management.

Used by both generate_index.py and parse_plugins.py.
"""

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


# Valid plugin types
PLUGIN_TYPES = [
    "pyscript",
    "pypkg",
]

# Valid plugin feature types
FEATURE_TYPES = [
    "electrostatic-models",
    "energy-models",
    "file-formats",
    "input-generators",
    "menu-commands",
]

# Valid metadata file names
METADATA_FILES = ["pyproject.toml", "avogadro.toml"]

# Default values for optional keys in repositories.toml
REPO_DEFAULTS = {
    "metadata": "pyproject.toml",
    "plugin-type": "pypkg",
}

# Keys that are file-level guidance comments, not plugin entries
NON_PLUGIN_KEYS: set[str] = set()


def load_toml(path: str) -> dict:
    """Load and parse a TOML file."""
    with open(path, "rb") as f:
        return tomllib.load(f)


def extract_plugins(data: dict) -> dict:
    """Return only the plugin tables from parsed TOML data."""
    plugins = {}
    for key, value in data.items():
        if isinstance(value, dict) and key not in NON_PLUGIN_KEYS:
            plugins[key] = value
    return plugins


def set_defaults(repo_info: dict):
    """Set default values for optional keys in a repo_info dict.

    Modifies the dict in place.
    """
    for k, v in REPO_DEFAULTS.items():
        repo_info.setdefault(k, v)


class ValidationResult:
    """Collects errors and warnings from validation."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def valid(self) -> bool:
        return len(self.errors) == 0

    def error(self, msg: str):
        self.errors.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)

    def raise_on_errors(self):
        """Raise an AssertionError if there are any errors.

        Useful for callers that want exception-based validation.
        """
        if not self.valid:
            raise AssertionError(
                "Validation failed:\n" + "\n".join(f"  - {e}" for e in self.errors)
            )


def validate_repo_info(name: str, info: dict) -> ValidationResult:
    """Validate a single plugin entry from repositories.toml.

    Args:
        name: The plugin table name (key in TOML).
        info: The plugin's dict of values.

    Returns:
        A ValidationResult with any errors and warnings.
    """
    result = ValidationResult()
    prefix = f"[{name}]"

    git = info.get("git", {})
    src = info.get("src", {})

    # Must have exactly one of git or src
    has_git = bool(git)
    has_src = bool(src)
    if not has_git and not has_src:
        result.error(f"{prefix}: Must have either 'git' or 'src' section")
    elif has_git and has_src:
        result.error(f"{prefix}: Cannot have both 'git' and 'src' sections")

    if has_git:
        if not git.get("repo"):
            result.error(f"{prefix}: Missing git.repo")
        elif not git["repo"].endswith(".git"):
            result.warn(f"{prefix}: git.repo should end with '.git'")

        commit = git.get("commit", "")
        if not commit:
            result.error(f"{prefix}: Missing git.commit")
        elif len(commit) != 40:
            result.error(
                f"{prefix}: git.commit should be a full 40-char SHA, "
                f"got {len(commit)} chars"
            )

    if has_src:
        if not src.get("url"):
            result.error(f"{prefix}: Missing src.url")
        if not src.get("sha256"):
            result.error(f"{prefix}: Missing src.sha256")

    # Validate plugin-type
    plugin_type = info.get("plugin-type", REPO_DEFAULTS["plugin-type"])
    if plugin_type not in PLUGIN_TYPES:
        result.error(
            f"{prefix}: plugin-type must be one of {PLUGIN_TYPES}, "
            f"got '{plugin_type}'"
        )

    # Validate metadata file
    metadata = info.get("metadata", REPO_DEFAULTS["metadata"])
    if metadata not in METADATA_FILES:
        result.error(
            f"{prefix}: metadata must be one of {METADATA_FILES}, "
            f"got '{metadata}'"
        )

    # Validate path
    path_val = info.get("path", ".")
    if "\\" in path_val:
        result.error(f"{prefix}: path should use '/' separators, not '\\'")
    if path_val != "." and path_val.endswith("/"):
        result.error(f"{prefix}: path should not end with '/'")
    final_component = path_val.split("/")
    if "." in final_component and path_val != ".":
        result.error(f"{prefix}: path components should not be '.'")

    return result


def validate_all_plugins(data: dict) -> ValidationResult:
    """Validate all plugin entries in parsed TOML data.

    Args:
        data: The full parsed TOML dict.

    Returns:
        A combined ValidationResult for all plugins.
    """
    combined = ValidationResult()
    plugins = extract_plugins(data)

    for name, info in plugins.items():
        plugin_result = validate_repo_info(name, info)
        combined.errors.extend(plugin_result.errors)
        combined.warnings.extend(plugin_result.warnings)

    return combined
