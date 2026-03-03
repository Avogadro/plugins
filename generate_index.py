# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "requests>=2.32.5",
# ]
# ///

import base64
import hashlib
import io
import json
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path

import requests


"""A list of current plugin types."""
PLUGIN_TYPES = [
    "pyscript",
    "pypkg",
    "pypixi",
]

"""A list of current plugin feature types."""
FEATURE_TYPES = [
    "electrostatic-models",
    "energy-models",
    "file-formats",
    "input-generators",
    "menu-commands",
]


def get_gh_repo_metadata(repo: str, commit: str, release_tag: str | None) -> dict:
    """Get the metadata of the GitHub repo itself."""
    repo_metadata = {}

    api_url = f"https://api.github.com/repos/{repo}"
    repo_data = requests.get(api_url).json()
    repo_metadata["last-update"] = repo_data["updated_at"]
    repo_metadata["gh-stars"] = repo_data["stargazers_count"]

    # Get the public URL of the repo's README
    # (we'll check this is the same as the plugin's README later)
    readme_url = f"{api_url}/readme"
    readme_data = requests.get(readme_url).json()
    repo_metadata["gh-readme"] = {
        "url": readme_data["html_url"],
        "path": readme_data["path"],
    }

    # Get the date and time of the specific commit provided
    commit_url = f"{api_url}/commits/{commit}"
    commit_data = requests.get(commit_url).json()
    repo_metadata["commit-timestamp"] = commit_data["commit"]["committer"]["date"]

    # Look for the release if provided
    if release_tag is None:
        repo_metadata["has-release"] = False
    else:
        repo_metadata["has-release"] = True
        repo_metadata["release-tag"] = release_tag
        repo_metadata["release-version"] = release_tag.lstrip("v")
        #release_url = f"{api_url}/releases/tags/{release_tag}"
        #release_data = requests.get(release_url).json()
        # TODO Get the corresponding commit

    return repo_metadata


def fetch_gh_toml(repo: str, commit: str, path: str | None, metadata_file: str) -> dict:
    """Get the metadata from the TOML file in the given repository."""
    metadata_path = f"{path}/{metadata_file}" if path else metadata_file
    plugin_toml_url = f"https://api.github.com/repos/{repo}/contents/{metadata_path}?ref={commit}"
    data = requests.get(plugin_toml_url).json()
    content = base64.b64decode(data["content"])
    toml = tomllib.load(io.BytesIO(content))

    return toml


def extract_toml_metadata(toml: dict, toml_format: str) -> dict:
    """Extract the necessary metadata from the plugin's metadata."""
    metadata = {}

    # Most of the necessary metadata for the index (name, version etc.) are
    # listed under `[project]` in `pyproject.toml` or at the top level otherwise
    if toml_format == "avogadro":
        project_metadata = toml
        avogadro_metadata = toml
    else:
        project_metadata = toml["project"]
        avogadro_metadata = toml["tool"]["avogadro"]
    
    # Required fields in `[project]`/the top level
    for required_key in ["name", "version", "authors", "license"]:
        # Only want authors' names, not emails
        if required_key == "authors":
            authors = [author["name"] for author in project_metadata["authors"] if "name" in author]
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
    metadata["minimum-avogadro-version"] = avogadro_metadata.get("minimum-avogadro-version", "1.103")

    # Also determine and store what features the plugin provides by looking at
    # which arrays the TOML contains
    metadata["feature-types"] = [t for t in FEATURE_TYPES if t in avogadro_metadata]

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
    for required_key in ["metadata", "plugin-type"]:
        assert required_key in repo_info
    
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
        raise Exception(f"Minimum Avogadro version number of {metadata["name"]} is not a string!")
    
    if not isinstance(metadata["version"], str):
        raise Exception(f"Version number of {metadata["name"]} is not a string!")
    
    # For now, Avogadro does not support fetching arbitrary Git repos with git
    # The plugin downloader requires the source URL and hash to function
    assert "url" in metadata["src"]
    assert "sha256" in metadata["src"]
    
    # If the plugin uses release tags, we want to verify that the commit of the
    # release is the one being uploaded
    if metadata["has-release"]:
        # The version in the TOML file must match the release version
        if not metadata["version"] == metadata["release-version"]:
            raise Exception(f"Version number of {metadata["name"]} does not match the release!")

        # The commit of a release and the commit given in the TOML file must match
        # TODO
    
    for c in metadata["name"]:
        # Only a-z, A-Z, 0-9, - are valid in plugin names
        if c.isascii() and (c.isalnum() or c == "-"):
            continue
        else:
            raise Exception(f"{metadata["name"]} is not a valid plugin name!")


