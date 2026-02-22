import base64
import io
import json
from pathlib import Path
import tomllib
from urllib import request


"""A list of current plugin types."""
PLUGIN_TYPES = [
    "pyscript",
    "pypkg",
    "pypixi",
]

"""A list of current plugin feature types."""
FEATURE_TYPES = [
    "commands",
    "charges",
    "energy",
    "formats",
    "generators",
]


def add_auth(url):
    data_url = request.Request(url)
    base64_string = 'ZXRwMTI6cXdlcnR5Njc='
    data_url.add_header("Authorization", f"Basic {base64_string}")
    return data_url


def get_repo_metadata(repo: str, commit: str, release_tag: str | None) -> dict:
    """Get the metadata of the GitHub repo itself."""
    repo_metadata = {}

    api_url = f"https://api.github.com/repos/{repo}"
    req = add_auth(api_url)
    response = request.urlopen(req)
    repo_data = json.load(response)
    repo_metadata["last-update"] = repo_data["updated_at"]

    # Get the date and time of the specific commit provided
    commit_url = f"https://api.github.com/repos/{repo}/commits/{commit}"
    req = add_auth(commit_url)
    response = request.urlopen(req)
    commit_data = json.load(response)
    repo_metadata["commit-timestamp"] = commit_data["commit"]["committer"]["date"]

    # Look for the release if provided
    if release_tag is None:
        repo_metadata["has-release"] = False
    else:
        repo_metadata["has-release"] = True
        repo_metadata["release-tag"] = release_tag
        repo_metadata["release-version"] = release_tag.lstrip("v")
        release_url = f"https://api.github.com/repos/{repo}/releases/tags/{release_tag}"
        req = add_auth(release_url)
        response = request.urlopen(req)
        release_data = json.load(response)
        # TODO Get the corresponding commit

    return repo_metadata


def fetch_toml(repo: str, commit: str, path: str | None, metadata_file: str) -> dict:
    """Get the metadata from the TOML file in the given repository."""
    metadata_path = f"{path}/{metadata_file}" if path else metadata_file
    plugin_toml_url = f"https://api.github.com/repos/{repo}/contents/{metadata_path}?ref={commit}"
    req = add_auth(plugin_toml_url)
    response = request.urlopen(req)
    data = json.load(response)
    content = base64.b64decode(data["content"])
    toml = tomllib.load(io.BytesIO(content))

    return toml


def extract_plugin_metadata(toml: dict, toml_format: str) -> dict:
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
        metadata[required_key] = project_metadata[required_key]

    # Optional fields in `[project]`/the top level
    metadata["description"] = project_metadata.get("description", "")

    # Required fields in `[tool.avogadro]`/the top level
    for required_key in []:
        metadata[required_key] = avogadro_metadata[required_key]
    
    # Optional fields in `[tool.avogadro]`/the top level
    metadata["minimum-avogadro-version"] = avogadro_metadata.get("minimum-avogadro-version", "1.103")

    # Also determine and store what features the plugin provides by looking at
    # which arrays the TOML contains
    metadata["feature-types"] = [t for t in FEATURE_TYPES if t in avogadro_metadata]

    return metadata


def check_metadata(metadata: dict):
    """Confirm that various fields in the extracted metadata are the appropriate
    format, type etc. according to the requirements."""

    if not isinstance(metadata["minimum-avogadro-version"], str):
        raise Exception(f"Minimum Avogadro version number of {metadata["name"]} is not a string!")
    
    if not isinstance(metadata["version"], str):
        raise Exception(f"Version number of {metadata["name"]} is not a string!")
    
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


def get_metadata_all(repos: dict[str, dict]) -> dict[str, dict]:
    """Collect all the metadata for all plugins with repository information in
    the provided dict."""
    all_metadata = []

    for plugin_name, repo_info in repos.items():
        # Take out the repo owner/name string from the git URL
        repo = repo_info["git"].removeprefix("https://github.com/").removesuffix(".git")

        release_tag = repo_info.get("release-tag", None)

        repo_metadata = get_repo_metadata(repo, repo_info["commit"], release_tag)

        path = repo_info.get("path")

        toml = fetch_toml(repo, repo_info["commit"], path, repo_info["metadata"])
        toml_filename = repo_info["metadata"].split("/")[-1]
        if toml_filename == "avogadro.toml":
            toml_format = "avogadro"
        elif toml_filename == "pyproject.toml":
            toml_format = "pyproject"
        else:
            raise Exception(f"Metadata file provided by {plugin_name} not a recognized format!")
        toml_metadata = extract_plugin_metadata(toml, toml_format)

        # Combine metadata from all sources
        plugin_metadata = repo_metadata | toml_metadata

        check_metadata(plugin_metadata)

        all_metadata.append(plugin_metadata)
    
    return all_metadata


if __name__ == "__main__":
    # When run as a script, get the metadata for all the repositories and save
    # as a JSON file in the current working directory
    repos_file = Path(__file__).with_name("repositories.toml")
    with open(repos_file, "rb") as f:
        repos = tomllib.load(f)
    metadata = get_metadata_all(repos)
    with open(Path.cwd()/"plugins.json", "w", encoding="utf-8") as f:
        plugins_json = json.dump(metadata, f)
