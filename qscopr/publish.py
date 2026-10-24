"""Credentialed publisher, run separately from all upstream source/build code."""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path


def matching_build(build, manifest):
    package = build.get("source_package") or {}
    version = package.get("version", "")
    expected = f"{manifest['version']}-{manifest['rpm_release']}"
    return (
        package.get("name") == manifest.get("name", "quickshell")
        and version in [expected, expected + ".fc44"]
        and manifest["chroot"] in build.get("chroots", [])
    )


def validate_manifest(manifest, directory):
    if manifest.get("name", "quickshell") not in ["quickshell", "cpptrace"]:
        raise ValueError("Unexpected pipeline package")
    if not re.fullmatch(r"@?[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", manifest["project"]):
        raise ValueError("COPR project must be owner/project")
    if manifest["chroot"] != "fedora-44-x86_64":
        raise ValueError("Only Fedora 44 x86_64 publication is supported")
    filename = manifest["srpm"]
    if Path(filename).name != filename or not filename.endswith(".src.rpm"):
        raise ValueError("Unsafe SRPM filename")
    path = directory / filename
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("SRPM checksum mismatch")
    return path


def publish(client, manifest, directory, deadline_seconds=7200):
    srpm = validate_manifest(manifest, directory)
    owner, project = manifest["project"].split("/")
    project_config = client.project_proxy.get(owner, project)
    chroot_config = client.project_chroot_proxy.get(owner, project, manifest["chroot"])
    for config in [project_config, chroot_config]:
        if config.get("additional_repos") or config.get("additional_packages"):
            raise ValueError("Use a COPR project/chroot without extra repositories or injected packages")
    if manifest["chroot"] not in project_config.get("chroot_repos", {}):
        raise ValueError("Enable the Fedora 44 x86_64 chroot in the COPR project")

    # Inspect every history page, not only the latest build. A failed NVR must
    # be revalidated with a higher Release instead of being silently resubmitted.
    duplicate = None
    offset = 0
    while True:
        builds = client.build_proxy.get_list(
            owner, project, packagename=manifest.get("name", "quickshell"),
            pagination={"limit": 100, "offset": offset},
        )
        if not builds:
            break
        for build in builds:
            if not build.get("source_package") and build["state"] not in [
                "succeeded", "failed", "canceled", "skipped",
            ]:
                raise ValueError("A source build is pending; retry after its identity is available")
            if matching_build(build, manifest):
                duplicate = build
                break
        if duplicate:
            break
        offset += len(builds)

    if duplicate:
        if duplicate["state"] in ["failed", "canceled", "skipped"]:
            raise ValueError("This NVR already failed or was canceled/skipped; promote a higher RPM Release")
        build_id = duplicate["id"]
    else:
        # No automatic retries for this mutating request: after an ambiguous
        # upload failure, the next run consults history before doing anything.
        options = {
            "chroots": [manifest["chroot"]],
            "enable_net": False,
        }
        created = client.build_proxy.create_from_file(
            owner, project, str(srpm),
            buildopts=options,
        )
        build_id = created["id"]
    url = f"https://copr.fedorainfracloud.org/coprs/build/{build_id}/"
    result = {
        "build_id": build_id,
        "url": url,
        "already_submitted": duplicate is not None,
        "project": manifest["project"],
        "srpm_sha256": manifest["sha256"],
        "state": "pending",
        "name": manifest.get("name", "quickshell"),
    }
    if "prerequisite_build_id" in manifest:
        result["prerequisite_build_id"] = manifest["prerequisite_build_id"]
    result_path = directory / (
        "cpptrace-copr-result.json" if result["name"] == "cpptrace" else "copr-result.json"
    )
    result_path.write_text(json.dumps(result, indent=4) + "\n")
    print(f"COPR build: {url}", flush=True)
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        build = client.build_proxy.get(build_id)
        result["state"] = build["state"]
        result_path.write_text(json.dumps(result, indent=4) + "\n")
        if build["state"] == "succeeded":
            chroot = client.build_chroot_proxy.get(build_id, manifest["chroot"])
            result["chroot_state"] = chroot["state"]
            result_path.write_text(json.dumps(result, indent=4) + "\n")
            if chroot["state"] != "succeeded":
                raise RuntimeError("The Fedora 44 chroot did not succeed")
            print("COPR Fedora 44 build succeeded.", flush=True)
            return result
        if build["state"] in ["failed", "canceled", "skipped", "unknown"]:
            raise RuntimeError("COPR build did not succeed; inspect the recorded build URL")
        time.sleep(30)
    raise TimeoutError("COPR build is still running; inspect the recorded build URL")


def publish_pipeline(client, manifest, directory):
    prerequisites = manifest.get("prerequisites", [])
    if manifest.get("name", "quickshell") != "quickshell" or len(prerequisites) != 1:
        raise ValueError("The pipeline requires one cpptrace prerequisite followed by Quickshell")
    dependency = prerequisites[0]
    if (
        dependency.get("name") != "cpptrace"
        or dependency["project"] != manifest["project"]
        or dependency["chroot"] != manifest["chroot"]
    ):
        raise ValueError("cpptrace must be built in the same COPR project/chroot")
    # Validate both uploads before starting either external operation.
    validate_manifest(manifest, directory)
    validate_manifest(dependency, directory)
    completed = publish(client, dependency, directory)
    # COPR cannot create a dependency batch around an already finished build.
    # Waiting for overall/chroot success above is the stronger ordering gate:
    # batch completion alone also permits failed prerequisites.
    return publish(client, manifest | {"prerequisite_build_id": completed["build_id"]}, directory)


def main():
    parser = argparse.ArgumentParser(description="Publish a freshly verified SRPM to COPR")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Publication is deliberately separate from python -m qscopr check.
    if os.environ.get("ENABLE_COPR_PUBLISH") != "true":
        parser.error("Set ENABLE_COPR_PUBLISH=true to explicitly enable publication")
    try:
        from copr.v3 import Client

        client = Client({
            "copr_url": "https://copr.fedorainfracloud.org",
            "login": os.environ["COPR_LOGIN"],
            "token": os.environ["COPR_TOKEN"],
            "gssapi": False,
            "connection_attempts": 1,
        })
        output = args.output.resolve()
        manifest = json.loads((output / "publication.json").read_text())
        publish_pipeline(client, manifest, output)
    except Exception as error:
        # Avoid logging credential-bearing request/client exception details.
        print(f"COPR publication stopped ({type(error).__name__}); inspect copr-result.json if present.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