def tidy_metadata(metadata: dict) -> dict:
    """Do any final tidying up of the metadata.
    
    This changes the input `dict`."""
    # Check that any GitHub README found is also the plugin's README
    # If not, ditch the URL
    # Though if the URL was provided in repositories.toml, we don't overwrite it
    if metadata["gh-readme"] and not metadata.get("readme-url"):
        gh_readme = metadata.pop("gh-readme")
        if gh_readme["path"] == metadata["readme"]:
            metadata["readme-url"] = gh_readme["url"]
        else:
            metadata["readme-url"] = None
    
    return metadata


def get_metadata_all(repos: dict[str, dict]) -> dict[str, dict]:
    """Collect all the metadata for all plugins with repository information in
    the provided dict."""
    all_metadata = []

    for plugin_name, repo_info in repos.items():
        # First just validate the information in `repositories.toml`
        validate_repo_info(repo_info)

        toml_filename = repo_info["metadata"].split("/")[-1]
        if toml_filename == "avogadro.toml":
            toml_format = "avogadro"
        elif toml_filename == "pyproject.toml":
            toml_format = "pyproject"
        else:
            raise Exception(f"Metadata file provided by {plugin_name} not a recognized format!")
        
        if "git" in repo_info:
            # Git repo, which for now means it will always be a GitHub repo
            # (support for GitLab and arbitrary repos is hopefully to come)
            repo_url = repo_info["git"]["repo"].removesuffix(".git")
            repo = repo_url.removeprefix("https://github.com/")
            commit = repo_info["git"]["commit"]

            # Automatically generate the source archive details for Avogadro
            src_url = f"{repo_url}/archive/{commit}.zip"
            # Work out the hash
            r = requests.get(src_url, stream=True)
            with tempfile.TemporaryFile() as tmp:
                src_hash = hashlib.sha256()
                for chunk in r.iter_content(chunk_size=8192):
                    tmp.write(chunk)
                    src_hash.update(chunk)
                tmp.close()
                src_sha256= src_hash.hexdigest()
            repo_info["src"] = {"url": src_url, "sha256": src_sha256}

            path = repo_info.get("path")
            toml = fetch_gh_toml(repo, commit, path, repo_info["metadata"])
            toml_metadata = extract_toml_metadata(toml, toml_format)

            release_tag = repo_info.get("release-tag", None)
            repo_metadata = get_gh_repo_metadata(repo, commit, release_tag)
        else:
            # Arbitrary source code archive
            src_info = repo_info["src"]
            r = requests.get(src_info["url"], stream=True)
            with tempfile.NamedTemporaryFile(delete_on_close=False) as tmp:
                src_hash = hashlib.sha256()
                for chunk in r.iter_content(chunk_size=8192):
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
                    path = repo_info.get("path")
                    if len(list(src.iterdir())) > 1:
                        toml_file: Path = src/path/toml_filename
                    else:
                        # Archive was presumably nested
                        toml_file: Path = next(src.iterdir())/path/toml_filename
                    with open(toml_file, "rb") as f:
                        toml = tomllib.load(f)
                    toml_metadata = extract_toml_metadata(toml, toml_format)

            # There is no repo metadata to look at, so just set defaults
            repo_metadata = {"has-release": False, "readme-url": None}

        # Combine metadata from all sources, including that in `repositories.toml`
        plugin_metadata = toml_metadata | repo_info | repo_metadata

        validate_metadata(plugin_metadata)
        plugin_metadata = tidy_metadata(plugin_metadata)

        all_metadata.append(plugin_metadata)
    
    return all_metadata


if __name__ == "__main__":
    # When run as a script, get the metadata for all the repositories and save
    # as a JSON file in the current working directory
    repos_file = Path(__file__).with_name("repositories.toml")
    with open(repos_file, "rb") as f:
        repos = tomllib.load(f)
    metadata = get_metadata_all(repos)
    args = sys.argv
    indent = 2 if "--pretty" in args else None
    if "--show" in args:
        print(json.dumps(metadata, indent=indent))
    else:
        with open(Path.cwd()/"plugins2.json", "w", encoding="utf-8") as f:
            plugins_json = json.dump(metadata, f, indent=indent)
