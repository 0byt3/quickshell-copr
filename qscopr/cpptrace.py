"""Reviewed cpptrace v1.0.4 profile, independent of Quickshell's vendoring."""

from .discovery import commands, words
from .source import GateError, download, extract_source, select_release


PROFILE = {
    "repository": "jeremy-rifkin/cpptrace",
    "tag": "v1.0.4",
    "commit": "3db8da80111171c219ab5839905771386bee06b3",
    "sha256": "77d689fd7956ff80351a079d83e86a03865dbbe2433b4559cc6cea50bed77390",
}
FLAGS = [
    "-DCPPTRACE_BUILD_SHARED=ON",
    "-DCPPTRACE_UNWIND_WITH_LIBUNWIND=ON",
    "-DCPPTRACE_GET_SYMBOLS_WITH_LIBDWARF=ON",
    "-DCPPTRACE_USE_EXTERNAL_LIBDWARF=ON",
    "-DCPPTRACE_FIND_LIBDWARF_WITH_PKGCONFIG=ON",
    "-DCPPTRACE_USE_EXTERNAL_ZSTD=ON",
    "-DCPPTRACE_BUILD_TESTING=ON",
    "-DCPPTRACE_USE_EXTERNAL_GTEST=ON",
    "-DCPPTRACE_BUILD_TOOLS=OFF",
    "-DCPPTRACE_BUILD_BENCHMARKING=OFF",
    "-DCPPTRACE_DISABLE_CXX_20_MODULES=ON",
    "-DFETCHCONTENT_FULLY_DISCONNECTED=ON",
    "-DFETCHCONTENT_UPDATES_DISCONNECTED=ON",
]
SMOKE_SOURCE = """#include <cpptrace/cpptrace.hpp>
#include <iostream>
int main() {
    if (!cpptrace::can_signal_safe_unwind()) {
        return 1;
    }
    cpptrace::frame_ptr frames[32];
    if (cpptrace::safe_generate_raw_trace(frames, 32) == 0) {
        return 2;
    }
    auto trace = cpptrace::generate_trace();
    if (trace.frames.empty()) {
        return 3;
    }
    std::cout << "cpptrace signal-safe unwinding and symbol resolution passed\\n";
    return 0;
}
"""


def validate_profile(config, upstream_pin):
    if any(config.get(key) != value for key, value in PROFILE.items()):
        raise GateError("discovery-blocked", "cpptrace source differs from the reviewed v1.0.4 profile")
    if not upstream_pin or any(upstream_pin[key] != PROFILE[key] for key in ["repository", "tag"]):
        raise GateError("discovery-blocked", "Quickshell's cpptrace pin changed; review the dependency profile")
    if type(config.get("rpm_release")) is not int or config["rpm_release"] < 1:
        raise GateError("discovery-blocked", "cpptrace.rpm_release must be a positive integer")


def prepare_cpptrace(config, inventory, workspace):
    validate_profile(config, inventory.get("source_dependencies", {}).get("cpptrace"))
    release = select_release(config["repository"], config["tag"])
    if release["commit"] != config["commit"]:
        raise GateError("discovery-blocked", "cpptrace release tag moved")
    archive = workspace / "cpptrace-source.tar.gz"
    download(release, archive)
    if release["sha256"] != config["sha256"]:
        raise GateError("discovery-blocked", "cpptrace source archive checksum mismatch")
    root = extract_source(archive, workspace / "cpptrace-source")
    release["source_root"] = root.name
    return release, inventory_cpptrace(root)


def inventory_cpptrace(root):
    """The immutable reviewed profile defines active branches, not arbitrary CMake."""
    declarations = []
    files = sorted(set(root.rglob("CMakeLists.txt")) | set(root.rglob("*.cmake")))
    for path in files:
        for name, body, location in commands(path.read_text(), str(path.relative_to(root))):
            if name in ["find_package", "pkg_check_modules", "find_library", "find_path", "find_program"]:
                declarations.append({
                    "command": name,
                    "arguments": words(body, location),
                    "source": location,
                })
    requirements = [
        ("cmake >= 3.15", "CMakeLists.txt", "CMake minimum version"),
        ("gcc-c++", "policy", "C/C++ compiler and C++ ABI"),
        ("ninja-build", "policy", "RPM build generator"),
        ("cmake-rpm-macros", "policy", "RPM build and test macros"),
        ("pkgconf-pkg-config", "CMakeLists.txt", "External dependency discovery"),
        ("pkgconfig(libunwind)", "CMakeLists.txt", "Required signal-safe unwinding backend"),
        ("pkgconfig(libdwarf) >= 2.1", "CMakeLists.txt", "External DWARF backend; conservative upstream 2.1 baseline"),
        ("pkgconfig(libzstd)", "cmake/in/cpptrace-config-cmake.in", "Installed CMake consumer dependency"),
        ("pkgconfig(zlib)", "policy", "System compression support"),
        ("cmake(GTest)", "test/CMakeLists.txt", "Upstream tests using system GTest"),
        ("gmock-devel", "test/CMakeLists.txt", "Upstream tests using system GMock"),
    ]
    return {
        "requirements": [
            {
                "capability": capability,
                "source": source,
                "reason": reason,
            }
            for capability, source, reason in requirements
        ],
        "declarations": declarations,
        "cmake_flags": FLAGS,
        "policy": "Reviewed immutable v1.0.4 profile; external Fedora libraries and tests; no fetching",
    }


def repository_requirements(inventory):
    """Only the explicitly reviewed cpptrace capability is supplied by this pipeline."""
    capabilities = set()
    for requirement in inventory["requirements"]:
        capability = requirement["capability"]
        if capability != "cmake(cpptrace)":
            capabilities.add(capability)
    for requirement in inventory["cpptrace"]["requirements"]:
        capabilities.add(requirement["capability"])
    return sorted(capabilities)
