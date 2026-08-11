# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "PyGitHub>=2.0",
# ]
# ///

import argparse
import hashlib
import io
import json
import re
import shutil
import tempfile
import tomllib
import traceback
import urllib.error
import urllib.request
from pathlib import Path

from github import Github
from github import Auth


"""A list of current plugin types."""
PLUGIN_TYPES = [
    "pyscript",
    "pypkg",
]

"""Base URL of the download counter that plugin downloads are routed through.

GitHub reports no download count for the auto-generated source archives, so
installs are counted by a redirect hop in front of them. See `worker/`."""
DEFAULT_WORKER = "https://plugins.avogadro.cc"

"""A list of current plugin feature types."""
FEATURE_TYPES = [
    "electrostatic-models",
    "energy-models",
    "file-formats",
    "input-generators",
    "menu-commands",
]


"""User agent for this script's own HTTP requests.

Cloudflare answers the default `Python-urllib/...` agent with a 403, which would
silently disable the download counter and fall the index back to GitHub URLs on
every build."""
USER_AGENT = "avogadro-plugin-index/1.0 (+https://github.com/Avogadro/plugins)"


def request(url: str, method: str = "GET") -> urllib.request.Request:
    """Build a request that identifies itself."""
    return urllib.request.Request(
        url, method=method, headers={"User-Agent": USER_AGENT}
    )


def normalize_pkg_name(name: str) -> str:
    """Normalize a package name as per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).lower()


def check_download_url(download_url: str, expected_target: str):
    """Confirm the download counter resolves a plugin to the expected archive.

    Uses HEAD, which the Worker deliberately does not count, so validating the
    index costs nothing in the download statistics. Catches the case where the
    Worker cannot resolve a plugin whose repository name differs from its
    plugin name."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request(download_url, "HEAD")) as response:
            raise Exception(
                f"{download_url} returned {response.status}, expected a redirect!"
            )
    except urllib.error.HTTPError as e:
        if e.code not in (301, 302, 307, 308):
            raise Exception(f"{download_url} returned {e.code}!")
        target = e.headers.get("Location")
        if target != expected_target:
            raise Exception(
                f"{download_url} redirects to {target}, expected {expected_target}!"
            )


def worker_available(worker: str) -> bool:
    """Check the download counter is reachable before relying on it.

    Without this, an outage would make every plugin fail its URL check and drop
    out of the generated index. Losing the download statistics for one build is
    an acceptable failure; publishing an empty index is not."""
    try:
        with urllib.request.urlopen(request(f"{worker}/health"), timeout=10) as r:
            if r.status == 200:
                return True
            print(f"{worker} returned {r.status} rather than 200")
    except Exception as e:
        print(f"Could not reach {worker}: {e}")
    return False


def fetch_download_counts(stats_url: str) -> dict:
    """Fetch per-plugin download counts from the download counter.

    Returns an empty dict on any failure: statistics are a nice-to-have, and
    must never stop the index from being generated."""
    try:
        with urllib.request.urlopen(request(stats_url), timeout=30) as response:
            stats = json.load(response)
    except Exception as e:
        print(f"Could not fetch download counts from {stats_url}: {e}")
        return {}

    window = stats.get("window_days")
    counts = {}
    for plugin, entry in stats.get("plugins", {}).items():
        counts[plugin] = {
            "total": entry.get("total", 0),
            "recent": entry.get("recent", 0),
            "window-days": window,
        }
    print(f"Fetched download counts for {len(counts)} plugins")
    return counts


def add_download_counts(all_metadata: list[dict], counts: dict):
    """Merge download counts into the generated metadata.

    A plugin with no counter entry gets zeroes rather than a missing key, so
    that consumers do not have to special-case a newly added plugin."""
    for metadata in all_metadata:
        short_name = metadata["name"].removeprefix("avogadro-")
        metadata["downloads"] = counts.get(
            short_name, {"total": 0, "recent": 0, "window-days": None}
        )


