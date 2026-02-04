import base64
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


def fetch_toml(repo: str, path: str, commit: str) -> dict:
    """Get the metadata from the TOML file in the given repository."""
    plugin_toml_url = f"https://api.github.com/repos/{repo}/contents/{path}?ref={commit}"
    req = add_auth(plugin_toml_url)
    response = request.urlopen(req)
    data = json.load(response)
    content = base64.b64decode(data["content"])
    toml = tomllib.loads(content)

    return toml


def extract_metadata(toml: dict, toml_format: str) -> dict:
    """Extract the necessary metadata from the plugin's metadata."""
    metadata = {}

    # Most of the necessary metadata for the index (name, version etc.) are
    # listed under `[project]` in `pyproject.toml` or at the top level otherwise
    if toml_format == "avogadro":
        project_metadata = toml["project"]
        avogadro_metadata = toml
    else:
        project_metadata = toml
        avogadro_metadata = toml["tool"]["avogadro"]
    
    for key in ["name", "version", "authors", "license"]:
        metadata[key] = project_metadata[key]

    # Description is optional
    if "description" in project_metadata:
        metadata["description"] = project_metadata["description"]
    else:
        metadata["description"] = ""
    
    metadata["plugin-type"] = avogadro_metadata["plugin-type"]

    # Also determine and store what features the plugin provides by looking at
    # which arrays the TOML contains
    metadata["feature-types"] = [t for t in FEATURE_TYPES if t in avogadro_metadata]

    return metadata


def check_metadata(metadata: dict):
    """Confirm that various fields in the extracted metadata are the appropriate
    format, type etc. according to the requirements."""

    if not isinstance(metadata["version"], str):
        raise Exception(f"Version number of {metadata["name"]} is not a string!")
    
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
        toml = fetch_toml(repo_info["git"], repo_info["path"], repo_info["commit"])
        toml_filename = repo_info["path"].split("/")[-1]
        if toml_filename == "avogadro.toml":
            toml_format = "avogadro"
        elif toml_filename == "pyproject.toml":
            toml_format = "pyproject"
        else:
            raise Exception(f"Metadata file provided by {plugin_name} not a recognized format!")
        plugin_metadata = extract_metadata(toml, toml_format)
        check_metadata(plugin_metadata)
        all_metadata.append(plugin_metadata)
    
    return all_metadata


if __name__ == "__main__":
    # When run as a script, get the metadata for all the repositories and save
    # as a JSON file in the current working directory
    repos_file = Path(__file__).with_name("repositories.toml")
    with open(repos_file, "b") as f:
        repos = tomllib.load(f)
    metadata = get_metadata_all(repos)
    with open(Path.cwd()/"plugins.json", "w", encoding="utf-8") as f:
        plugins_json = json.dump(f, metadata)
