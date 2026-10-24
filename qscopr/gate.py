"""Host-side orchestration; no writable host mounts or credentials in builders."""

import hashlib
import json
import shutil
import subprocess
import tempfile
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .discovery import discover
from .cpptrace import SMOKE_SOURCE, prepare_cpptrace, repository_requirements
from .packaging import render_cpptrace_spec, render_spec
from .source import (
    GateError,
    download,
    extract_source,
    select_release,
    sha256,
    should_build,
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=4, sort_keys=True) + "\n")


def load_state(project):
    path = project / "packaging" / "validated.json"
    return json.loads(path.read_text()) if path.exists() else None


def inputs_hash(project):
    digest = hashlib.sha256()
    paths = [
        project / "config.toml",
        *sorted((project / "qscopr").glob("*.py")),
    ]
    for path in paths:
        digest.update(str(path.relative_to(project)).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def load_config(project):
    config = tomllib.loads((project / "config.toml").read_text())
    if config["fedora"] != 44 or config["architecture"] != "x86_64":
        raise ValueError("This initial gate supports only Fedora 44 x86_64")
    if config["image"] != "registry.fedoraproject.org/fedora:44":
        raise ValueError("Use the official Fedora 44 container image")
    if type(config["jobs"]) is not int or not 1 <= config["jobs"] <= 8:
        raise ValueError("jobs must be an integer between 1 and 8")
    return config


def run(command, log, capture=False):
    print("+", " ".join(map(str, command)), flush=True)
    with log.open("a") as stream:
        if capture:
            result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=stream)
            stream.write(result.stdout)
            result.check_returncode()
            return result.stdout.strip()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        for line in process.stdout:
            stream.write(line)
            stream.flush()
            print(line, end="", flush=True)
        code = process.wait()
        if code:
            raise subprocess.CalledProcessError(code, command)