def get_gh_repo_metadata(gh_repo, commit: str, release_tag: str | None) -> dict:
    """Get the metadata of the GitHub repo itself."""
    repo_metadata = {}

    repo_metadata["last-update"] = gh_repo.updated_at.isoformat()
    repo_metadata["gh-stars"] = gh_repo.stargazers_count

    # Get the public URL of the repo's README
    # (we'll check this is the same as the plugin's README later)
    readme = gh_repo.get_readme()
    repo_metadata["gh-readme"] = {
        "url": readme.html_url,
        "path": readme.path,
    }

    # Get the date and time of the specific commit provided
    gh_commit = gh_repo.get_commit(commit)
    repo_metadata["commit-timestamp"] = gh_commit.commit.committer.date.isoformat()

    # Look for the release if provided
    if release_tag is None:
        repo_metadata["has-release"] = False
    else:
        repo_metadata["has-release"] = True
        repo_metadata["release-tag"] = release_tag
        repo_metadata["release-version"] = release_tag.lstrip("v")
        # TODO Get the corresponding commit and verify it matches

    return repo_metadata


def fetch_gh_toml(gh_repo, commit: str, path: str | None, metadata_file: str) -> dict:
    """Get the metadata from the TOML file in the given repository."""
    metadata_path = f"{path}/{metadata_file}" if path else metadata_file
    contents = gh_repo.get_contents(metadata_path, ref=commit)
    toml = tomllib.load(io.BytesIO(contents.decoded_content))
    return toml


def extract_toml_metadata(toml: dict, toml_format: str) -> dict:
    """Extract the necessary metadata from the plugin's metadata."""
    metadata = {}

    # Most of the necessary metadata for the index (name, version etc.) are
    # listed under `[project]` in `pyproject.toml` or at the top level otherwise
    if toml_format == "avogadro":
        project_metadata = toml
        avogadro_metadata = toml
        pixi_metadata = None
    else:
        project_metadata = toml["project"]
        avogadro_metadata = toml["tool"]["avogadro"]
        pixi_metadata = toml["tool"].get("pixi", None)

    # Required fields in `[project]`/the top level
    for required_key in ["name", "version", "authors", "license"]:
        # Only want authors' names, not emails
        if required_key == "authors":
            authors = [
                author["name"]
                for author in project_metadata["authors"]
                if "name" in author
            ]
            metadata["authors"] = authors
            continue
        metadata[required_key] = project_metadata[required_key]

    # Optional fields in `[project]`/the top level
    metadata["description"] = project_metadata.get("description", "")
    metadata["readme"] = project_metadata.get("readme", None)

    # Required fields in `[tool.avogadro]`/the top level
    for required_key in []:
        metadata[required_key] = avogadro_metadata[required_key]

    # Optional fields in `[tool.avogadro]`/the top level
    metadata["minimum-avogadro-version"] = avogadro_metadata.get(
        "minimum-avogadro-version", "1.103"
    )

    # Also determine and store what features the plugin provides by looking at
    # which arrays the TOML contains
    metadata["feature-types"] = [t for t in FEATURE_TYPES if t in avogadro_metadata]

    # If the plugin is a Python package...
    if "scripts" in project_metadata:
        # Get the entry point
        # (It doesn't go into the index, but we want to validate it)
        metadata["scripts"] = project_metadata["scripts"]
        # The plugin is required to have a minimal Pixi table, regardless of
        # whether the plugin needs Conda dependencies
        if "channels" not in pixi_metadata["workspace"]:
            raise Exception(
                f"{metadata['name']} needs to specify a Conda channel! e.g. conda-forge"
            )
        # Might be useful to know what platforms the plugin supports
        # Note that these are Conda platform tags and differ from those of
        # Python; they're also hard to find proper documentation for
        metadata["conda-platforms"] = pixi_metadata["workspace"]["platforms"]
        # Work out whether the plugin has Conda dependencies and therefore
        # requires Avogadro to have access to Pixi
        metadata["conda-dependencies"] = list(
            pixi_metadata.get("dependencies", {}).keys()
        )
        # Make sure the package itself is an editable dependency, but that
        # there's no other PyPI dependencies listed in the Pixi table.
        # The package must always list itself, even when all other
        # dependencies are resolved via conda-forge.
        # Have to normalize the dependency names first
        normalized_name = normalize_pkg_name(metadata["name"])
        pypi_deps = [
            normalize_pkg_name(p)
            for p in pixi_metadata.get("pypi-dependencies", {}).keys()
        ]
        if len(pypi_deps) < 1 or normalized_name not in pypi_deps:
            raise Exception(
                f"{metadata['name']} does not include itself as an editable dependency!"
            )
        if len(pypi_deps) > 1:
            raise Exception(
                f"{metadata['name']} specifies PyPI dependencies in tool.pixi.pypi-dependencies instead of project.dependencies!"
            )
    return metadata


