"""Copied into the disposable Fedora container; uses only the standard library."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


WORK = Path("/work")
ARTIFACTS = WORK / "artifacts"
DNF = [
    "dnf",
    "--repo=fedora",
    "--repo=updates",
    "--setopt=fedora.skip_if_unavailable=False",
    "--setopt=updates.skip_if_unavailable=False",
    "--setopt=install_weak_deps=False",
    "--setopt=gpgcheck=True",
]


def write_json(path, value):
    path.write_text(json.dumps(value, indent=4, sort_keys=True) + "\n")


def execute(command, log_name, capture=False):
    print("+", " ".join(command), flush=True)
    # stderr is deliberately included in the durable stage log.
    with (ARTIFACTS / log_name).open("a") as log:
        if capture:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=log, text=True)
            log.write(result.stdout)
            result.check_returncode()
            return result.stdout
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        for line in process.stdout:
            log.write(line)
            log.flush()
            print(line, end="", flush=True)
        code = process.wait()
    if code:
        raise subprocess.CalledProcessError(code, command)
    return ""


def prepare():
    inventory = json.loads((WORK / "inventory.json").read_text())
    requirements = inventory.get(
        "repository_requirements",
        sorted({item["capability"] for item in inventory["requirements"]}),
    )
    execute(["rpmspec", "--parse", str(WORK / "candidate.spec")], "spec-expanded.log", capture=True)
    if (WORK / "cpptrace.spec").exists():
        execute(["rpmspec", "--parse", str(WORK / "cpptrace.spec")], "cpptrace-spec-expanded.log", capture=True)
    execute(DNF + ["makecache", "--refresh"], "repositories.log")
    execute(DNF + ["repolist", "--enabled"], "repositories.log")
    metadata = {}
    for base in [Path("/var/cache/libdnf5"), Path("/var/cache/dnf")]:
        if base.exists():
            for path in sorted(base.rglob("repomd.xml")):
                metadata[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(ARTIFACTS / "repository-metadata.json", metadata)
    shutil.copytree("/etc/yum.repos.d", ARTIFACTS / "repository-definitions")
    providers = {}
    missing = []
    for requirement in requirements:
        result = execute(
            DNF + [
                "repoquery",
                "--available",
                "--arch=x86_64,noarch",
                "--whatprovides", requirement,
                "--queryformat", "%{name}-%{evr}.%{arch} [%{repoid}]\\n",
            ],
            "resolution.log",
            capture=True,
        )
        matches = [line.strip() for line in result.splitlines() if line.strip()]
        providers[requirement] = matches
        if not matches:
            missing.append(requirement)
        print(f"{requirement}: {', '.join(matches) if matches else 'MISSING'}", flush=True)
    write_json(ARTIFACTS / "providers.json", providers)
    if missing:
        write_json(ARTIFACTS / "result.json", {
            "status": "dependency-blocked",
            "missing": missing,
            "message": "Official Fedora 44 repositories lack required capabilities",
        })
        return
    # Resolve/install the whole transaction. A solver failure cannot permit a build.
    try:
        execute(DNF + ["install", "-y", *requirements], "transaction.log")
    except subprocess.CalledProcessError:
        transaction = (ARTIFACTS / "transaction.log").read_text()
        if any(marker in transaction for marker in [
            "conflicting requests",
            "nothing provides",
            "cannot install both",
            "Problem:",
        ]):
            write_json(ARTIFACTS / "result.json", {
                "status": "dependency-blocked",
                "message": "Fedora packages exist individually but cannot satisfy the complete transaction",
            })
            return
        raise
    record_environment()
    execute(["useradd", "--create-home", "--uid", "1000", "builder"], "prepare.log")
    write_json(ARTIFACTS / "result.json", {
        "status": "prepared",
        "message": "All repository dependency capabilities resolved and installed",
    })
    execute(["chown", "-R", "builder:builder", str(WORK)], "prepare.log")


def record_environment():
    packages = execute(
        ["rpm", "-qa", "--qf", "%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}\\n"],
        "packages.log",
        capture=True,
    )
    package_list = sorted(packages.splitlines())
    fingerprint = hashlib.sha256("\n".join(package_list).encode()).hexdigest()
    write_json(ARTIFACTS / "environment.json", {
        "packages": package_list,
        "dependency_fingerprint": fingerprint,
        "repositories": [
            "fedora",
            "updates",
        ],
        "fedora": 44,
        "architecture": "x86_64",
    })


def build_package(name, release, source, spec):
    if os.getuid() == 0:
        raise RuntimeError("Refusing to compile as root")
    options = json.loads((WORK / "build-options.json").read_text())
    if hashlib.sha256(source.read_bytes()).hexdigest() != release["sha256"]:
        raise RuntimeError("Source checksum mismatch")
    top = WORK / ("rpmbuild" if name == "quickshell" else "rpmbuild-cpptrace")
    for directory in ["BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"]:
        (top / directory).mkdir(parents=True)
    shutil.copyfile(source, top / "SOURCES" / f"{name}-{release['commit']}.tar.gz")
    shutil.copyfile(spec, top / "SPECS" / f"{name}.spec")
    execute(
        [
            "rpmbuild", "-ba",
            "--define", f"_topdir {top}",
            "--define", f"_smp_build_ncpus {options['jobs']}",
            "--define", f"_smp_mflags -j{options['jobs']}",
            str(top / "SPECS" / f"{name}.spec"),
        ],
        "rpmbuild.log" if name == "quickshell" else "cpptrace-rpmbuild.log",
    )
    rpms = sorted((top / "RPMS").rglob("*.rpm"))
    srpms = sorted((top / "SRPMS").glob("*.src.rpm"))
    if not rpms or len(srpms) != 1:
        raise RuntimeError("rpmbuild did not produce the expected RPM and SRPM")
    # RPM generation also checks installed/unpackaged files. Retain the output
    # metadata and dependency constraints for inspection (including Qt ABI).
    for rpm in rpms:
        execute(["rpm", "-qp", "--requires", str(rpm)], f"{name}-rpm-requires.log")
        execute(["rpm", "-qpl", str(rpm)], f"{name}-rpm-files.log")
        shutil.copyfile(rpm, ARTIFACTS / rpm.name)
    shutil.copyfile(srpms[0], ARTIFACTS / srpms[0].name)
    hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [*rpms, *srpms]
    }
    return {
        "rpm_sha256": hashes,
        "srpm": srpms[0].name,
    }


def build_cpptrace():
    release = json.loads((WORK / "cpptrace-release.json").read_text())
    result = build_package(
        "cpptrace", release, WORK / "cpptrace-source.tar.gz", WORK / "cpptrace.spec",
    )
    runtime = []
    for filename in result["rpm_sha256"]:
        if filename.endswith(".src.rpm"):
            continue
        name = execute(
            ["rpm", "-qp", "--qf", "%{NAME}", str(ARTIFACTS / filename)],
            "cpptrace-packages.log", capture=True,
        ).strip()
        if name in ["cpptrace", "cpptrace-devel"]:
            runtime.append(filename)
    if len(runtime) != 2:
        raise RuntimeError("Expected cpptrace runtime and devel RPMs")
    result["runtime_rpms"] = sorted(runtime)
    result["release"] = release
    write_json(ARTIFACTS / "cpptrace-result.json", result)
    write_json(ARTIFACTS / "result.json", {
        "status": "cpptrace-validated",
        "stage": "cpptrace-build",
        "cpptrace": result,
        "message": "cpptrace RPM build, upstream tests, and signal-safe smoke check passed",
    })


def install_cpptrace():
    if os.getuid() != 0:
        raise RuntimeError("RPM installation requires the isolated preparation user")
    result = json.loads((ARTIFACTS / "cpptrace-result.json").read_text())
    paths = []
    for filename in result["runtime_rpms"]:
        if Path(filename).name != filename:
            raise RuntimeError("Unsafe cpptrace RPM filename")
        path = ARTIFACTS / filename
        if hashlib.sha256(path.read_bytes()).hexdigest() != result["rpm_sha256"][filename]:
            raise RuntimeError("cpptrace RPM checksum mismatch")
        paths.append(str(path))
    # All transitive dependencies were installed from Fedora in prepare().
    # rpm still verifies the complete local-package dependency transaction.
    execute(["rpm", "-Uvh", *paths], "cpptrace-install.log")
    execute(["ldconfig"], "cpptrace-install.log")
    inventory = json.loads((WORK / "inventory.json").read_text())
    for requirement in inventory["requirements"]:
        providers = execute(
            DNF + [
                "--cacheonly", "repoquery", "--installed",
                "--whatprovides", requirement["capability"],
                "--queryformat", "%{name}-%{evr}.%{arch}\\n",
            ],
            "installed-providers.log", capture=True,
        )
        if not providers.strip():
            raise RuntimeError(f"No installed provider for {requirement['capability']}")
    record_environment()
    write_json(ARTIFACTS / "result.json", {
        "status": "cpptrace-installed",
        "stage": "cpptrace-install",
        "cpptrace": result,
        "message": "Built cpptrace RPMs installed; all Quickshell requirements have installed providers",
    })


def validate_cpptrace_consumer():
    consumer = WORK / "cpptrace-consumer"
    consumer.mkdir()
    (consumer / "main.cpp").write_text((WORK / "cpptrace-smoke.cpp").read_text())
    (consumer / "CMakeLists.txt").write_text(
        "cmake_minimum_required(VERSION 3.20)\n"
        "project(cpptrace_consumer LANGUAGES CXX)\n"
        "find_package(cpptrace 1.0.4 REQUIRED)\n"
        "add_executable(smoke main.cpp)\n"
        "target_compile_features(smoke PRIVATE cxx_std_20)\n"
        "target_link_libraries(smoke PRIVATE cpptrace::cpptrace)\n"
    )
    execute(["cmake", "-S", str(consumer), "-B", str(consumer / "build"), "-GNinja"], "cpptrace-consumer.log")
    execute(["cmake", "--build", str(consumer / "build"), "--parallel", "2"], "cpptrace-consumer.log")
    execute([str(consumer / "build" / "smoke")], "cpptrace-consumer.log")


def build():
    if os.getuid() == 0:
        raise RuntimeError("Refusing to compile as root")
    cpptrace_path = ARTIFACTS / "cpptrace-result.json"
    dependency = json.loads(cpptrace_path.read_text()) if cpptrace_path.exists() else None
    if dependency:
        validate_cpptrace_consumer()
    release = json.loads((WORK / "release.json").read_text())
    result = build_package("quickshell", release, WORK / "source.tar.gz", WORK / "candidate.spec")
    if dependency:
        result["cpptrace"] = dependency
        result["rpm_sha256"].update(dependency["rpm_sha256"])
    write_json(ARTIFACTS / "result.json", result | {
        "status": "validated",
        "stage": "quickshell-build",
        "message": "Offline RPM builds and package checks succeeded",
    })


if __name__ == "__main__":
    ARTIFACTS.mkdir(exist_ok=True)
    stage = sys.argv[1]
    try:
        {
            "prepare": prepare,
            "build-cpptrace": build_cpptrace,
            "install-cpptrace": install_cpptrace,
            "build": build,
        }[stage]()
    except Exception as error:
        result = {
            "status": "build-failed" if stage in ["build", "build-cpptrace"] else "infrastructure-failed",
            "stage": stage,
            "message": str(error),
        }
        dependency = ARTIFACTS / "cpptrace-result.json"
        if dependency.exists():
            result["cpptrace"] = json.loads(dependency.read_text())
        write_json(ARTIFACTS / "result.json", result)
        raise