def container_gate(project, workspace, output, config):
    runtime = shutil.which("docker") or shutil.which("podman")
    if not runtime:
        raise GateError("infrastructure-failed", "Docker or Podman is required")
    log = output / "container.log"
    token = uuid.uuid4().hex
    preparation = f"qscopr-prepare-{token}"
    builder = f"qscopr-build-{token}"
    dependency_builder = f"qscopr-cpptrace-{token}"
    installer = f"qscopr-install-{token}"
    snapshot = None
    snapshots = []
    active = None
    isolation = [
        "--platform=linux/amd64",
        "--security-opt=no-new-privileges",
        "--pids-limit=1024",
    ]
    result = {}
    try:
        run([runtime, "pull", "--platform=linux/amd64", config["image"]], log)
        image = json.loads(run(
            [runtime, "image", "inspect", config["image"]], log, capture=True,
        ))[0]
        identity = {
            "id": image["Id"],
            "digests": image.get("RepoDigests", []),
            "platform": "linux/amd64",
        }
        run([
            runtime, "run", "--detach", "--name", preparation, *isolation,
            config["image"], "sleep", "infinity",
        ], log)
        active = preparation
        run([runtime, "exec", preparation, "mkdir", "/work"], log)
        run([runtime, "cp", f"{workspace}/.", f"{preparation}:/work"], log)
        run([
            runtime, "exec", preparation, "dnf", "--repo=fedora", "--repo=updates",
            "--setopt=fedora.skip_if_unavailable=False",
            "--setopt=updates.skip_if_unavailable=False",
            "--setopt=install_weak_deps=False",
            "--setopt=gpgcheck=True",
            "install", "-y", "python3", "rpm-build", "redhat-rpm-config",
            "cmake-rpm-macros", "shadow-utils",
        ], log)
        try:
            run([runtime, "exec", preparation, "python3", "/work/container.py", "prepare"], log)
        finally:
            run([runtime, "cp", f"{preparation}:/work/artifacts/.", str(output)], log)
        result = json.loads((output / "result.json").read_text())
        if result["status"] != "prepared":
            return result | {"image": identity}
        snapshot = run([runtime, "commit", preparation], log, capture=True)
        snapshots.append(snapshot)
        if (workspace / "cpptrace-release.json").exists():
            run([
                runtime, "run", "--detach", "--name", dependency_builder, *isolation,
                "--network=none", "--cap-drop=ALL", "--user=1000:1000",
                "--env=HOME=/home/builder", snapshot, "sleep", "infinity",
            ], log)
            active = dependency_builder
            try:
                run([runtime, "exec", dependency_builder, "python3", "/work/container.py", "build-cpptrace"], log)
            finally:
                run([runtime, "cp", f"{dependency_builder}:/work/artifacts/.", str(output)], log)
            result = json.loads((output / "result.json").read_text())
            if result["status"] != "cpptrace-validated":
                return result | {"image": identity}
            dependency = result["cpptrace"]
            run([
                runtime, "run", "--detach", "--name", installer, *isolation,
                "--network=none", "--user=0:0", snapshot, "sleep", "infinity",
            ], log)
            active = installer
            for filename in ["cpptrace-result.json", *dependency["runtime_rpms"]]:
                if Path(filename).name != filename:
                    raise GateError("build-failed", "Unsafe dependency artifact path")
                if filename.endswith(".rpm") and sha256(output / filename) != dependency["rpm_sha256"][filename]:
                    raise GateError("build-failed", "cpptrace RPM changed before installation")
                run([runtime, "cp", str(output / filename), f"{installer}:/work/artifacts/{filename}"], log)
            try:
                run([runtime, "exec", installer, "python3", "/work/container.py", "install-cpptrace"], log)
            finally:
                run([runtime, "cp", f"{installer}:/work/artifacts/.", str(output)], log)
            result = json.loads((output / "result.json").read_text())
            if result["status"] != "cpptrace-installed":
                return result | {"image": identity}
            snapshot = run([runtime, "commit", installer], log, capture=True)
            snapshots.append(snapshot)
        # Fresh network namespace, no network devices other than loopback,
        # no capabilities, no mounts, and no build/publishing credentials.
        run([
            runtime, "run", "--detach", "--name", builder, *isolation,
            "--network=none", "--cap-drop=ALL", "--user=1000:1000",
            "--env=HOME=/home/builder", snapshot, "sleep", "infinity",
        ], log)
        active = builder
        try:
            run([runtime, "exec", builder, "python3", "/work/container.py", "build"], log)
        finally:
            run([runtime, "cp", f"{builder}:/work/artifacts/.", str(output)], log)
        result = json.loads((output / "result.json").read_text())
        if "cpptrace" in result:
            # The clean install/build image contains only the prerequisite
            # runtime/devel RPMs; the host retains its full RPM/SRPM artifacts.
            result["rpm_sha256"].update(result["cpptrace"]["rpm_sha256"])
        return result | {"image": identity}
    except subprocess.CalledProcessError as error:
        path = output / "result.json"
        if path.exists():
            result = json.loads(path.read_text())
        if result.get("status") not in {"build-failed", "infrastructure-failed"}:
            result = {
                "status": "build-failed" if active in [builder, dependency_builder] else "infrastructure-failed",
                "message": str(error),
            }
        return result | ({"image": identity} if "identity" in locals() else {})
    finally:
        # Only UUID-named resources created by this invocation are removed.
        names = [builder, preparation]
        if (workspace / "cpptrace-release.json").exists():
            names.extend([dependency_builder, installer])
        for name in names:
            subprocess.run(
                [runtime, "rm", "--force", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        for snapshot in reversed(snapshots):
            subprocess.run(
                [runtime, "image", "rm", snapshot],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


def check(project, output, tag=None, force=False, verify_tracked=False):
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "status": "infrastructure-failed",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "build_inputs_sha256": inputs_hash(project),
    }
    try:
        config = load_config(project)
        state = load_state(project)
        release = select_release(config["upstream"], tag)
        report["release"] = release
        inputs_changed = bool(state and state["build_inputs_sha256"] != report["build_inputs_sha256"])
        if not should_build(release, state, force=force or verify_tracked or inputs_changed):
            report.update(status="unchanged", message="The stable release is already validated")
            return report
        if verify_tracked and (
            not state or release["commit"] != state["release"]["commit"]
        ):
            raise GateError("discovery-blocked", "Publishing requires the tracked validated release")
        with tempfile.TemporaryDirectory(prefix="qscopr-") as temporary:
            workspace = Path(temporary)
            archive = workspace / "source.tar.gz"
            download(release, archive)
            source = extract_source(archive, workspace / "source")
            release["source_root"] = source.name
            inventory = discover(source)
            cpptrace_release, cpptrace_inventory = prepare_cpptrace(config["cpptrace"], inventory, workspace)
            report["cpptrace_release"] = cpptrace_release
            inventory["cpptrace"] = cpptrace_inventory
            inventory["repository_requirements"] = repository_requirements(inventory)
            inventory["pipeline_provides"] = {
                "cmake(cpptrace)": cpptrace_release,
            }
            write_json(output / "inventory.json", inventory)
            report["inventory_sha256"] = sha256(output / "inventory.json")
            rpm_release = 1
            if state and state["release"]["version"] == release["version"]:
                rpm_release = state["rpm_release"] + (0 if verify_tracked else 1)
            report["rpm_release"] = rpm_release
            report["cpptrace_rpm_release"] = config["cpptrace"]["rpm_release"]
            if state and state.get("cpptrace_release", {}).get("version") == cpptrace_release["version"]:
                previous = state["cpptrace_rpm_release"]
                report["cpptrace_rpm_release"] = previous if verify_tracked else max(
                    config["cpptrace"]["rpm_release"], previous + 1,
                )
            cpptrace_spec = render_cpptrace_spec(
                cpptrace_release, cpptrace_inventory, report["cpptrace_rpm_release"],
            )
            (output / "cpptrace.spec").write_text(cpptrace_spec)
            report["cpptrace_spec_sha256"] = sha256(output / "cpptrace.spec")
            spec = render_spec(release, inventory, config, rpm_release)
            (output / "candidate.spec").write_text(spec)
            report["spec_sha256"] = sha256(output / "candidate.spec")
            if verify_tracked:
                if (
                    report["build_inputs_sha256"] != state["build_inputs_sha256"]
                    or report["spec_sha256"] != state["spec_sha256"]
                    or release != state["release"]
                    or cpptrace_release != state.get("cpptrace_release")
                    or report["cpptrace_spec_sha256"] != state.get("cpptrace_spec_sha256")
                ):
                    raise GateError("discovery-blocked", "Tracked inputs changed; create a fresh validated update")
            shutil.copyfile(project / "qscopr" / "container.py", workspace / "container.py")
            shutil.copyfile(output / "candidate.spec", workspace / "candidate.spec")
            shutil.copyfile(output / "cpptrace.spec", workspace / "cpptrace.spec")
            (workspace / "cpptrace-smoke.cpp").write_text(SMOKE_SOURCE)
            write_json(workspace / "inventory.json", inventory)
            write_json(workspace / "release.json", release)
            write_json(workspace / "cpptrace-release.json", cpptrace_release)
            write_json(workspace / "build-options.json", {"jobs": config["jobs"]})
            # Source discovery was on the host; don't copy the expanded source
            # (or any files from the user's checkout) into the container.
            shutil.rmtree(source.parent)
            shutil.rmtree(workspace / "cpptrace-source")
            report.update(container_gate(project, workspace, output, config))
            environment = output / "environment.json"
            if environment.exists():
                report["environment"] = json.loads(environment.read_text())
            if verify_tracked and report["status"] == "validated":
                if report["environment"]["dependency_fingerprint"] != state["environment"]["dependency_fingerprint"]:
                    raise GateError("discovery-blocked", "Fedora dependencies changed; revalidate and promote a rebuild first")
            return report
    except GateError as error:
        report.update(status=error.status, message=str(error))
        return report
    except Exception as error:
        report.update(status="infrastructure-failed", message=f"{type(error).__name__}: {error}")
        return report
    finally:
        write_json(output / "gate.json", report)


def verify_report(project, output):
    report = json.loads((output / "gate.json").read_text())
    if report["status"] != "validated":
        raise GateError("discovery-blocked", "Only a validated build can be promoted or published")
    if report["build_inputs_sha256"] != inputs_hash(project):
        raise GateError("discovery-blocked", "Build inputs changed since validation")
    if sha256(output / "candidate.spec") != report["spec_sha256"]:
        raise GateError("discovery-blocked", "Candidate spec changed since validation")
    if sha256(output / "inventory.json") != report["inventory_sha256"]:
        raise GateError("discovery-blocked", "Dependency inventory changed since validation")
    if "cpptrace_spec_sha256" in report:
        if sha256(output / "cpptrace.spec") != report["cpptrace_spec_sha256"]:
            raise GateError("discovery-blocked", "cpptrace spec changed since validation")
        dependency = report.get("cpptrace")
        if not dependency or dependency["srpm"] not in report["rpm_sha256"]:
            raise GateError("discovery-blocked", "Validated cpptrace artifacts are required")
    if not report.get("rpm_sha256") or not report.get("srpm"):
        raise GateError("discovery-blocked", "Validated RPM artifacts are required")
    for name, expected in report["rpm_sha256"].items():
        if Path(name).name != name or sha256(output / name) != expected:
            raise GateError("discovery-blocked", "RPM artifact checksum mismatch")
    if report["srpm"] not in report["rpm_sha256"]:
        raise GateError("discovery-blocked", "Missing SRPM checksum")
    return report


def promote(project, output):
    report = verify_report(project, output)
    previous = load_state(project)
    should_build(report["release"], previous, force=True)
    if (
        previous
        and report["release"]["version"] == previous["release"]["version"]
        and report["rpm_release"] <= previous["rpm_release"]
    ):
        raise GateError("discovery-blocked", "Rebuild must increase RPM Release")
    packaging = project / "packaging"
    packaging.mkdir(exist_ok=True)
    shutil.copyfile(output / "candidate.spec", packaging / "quickshell.spec")
    if "cpptrace_spec_sha256" in report:
        shutil.copyfile(output / "cpptrace.spec", packaging / "cpptrace.spec")
    write_json(packaging / "validated.json", report)
    shutil.copyfile(output / "inventory.json", packaging / "dependencies.json")


def publication(project, output):
    report = verify_report(project, output)
    state = load_state(project)
    if not state or any(report[key] != state[key] for key in [
        "release", "rpm_release", "spec_sha256", "build_inputs_sha256",
    ]):
        raise GateError("discovery-blocked", "Publication does not match the tracked validation")
    if sha256(project / "packaging" / "quickshell.spec") != report["spec_sha256"]:
        raise GateError("discovery-blocked", "Maintained spec was modified")
    if "cpptrace_spec_sha256" in report:
        if (
            sha256(project / "packaging" / "cpptrace.spec") != report["cpptrace_spec_sha256"]
            or state.get("cpptrace_release") != report["cpptrace_release"]
            or state.get("cpptrace_spec_sha256") != report["cpptrace_spec_sha256"]
        ):
            raise GateError("discovery-blocked", "Maintained cpptrace validation differs")
    if report["environment"]["dependency_fingerprint"] != state["environment"]["dependency_fingerprint"]:
        raise GateError("discovery-blocked", "Publication dependency fingerprint differs")
    config = load_config(project)
    if not config["copr"]["project"]:
        raise GateError("discovery-blocked", "Configure copr.project before publishing")
    manifest = {
        "name": "quickshell",
        "project": config["copr"]["project"],
        "chroot": "fedora-44-x86_64",
        "srpm": report["srpm"],
        "sha256": report["rpm_sha256"][report["srpm"]],
        "version": report["release"]["version"],
        "rpm_release": report["rpm_release"],
    }
    if "cpptrace" in report:
        dependency = report["cpptrace"]
        manifest["prerequisites"] = [{
            "name": "cpptrace",
            "project": config["copr"]["project"],
            "chroot": "fedora-44-x86_64",
            "srpm": dependency["srpm"],
            "sha256": report["rpm_sha256"][dependency["srpm"]],
            "version": report["cpptrace_release"]["version"],
            "rpm_release": report["cpptrace_rpm_release"],
        }]
    write_json(output / "publication.json", manifest)