def validate_repo_info(repo_info: dict):
    """Confirm that the information provided in `repositories.toml` is complete
    and well-formed."""

    git_info = repo_info.get("git")
    src_info = repo_info.get("src")
    # Make sure either the git repository or a source archive were provided
    assert git_info or src_info

    if git_info:
        # Check details of the git repository were provided
        assert "repo" in git_info
        assert "commit" in git_info
    else:
        # Check for details of the source archive
        assert "url" in src_info
        assert "sha256" in src_info

    # Confirm presence of other required information
    # Metadata file must be `avogadro.toml` or `pyproject.toml` at top level
    assert repo_info["metadata"] in ["avogadro.toml", "pyproject.toml"]

    assert repo_info["plugin-type"] in PLUGIN_TYPES

    # Make sure that any path provided is to a directory, not a file, but with
    # no final slash, and that backslashes aren't used
    if "path" in repo_info:
        path: str = repo_info["path"]
        assert not path.endswith("/")
        assert "\\" not in path
        final_component = path.split("/")
        assert "." not in final_component


def validate_metadata(metadata: dict):
    """Confirm that various fields in the extracted metadata are the appropriate
    format, type etc. according to the requirements."""

    if not isinstance(metadata["minimum-avogadro-version"], str):
        raise Exception(
            f"Minimum Avogadro version number of {metadata['name']} is not a string!"
        )

    if not isinstance(metadata["version"], str):
        raise Exception(f"Version number of {metadata['name']} is not a string!")

    # For now, Avogadro does not support fetching arbitrary Git repos with git
    # The plugin downloader requires the source URL and hash to function
    assert "url" in metadata["src"]
    assert "sha256" in metadata["src"]

    # If the plugin uses release tags, we want to verify that the commit of the
    # release is the one being uploaded
    if metadata["has-release"]:
        # The version in the TOML file must match the release version
        if not metadata["version"] == metadata["release-version"]:
            raise Exception(
                f"Version number of {metadata['name']} does not match the release!"
            )

        # The commit of a release and the commit given in the TOML file must match
        # TODO

    name: str = metadata["name"]
    for c in name:
        # Only a-z, A-Z, 0-9, - are valid in plugin names
        if c.isascii() and (c.isalnum() or c == "-"):
            continue
        else:
            raise Exception(
                f"{name} is not a valid plugin name (disallowed characters)!"
            )
    # Plugin name must begin with `avogadro-`
    if not name.startswith("avogadro-"):
        raise Exception(f"{name} is not a valid plugin name (missing prefix)!")

    # Python packages must have a correctly defined entry point
    if metadata["plugin-type"] == "pypkg":
        scripts = metadata.get("scripts")
        if scripts and metadata["name"] in scripts:
            pass
        else:
            raise Exception(
                f"{metadata['name']} does not define an entry point that is the same as the plugin name!"
            )


