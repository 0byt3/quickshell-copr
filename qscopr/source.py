"""Release identity, downloads, and safe archive handling."""

import hashlib
import json
import re
import tarfile
import urllib.request
from pathlib import Path, PurePosixPath


class GateError(Exception):
    """An expected gate failure with a machine-readable outcome."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def version_key(tag):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    if not match:
        raise GateError("discovery-blocked", f"Unsupported stable release tag: {tag!r}")
    return tuple(int(part) for part in match.groups())


def request(url):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "quickshell-copr-release-gate",
        },
    )
    # No authentication token is needed or forwarded to source download hosts.
    return urllib.request.urlopen(req, timeout=90)


def api(path):
    with request(f"https://api.github.com/{path}") as response:
        return json.load(response)


def validate_repository(repository):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("upstream must be an owner/repository pair")


def select_release(repository, tag=None):
    validate_repository(repository)
    if tag:
        version_key(tag)
    endpoint = f"tags/{tag}" if tag else "latest"
    release = api(f"repos/{repository}/releases/{endpoint}")
    if release["draft"] or release["prerelease"]:
        raise GateError("discovery-blocked", "Only stable, published releases are accepted")
    tag = release["tag_name"]
    version_key(tag)
    # The commits endpoint dereferences annotated tags as well as lightweight tags.
    commit = api(f"repos/{repository}/commits/{tag}")["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise GateError("discovery-blocked", "Invalid upstream commit identity")
    return {
        "tag": tag,
        "version": tag.removeprefix("v"),
        "commit": commit,
        "url": release["html_url"],
        "published_at": release["published_at"],
        "repository": repository,
    }


def should_build(release, state, force=False):
    if not state:
        return True
    candidate = version_key(release["version"])
    previous = version_key(state["release"]["version"])
    if candidate < previous:
        raise GateError("discovery-blocked", "Refusing to downgrade the validated release")
    if candidate == previous and release["commit"] != state["release"]["commit"]:
        raise GateError("discovery-blocked", "Previously validated release tag has moved")
    return force or candidate > previous


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(release, destination):
    repository = release["repository"]
    commit = release["commit"]
    url = f"https://codeload.github.com/{repository}/tar.gz/{commit}"
    with request(url) as response, Path(destination).open("wb") as stream:
        size = 0
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 100 * 1024 * 1024:
                raise GateError("discovery-blocked", "Source archive exceeds 100 MiB")
            stream.write(chunk)
    release["sha256"] = sha256(destination)


def extract_source(archive, destination):
    """Reject links, devices, duplicate paths, traversal, and decompression bombs."""
    destination = Path(destination)
    with tarfile.open(archive, "r:gz") as source:
        members = source.getmembers()
        if len(members) > 30000 or sum(m.size for m in members) > 512 * 1024 * 1024:
            raise GateError("discovery-blocked", "Source archive exceeds extraction limits")
        roots = set()
        paths = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or "\\" in member.name
                or member.name in paths
                or not (member.isfile() or member.isdir())
            ):
                raise GateError("discovery-blocked", f"Unsafe archive entry: {member.name}")
            paths.add(member.name)
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise GateError("discovery-blocked", "Expected one source archive root")
        source.extractall(destination, filter="data")
    return destination / roots.pop()
