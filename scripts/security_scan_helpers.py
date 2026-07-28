#!/usr/bin/env python3
"""Trusted helpers for the security scan workflow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - fallback for older local Python
    import tomli as tomllib


DIRECT_URL_RE = re.compile(r"(?:^|\s)(?:-e\s+)?(?:git\+|https?://|file://)|\s@\s(?:git\+|https?://|file://)")


def _append_requirement(requirements: list[str], value: str | None) -> None:
    if not value:
        return

    normalized = value.strip()
    if not normalized:
        return

    requirements.append(normalized)


def _load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def _is_non_pypi(value: str) -> bool:
    return bool(DIRECT_URL_RE.search(value))


# PEP 440 comparison operators a constraint may already use.
_PEP440_OP_RE = re.compile(r"^(===|==|!=|>=|<=|~=|>|<)")
# A plain numeric release like 1, 1.2, or 1.2.3 (needed for ^/~ expansion).
_NUMERIC_RELEASE_RE = re.compile(r"^\d+(\.\d+)*$")
_UNSUPPORTED_POETRY = "manual-review:unsupported-poetry-constraint"


def _parse_release(version: str) -> list[int] | None:
    if not _NUMERIC_RELEASE_RE.match(version):
        return None
    return [int(part) for part in version.split(".")]


def _caret_to_pep440(version: str) -> tuple[str | None, str | None]:
    # Caret allows changes that don't modify the left-most non-zero component.
    comps = _parse_release(version)
    if not comps:
        return None, _UNSUPPORTED_POETRY
    lower = (comps + [0, 0, 0])[:3]
    upper = list(comps)
    idx = next((i for i, c in enumerate(comps) if c != 0), None)
    if idx is None:
        upper[-1] += 1  # all zeros (e.g. ^0, ^0.0): bump the last component
    else:
        upper[idx] += 1
        for k in range(idx + 1, len(upper)):
            upper[k] = 0
    upper = (upper + [0, 0, 0])[:3]
    lo = ".".join(str(c) for c in lower)
    hi = ".".join(str(c) for c in upper)
    return f">={lo},<{hi}", None


def _tilde_to_pep440(version: str) -> tuple[str | None, str | None]:
    # Tilde bumps the minor when a minor/patch is given, else the major.
    comps = _parse_release(version)
    if not comps:
        return None, _UNSUPPORTED_POETRY
    lower = (comps + [0, 0, 0])[:3]
    if len(comps) >= 2:
        upper = [comps[0], comps[1] + 1, 0]
    else:
        upper = [comps[0] + 1, 0, 0]
    lo = ".".join(str(c) for c in lower)
    hi = ".".join(str(c) for c in upper)
    return f">={lo},<{hi}", None


def _convert_constraint(part: str) -> tuple[str | None, str | None]:
    part = part.strip()
    if not part or part == "*":
        return "", None
    if _PEP440_OP_RE.match(part):
        return part, None  # already a valid PEP 440 constraint (incl. ~=)
    if part.startswith("^"):
        return _caret_to_pep440(part[1:])
    if part.startswith("~"):  # bare tilde; ~= was matched above
        return _tilde_to_pep440(part[1:])
    if part.endswith(".*"):
        return f"=={part}", None  # PEP 440 prefix match, e.g. ==1.2.*
    if part[:1].isdigit():
        return f"=={part}", None  # a bare version means exact in Poetry
    return None, _UNSUPPORTED_POETRY


def _poetry_version_to_pep440(spec: str) -> tuple[str | None, str | None]:
    """Convert a Poetry version constraint to a PEP 440 specifier.

    Returns (specifier, None) on success (specifier may be "" for "any"), or
    (None, reason) when the constraint can't be normalized, so the caller routes
    it to manual review instead of emitting an unparseable pip requirement.
    """
    text = spec.strip()
    if text in {"", "*"}:
        return "", None
    if "||" in text:  # Poetry OR groups don't map to a single specifier
        return None, _UNSUPPORTED_POETRY
    converted: list[str] = []
    for part in text.split(","):
        piece, reason = _convert_constraint(part)
        if reason:
            return None, reason
        if piece:
            converted.append(piece)
    return ",".join(converted), None


def _format_poetry_dependency(name: str, spec) -> tuple[str | None, str | None]:
    if name == "python":
        return None, None

    if isinstance(spec, str):
        if _is_non_pypi(spec):
            return None, "direct URL dependency"
        constraint, reason = _poetry_version_to_pep440(spec)
        if reason:
            return None, reason
        return f"{name}{constraint}", None

    if not isinstance(spec, dict):
        return None, "unsupported dependency format"

    if "git" in spec:
        return None, "git dependency"
    if "path" in spec:
        return None, "path dependency"
    if "url" in spec:
        return None, "url dependency"
    if "source" in spec:
        return None, f"custom source {spec['source']}"

    extras = ""
    if spec.get("extras"):
        extras = "[" + ",".join(spec["extras"]) + "]"

    version = spec.get("version", "")
    requirement = f"{name}{extras}"
    if version not in {"", "*"}:
        constraint, reason = _poetry_version_to_pep440(version)
        if reason:
            return None, reason
        requirement += constraint
    if spec.get("markers"):
        requirement += f"; {spec['markers']}"
    return requirement, None


def _collect_requirements_file(
    path: Path,
    source_label: str,
    requirements: list[str],
    non_pypi: list[dict[str, str]],
    visited: set[Path],
) -> None:
    resolved = path.resolve()
    if resolved in visited or not path.exists():
        return

    visited.add(resolved)
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if " #" in line:
            line = line.split(" #", 1)[0].strip()
        if not line:
            continue

        if line.startswith(("-r ", "--requirement ")):
            _, include_target = line.split(maxsplit=1)
            _collect_requirements_file(
                path.parent / include_target,
                str(path.parent / include_target),
                requirements,
                non_pypi,
                visited,
            )
            continue

        if line.startswith(("-c ", "--constraint ", "--index-url", "--extra-index-url", "--find-links")):
            non_pypi.append(
                {
                    "source": source_label,
                    "dependency": line,
                    "reason": "custom package index or finder directive",
                }
            )
            continue

        if _is_non_pypi(line):
            non_pypi.append(
                {
                    "source": source_label,
                    "dependency": line,
                    "reason": "direct URL dependency",
                }
            )
            continue

        _append_requirement(requirements, line)


def extract_dependencies(src_dir: Path, requirements_out: Path) -> dict:
    dependency_files: list[str] = []
    dynamic_sources: list[dict[str, str]] = []
    non_pypi: list[dict[str, str]] = []
    requirements: list[str] = []
    visited: set[Path] = set()

    requirements_txt = src_dir / "requirements.txt"
    if requirements_txt.exists():
        dependency_files.append(str(requirements_txt))
        _collect_requirements_file(
            requirements_txt,
            str(requirements_txt),
            requirements,
            non_pypi,
            visited,
        )

    pyproject = src_dir / "pyproject.toml"
    if pyproject.exists():
        dependency_files.append(str(pyproject))
        data = _load_toml(pyproject)

        project = data.get("project", {})
        for dep in project.get("dependencies", []):
            if _is_non_pypi(dep):
                non_pypi.append(
                    {
                        "source": str(pyproject),
                        "dependency": dep,
                        "reason": "direct URL dependency",
                    }
                )
            else:
                _append_requirement(requirements, dep)

        for group_name, deps in project.get("optional-dependencies", {}).items():
            for dep in deps:
                if _is_non_pypi(dep):
                    non_pypi.append(
                        {
                            "source": str(pyproject),
                            "dependency": dep,
                            "reason": f"direct URL dependency in optional group {group_name}",
                        }
                    )
                else:
                    _append_requirement(requirements, dep)

        dynamic_fields = set(project.get("dynamic", []))
        if "dependencies" in dynamic_fields or "optional-dependencies" in dynamic_fields:
            dynamic_sources.append(
                {
                    "file": str(pyproject),
                    "reason": "project dependencies are declared dynamically and were not executed",
                }
            )

        setuptools_dynamic = data.get("tool", {}).get("setuptools", {}).get("dynamic", {})
        if "dependencies" in setuptools_dynamic or "optional-dependencies" in setuptools_dynamic:
            dynamic_sources.append(
                {
                    "file": str(pyproject),
                    "reason": "setuptools dynamic dependencies were not executed",
                }
            )

        poetry = data.get("tool", {}).get("poetry", {})
        poetry_dependencies = poetry.get("dependencies", {})
        for name, spec in poetry_dependencies.items():
            requirement, reason = _format_poetry_dependency(name, spec)
            if reason:
                non_pypi.append(
                    {
                        "source": str(pyproject),
                        "dependency": name,
                        "reason": reason,
                    }
                )
            elif requirement:
                # (None, None) means "skip" (e.g. the python constraint).
                _append_requirement(requirements, requirement)

        poetry_groups = poetry.get("group", {})
        for group_name, group_data in poetry_groups.items():
            for name, spec in group_data.get("dependencies", {}).items():
                requirement, reason = _format_poetry_dependency(name, spec)
                if reason:
                    non_pypi.append(
                        {
                            "source": str(pyproject),
                            "dependency": name,
                            "reason": f"{reason} in Poetry group {group_name}",
                        }
                    )
                elif requirement:
                    _append_requirement(requirements, requirement)

        for group_name, deps in data.get("dependency-groups", {}).items():
            for dep in deps:
                # Entries may be include-group tables (dicts) or, in malformed
                # files, other non-string values; only string specs are auditable.
                if not isinstance(dep, str):
                    dynamic_sources.append(
                        {
                            "file": str(pyproject),
                            "reason": f"dependency group {group_name} contains a non-string item and was not expanded",
                        }
                    )
                    continue
                if _is_non_pypi(dep):
                    non_pypi.append(
                        {
                            "source": str(pyproject),
                            "dependency": dep,
                            "reason": f"direct URL dependency in dependency group {group_name}",
                        }
                    )
                else:
                    _append_requirement(requirements, dep)

    setup_py = src_dir / "setup.py"
    if setup_py.exists():
        dependency_files.append(str(setup_py))
        dynamic_sources.append(
            {
                "file": str(setup_py),
                "reason": "setup.py dependencies were not executed",
            }
        )

    deduped_requirements = list(dict.fromkeys(requirements))
    requirements_out.write_text(
        "".join(f"{requirement}\n" for requirement in deduped_requirements),
        encoding="utf-8",
    )

    return {
        "dependency_files": dependency_files,
        "dynamic_sources": dynamic_sources,
        "non_pypi": non_pypi,
        "requirements": deduped_requirements,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Trusted helpers for security scans.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract_parser = subparsers.add_parser(
        "extract-deps",
        help="Extract statically auditable dependency requirements.",
    )
    extract_parser.add_argument("src_dir", help="Plugin source directory")
    extract_parser.add_argument("requirements_out", help="Output requirements file path")

    args = parser.parse_args()

    if args.command == "extract-deps":
        src_dir = Path(args.src_dir)
        requirements_out = Path(args.requirements_out)
        result = extract_dependencies(src_dir, requirements_out)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