def tidy_metadata(metadata: dict):
    """Do any final tidying up of the metadata.

    This changes the input `dict`."""
    # Check the GitHub README against the plugin README
    # Though if the URL was provided in repositories.toml, we don't overwrite it
    if metadata["gh-readme"] and not metadata.get("readme-url"):
        gh_readme = metadata.pop("gh-readme")
        print(f"Found GitHub README for {metadata['name']} at {gh_readme['url']}")
        metadata["readme-url"] = gh_readme["url"]

    # Don't need the entry points/scripts in the index
    metadata.pop("scripts", None)


def set_defaults(repo_info: dict):
    """Set default values for various keys that are optional in `repositories.toml`."""
    # Chosen because at the moment the vast majority of plugins are Python packages;
    # this may change in future
    defaults = {
        "metadata": "pyproject.toml",
        "plugin-type": "pypkg",
    }
    for k, v in defaults.items():
        repo_info.setdefault(k, v)


def get_metadata(
    table_name: str, repo_info: dict, gh: Github, worker: str | None, strict: bool
) -> dict:
    """Get and validate the metadata for a single plugin based on the provided
    information."""
    # First add the default values for any optional keys
    set_defaults(repo_info)
    print(f"Generating metadata for {table_name} using {repo_info['metadata']}...")
    # Next just validate the information in `repositories.toml`
    validate_repo_info(repo_info)

    toml_filename = repo_info["metadata"].split("/")[-1]
    if toml_filename == "avogadro.toml":
        toml_format = "avogadro"
    elif toml_filename == "pyproject.toml":
        toml_format = "pyproject"
    else:
        raise Exception(
            f"Metadata file provided by {table_name} not a recognized format!"
        )

    if "git" in repo_info:
        # Git repo, which for now means it will always be a GitHub repo
        # (support for GitLab and arbitrary repos is hopefully to come)
        repo_url = repo_info["git"]["repo"].removesuffix(".git")
        repo_name = repo_url.removeprefix("https://github.com/")
        commit = repo_info["git"]["commit"]
        gh_repo = gh.get_repo(repo_name)

        # Automatically generate the source archive details for Avogadro
        # Always hash the archive straight from GitHub, so that generating the
        # index does not depend on the download counter being up
        archive_url = f"{repo_url}/archive/{commit}.zip"
        src_hash = hashlib.sha256()
        with urllib.request.urlopen(request(archive_url)) as response:
            while chunk := response.read(8192):
                src_hash.update(chunk)

        # Hand Avogadro the counted URL instead, which redirects to exactly the
        # archive hashed above, so the recorded hash still validates
        src_url = archive_url
        if worker:
            counted_url = f"{worker}/dl/{table_name}/{commit}.zip"
            try:
                check_download_url(counted_url, archive_url)
                src_url = counted_url
            except Exception as e:
                # An uncounted install beats a broken one, so fall back to the
                # archive URL rather than dropping the plugin from the index
                print(f"WARNING: {e}")
                print(f"Falling back to the GitHub archive URL for {table_name}")
                if strict:
                    raise

        repo_info["src"] = {"url": src_url, "sha256": src_hash.hexdigest()}

        path = repo_info.get("path", ".")
        toml = fetch_gh_toml(gh_repo, commit, path, repo_info["metadata"])
        toml_metadata = extract_toml_metadata(toml, toml_format)

        release_tag = repo_info.get("release-tag", None)
        repo_metadata = get_gh_repo_metadata(gh_repo, commit, release_tag)
    else:
        # Arbitrary source code archive
        src_info = repo_info["src"]
        src_hash = hashlib.sha256()
        with tempfile.NamedTemporaryFile(delete_on_close=False) as tmp:
            with urllib.request.urlopen(src_info["url"]) as response:
                while chunk := response.read(8192):
                    tmp.write(chunk)
                    src_hash.update(chunk)
            tmp.close()
            # First confirm the hash is correct
            assert src_hash.hexdigest() == src_info["sha256"]
            # Extract the archive's contents
            # We don't know the format, so try all in turn
            with tempfile.TemporaryDirectory() as d:
                src = Path(d)
                for fmt in ["zip", "tar", "gztar", "bztar", "xztar"]:
                    try:
                        shutil.unpack_archive(tmp.name, src, fmt)
                        # Stop once we extract successfully
                        break
                    except shutil.ReadError:
                        continue
                path = repo_info.get("path", ".")
                if len(list(src.iterdir())) > 1:
                    toml_file: Path = src / path / toml_filename
                else:
                    # Archive was presumably nested
                    toml_file: Path = next(src.iterdir()) / path / toml_filename
                with open(toml_file, "rb") as f:
                    toml = tomllib.load(f)
                toml_metadata = extract_toml_metadata(toml, toml_format)

        # There is no repo metadata to look at, so just set defaults
        repo_metadata = {"has-release": False, "readme-url": None}

    # Combine metadata from all sources, including that in `repositories.toml`
    # The TOML metadata file of the plugin itself takes precedence
    plugin_metadata = repo_info | repo_metadata | toml_metadata

    validate_metadata(plugin_metadata)
    # The one thing that doesn't get validated by `validate_metadata()` is
    # that the table key was the plugin name (minus the `avogadro-` prefix)
    if plugin_metadata["name"] != "avogadro-" + table_name:
        raise Exception(
            f"The name of the [{table_name}] table in repositories.toml is incorrect!\nThe plugin name is {plugin_metadata['name']}\nThe table header should be [{plugin_metadata['name'].removeprefix('avogadro-')}]"
        )
    tidy_metadata(plugin_metadata)

    return plugin_metadata


