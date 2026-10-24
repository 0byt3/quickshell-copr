"""Conservative inventory, not a general CMake interpreter.

Scan every CMake file (including optional paths) to avoid losing dependencies
behind conditionals. This can over-approximate requirements. CMake/rpmbuild is
the final authority on both API compatibility and conditional dependencies.
"""

import re
import shlex
from dataclasses import asdict, dataclass

from .source import GateError


PACKAGES = {
    "CLI11": "cmake(CLI11)",
    "PkgConfig": "pkgconf-pkg-config",
    # FindWaylandScanner is a Qt-provided CMake find-module, not an RPM
    # cmake(WaylandScanner) capability. Fedora 44 has Qt >= 6.10.
    "WaylandScanner": "cmake(Qt6WaylandClient) >= 6.10",
    "VulkanHeaders": "cmake(VulkanHeaders)",
    "XCB": "pkgconfig(xcb)",
    "cpptrace": "cmake(cpptrace)",
}
PKGCONFIG = {
    "jemalloc",
    "libdrm",
    "libpipewire-0.3",
    "glib-2.0",
    "gobject-2.0",
    "polkit-agent-1",
    "polkit-gobject-1",
    "wayland-client",
    "wayland-protocols",
    "gbm",
    "egl",
    "xcb",
    "pam",
    "breakpad",
}
QT_COMPONENTS = {
    "Core",
    "Gui",
    "Qml",
    "Quick",
    "QuickControls2",
    "Widgets",
    "ShaderTools",
    "QuickPrivate",
    "Network",
    "WaylandClient",
    "WaylandClientPrivate",
    "DBus",
    "Test",
}
# Documentation-only/tool/private-header requirements. CMake may not discover
# programs used in custom build commands, so BUILD.md is checked independently.
DOCUMENTED = {
    "cmake": "cmake",
    "qt6base": "cmake(Qt6Core)",
    "qt6declarative": "qt6-qtdeclarative-private-devel",
    "libdrm": "pkgconfig(libdrm)",
    "qtshadertools": "cmake(Qt6ShaderTools)",
    "spirv-tools": "spirv-tools",
    "pkg-config": "pkgconf-pkg-config",
    "cli11": "cmake(CLI11)",
    "qt6wayland": "cmake(Qt6WaylandClientPrivate) >= 6.10",
    "qt6svg": "qt6-qtsvg",
    "qt6dbus": "cmake(Qt6DBus)",
    "cpptrace": "cmake(cpptrace)",
    "jemalloc": "pkgconfig(jemalloc)",
    "wayland": "pkgconfig(wayland-client)",
    "wayland-scanner": "wayland-devel",
    "wayland-protocols": "pkgconfig(wayland-protocols)",
    "libgbm": "pkgconfig(gbm)",
    "vulkan-headers": "vulkan-headers",
    "libxcb": "pkgconfig(xcb)",
    "libpipewire": "pkgconfig(libpipewire-0.3)",
    "pam": "pam-devel",
    "polkit": "pkgconfig(polkit-agent-1)",
    "glib": "pkgconfig(glib-2.0)",
    "breakpad": "pkgconfig(breakpad)",
}


@dataclass(frozen=True, order=True)
class Requirement:
    capability: str
    source: str
    reason: str


def blocked(location, message):
    raise GateError("discovery-blocked", f"{location}: {message}")


def commands(text, location):
    """Tokenize balanced CMake commands, respecting comments and quoted strings."""
    token = re.compile(
        r'(?P<space>\s+)|(?P<comment>\#\[(?P<ceq>=*)\[.*?\](?P=ceq)\]|\#[^\n]*)'
        r'|(?P<bracket>\[(?P<eq>=*)\[.*?\](?P=eq)\])'
        r'|(?P<string>"(?:\\.|[^"\\])*")'
        r"|(?P<word>[A-Za-z_][A-Za-z_0-9]*)"
        r"|(?P<other>.)",
        re.S,
    )
    tokens = [match for match in token.finditer(text) if not (match["space"] or match["comment"])]
    index = 0
    while index < len(tokens):
        name = tokens[index]
        if not name["word"] or index + 1 >= len(tokens) or tokens[index + 1].group() != "(":
            blocked(location, f"Unsupported CMake syntax near {name.group()!r}")
        start = tokens[index + 1].end()
        depth = 1
        end_index = index + 2
        while end_index < len(tokens):
            current = tokens[end_index]
            if current["other"] and current.group() == "(":
                depth += 1
            elif current["other"] and current.group() == ")":
                depth -= 1
                if depth == 0:
                    break
            end_index += 1
        if depth:
            blocked(location, "Unbalanced CMake command")
        body = text[start:tokens[end_index].start()]
        # Remove comments only outside strings.
        body = token.sub(lambda m: " " if m["comment"] else m.group(), body)
        line = text.count("\n", 0, name.start()) + 1
        yield name.group().lower(), body, f"{location}:{line}"
        index = end_index + 1