def get_metadata_all(
    repos: dict[str, dict], gh: Github, strict: bool, worker: str | None
) -> list[dict]:
    """Collect all the metadata for all plugins with repository information in
    the provided dict."""
    all_metadata = []

    for table_name, repo_info in repos.items():
        print("----------" * 8)
        try:
            plugin_metadata = get_metadata(table_name, repo_info, gh, worker, strict)
            all_metadata.append(plugin_metadata)
            print(f"Metadata OK, {table_name} added to generated plugin index")
        except Exception as e:
            if strict:
                raise e
            else:
                # By default, don't halt if a plugin fails, because that prevents us
                # seeing how many plugins fail when we change this script
                # We should *not* include the plugin in the index though, obviously
                traceback.print_exception(e)

    return all_metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate plugin index")
    parser.add_argument("--token", "-t", help="GitHub access token")
    parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print JSON output"
    )
    parser.add_argument(
        "--show", action="store_true", help="Print to stdout instead of saving to file"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Error if any plugin entry raises an exception",
    )
    parser.add_argument(
        "-p",
        "--plugins",
        nargs="+",
        help="Fetch and validate the metadata for only the given plugins",
    )
    parser.add_argument(
        "--worker",
        default=DEFAULT_WORKER,
        help=(
            "Base URL of the download counter, which the source URLs are routed"
            f" through (default: {DEFAULT_WORKER})"
        ),
    )
    parser.add_argument(
        "--no-worker",
        action="store_true",
        help="Point source URLs straight at GitHub and omit download counts",
    )
    parser.add_argument(
        "repos_file",
        nargs="?",
        default=Path(__file__).with_name("repositories.toml"),
        type=Path,
        help="Path to repositories.toml (default: next to this script)",
    )
    args = parser.parse_args()

    worker = None if args.no_worker else args.worker.rstrip("/")
    if worker and not worker_available(worker):
        print("Continuing with GitHub archive URLs and no download counts")
        worker = None

    auth = Auth.Token(args.token) if args.token else None

    gh = Github(auth=auth)

    repos_file = args.repos_file
    with open(repos_file, "rb") as f:
        repos = tomllib.load(f)
    if args.plugins:
        repos = {k: v for k, v in repos.items() if k in args.plugins}
    metadata = get_metadata_all(repos, gh, args.strict, worker)
    if worker:
        add_download_counts(metadata, fetch_download_counts(f"{worker}/stats"))
    indent = 2 if args.pretty else None
    if args.show:
        print(json.dumps(metadata, indent=indent))
    else:
        with open(Path.cwd() / "plugins2.json", "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=indent)