def words(body, location):
    try:
        return shlex.split(body, posix=True)
    except ValueError as error:
        blocked(location, str(error))


def constraint(name, version, location):
    if not re.fullmatch(r"\d+(?:\.\d+)*", version):
        blocked(location, f"Unsupported version constraint: {version}")
    return f"{name} >= {version}"


def discover(root):
    requirements = set()
    declarations = []
    features = {}
    qt_variables = {}
    source_dependencies = {}
    files = sorted(set(root.rglob("CMakeLists.txt")) | set(root.rglob("*.cmake")))
    if not (root / "CMakeLists.txt").is_file() or not (root / "BUILD.md").is_file():
        blocked("source", "CMakeLists.txt and BUILD.md are required")
    parsed = []
    for path in files:
        parsed.extend(commands(path.read_text(), str(path.relative_to(root))))

    def add(capability, location, reason):
        requirements.add(Requirement(capability, location, reason))

    # Source-declared Qt lists are expanded, conservatively across branches.
    for name, body, location in parsed:
        args = words(body, location)
        if name in {"set", "list"}:
            if name == "list" and args[:1] == ["APPEND"]:
                args = args[1:]
            if args and args[0] in {"QT_FPDEPS", "QT_PRIVDEPS"}:
                for component in args[1:]:
                    if component not in QT_COMPONENTS:
                        blocked(location, f"Unknown Qt component/list expression: {component}")
                    qt_variables.setdefault(args[0], set()).add(component)
        if name in {"boption", "option"} and len(args) >= 3:
            if name == "option" and args == ["${VAR}", "${NAME}", "${DEFAULT}"]:
                continue
            default = args[2]
            if default not in {"ON", "OFF", "${ASAN}"}:
                blocked(location, f"Unsupported feature default: {default}")
            features.setdefault(args[0], []).append({
                "default": default,
                "source": location,
            })
        if name == "cmake_minimum_required":
            if len(args) != 2 or args[0] != "VERSION":
                blocked(location, "Unsupported CMake minimum-version declaration")
            add(constraint("cmake", args[1], location), location, "CMake minimum version")
        if name == "qt_standard_project_setup":
            if args[:1] != ["REQUIRES"] or len(args) != 2:
                blocked(location, "Unsupported Qt minimum-version declaration")
            add(constraint("cmake(Qt6Core)", args[1], location), location, "Qt minimum version")

    for name, body, location in parsed:
        args = words(body, location)
        if name in {"find_package", "pkg_check_modules"}:
            declarations.append({
                "command": name,
                "arguments": args,
                "source": location,
            })
        if name == "find_package":
            if not args:
                blocked(location, "Empty find_package")
            package, *tail = args
            version = None
            if tail and re.match(r"\d", tail[0]):
                version = tail.pop(0)
            allowed = {"REQUIRED", "CONFIG", "MODULE", "QUIET", "COMPONENTS"}
            if package == "Qt6":
                components = []
                if "COMPONENTS" not in tail:
                    blocked(location, "Qt6 must declare components")
                head = tail[:tail.index("COMPONENTS")]
                if any(item not in allowed for item in head):
                    blocked(location, "Unsupported Qt package modifier")
                for item in tail[tail.index("COMPONENTS") + 1:]:
                    variable = re.fullmatch(r"\$\{(QT_FPDEPS|QT_PRIVDEPS)\}", item)
                    if variable:
                        if variable[1] not in qt_variables:
                            blocked(location, f"Undefined Qt list: {item}")
                        components.extend(qt_variables[variable[1]])
                    elif item in QT_COMPONENTS:
                        components.append(item)
                    else:
                        blocked(location, f"Unknown Qt component: {item}")
                for component in components:
                    capability = f"cmake(Qt6{component})"
                    if version:
                        capability = constraint(capability, version, location)
                    add(capability, location, "Qt component (all branches inventoried)")
            elif package in PACKAGES:
                # XCB's single COMPONENTS XCB is explicitly supported.
                if package == "XCB" and tail[-2:] == ["COMPONENTS", "XCB"]:
                    tail = tail[:-2]
                if any(item not in allowed - {"COMPONENTS"} for item in tail):
                    blocked(location, f"Unsupported package modifiers: {tail}")
                capability = PACKAGES[package]
                if version:
                    capability = constraint(capability, version, location)
                add(capability, location, "CMake package")
            else:
                blocked(location, f"Unmapped CMake dependency: {package}")
        elif name == "pkg_check_modules":
            if len(args) < 2:
                blocked(location, "Incomplete pkg_check_modules")
            modules = [item for item in args[1:] if item not in {
                "REQUIRED", "QUIET", "IMPORTED_TARGET", "GLOBAL", "NO_CMAKE_PATH",
                "NO_CMAKE_ENVIRONMENT_PATH",
            }]
            for module in modules:
                match = re.fullmatch(r"([A-Za-z0-9_.+-]+)(?:(>=|<=|=|>|<)(\d+(?:\.\d+)*))?", module)
                if not match or match[1] not in PKGCONFIG:
                    blocked(location, f"Unmapped pkg-config dependency: {module}")
                capability = f"pkgconfig({match[1]})"
                if match[2]:
                    capability += f" {match[2]} {match[3]}"
                add(capability, location, "pkg-config module")
        elif name.startswith("find_") or name in {
            "pkg_search_module", "externalproject_add", "cpmaddpackage",
        }:
            blocked(location, f"Unsupported dependency discovery command: {name}")
        elif name.startswith("fetchcontent_"):
            # The sole supported FetchContent declaration belongs to the disabled
            # upstream cpptrace fallback. All FetchContent is disconnected at build.
            if not location.startswith("src/crash/CMakeLists.txt:"):
                blocked(location, "Unexpected FetchContent use")
            if name == "fetchcontent_declare":
                if len(args) != 5 or args[:3] != [
                    "cpptrace", "GIT_REPOSITORY", "https://github.com/jeremy-rifkin/cpptrace.git",
                ] or args[3] != "GIT_TAG" or not re.fullmatch(r"v\d+\.\d+\.\d+", args[4]):
                    blocked(location, "Unexpected vendored dependency")
                source_dependencies["cpptrace"] = {
                    "repository": "jeremy-rifkin/cpptrace",
                    "tag": args[4],
                    "source": location,
                }

    in_dependencies = False
    for line_number, line in enumerate((root / "BUILD.md").read_text().splitlines(), 1):
        if line.startswith("## "):
            in_dependencies = line.strip() == "## Dependencies"
        if in_dependencies and re.match(r"\s*[-*]\s+`", line):
            dependency = re.search(r"`([^`]+)`", line)[1]
            if re.fullmatch(r"D?[A-Z][A-Z0-9_]*=(?:OFF|ON)", dependency):
                continue
            if dependency not in DOCUMENTED:
                blocked(f"BUILD.md:{line_number}", f"Unmapped documented dependency: {dependency}")
            add(DOCUMENTED[dependency], f"BUILD.md:{line_number}", "Documented dependency")
        if in_dependencies and line.startswith("Dependencies:"):
            for dependency in re.findall(r"`([^`]+)`", line):
                if dependency not in DOCUMENTED:
                    blocked(f"BUILD.md:{line_number}", f"Unmapped documented dependency: {dependency}")
                add(DOCUMENTED[dependency], f"BUILD.md:{line_number}", "Documented feature dependency")
    # Toolchain/private headers/PAM aren't all represented by find_package.
    for capability in [
        "gcc-c++",
        "ninja-build",
        "cmake-rpm-macros",
        "qt6-qtbase-private-devel",
        "qt6-qtdeclarative-private-devel",
        "pam-devel",
        "spirv-tools",
    ]:
        add(capability, "policy", "Packaging toolchain / private headers / direct linkage")
    if not declarations:
        blocked("source", "No dependency declarations found")
    return {
        "requirements": [asdict(item) for item in sorted(requirements)],
        "declarations": declarations,
        "features": features,
        "source_dependencies": source_dependencies,
        "policy": "Conservative union of conditional requirements; upstream defaults; no vendoring",
    }
