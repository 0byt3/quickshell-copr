import tempfile
import unittest
from pathlib import Path

from qscopr.discovery import commands, discover
from qscopr.packaging import render_spec
from qscopr.source import GateError


BASE = """cmake_minimum_required(VERSION 3.20)
project(quickshell VERSION "0.3.2")
set(QT_FPDEPS Gui Qml Quick ShaderTools)
set(QT_PRIVDEPS QuickPrivate)
if(WAYLAND)
    list(APPEND QT_FPDEPS WaylandClient)
    list(APPEND QT_PRIVDEPS WaylandClientPrivate)
endif()
find_package(Qt6 REQUIRED COMPONENTS ${QT_FPDEPS})
if(Qt6_VERSION VERSION_GREATER_EQUAL "6.9.0")
    find_package(Qt6 REQUIRED COMPONENTS ${QT_PRIVDEPS})
endif()
qt_standard_project_setup(REQUIRES 6.6)
find_package(PkgConfig REQUIRED)
pkg_check_modules(wayland REQUIRED IMPORTED_TARGET wayland-client wayland-protocols>=1.41)
"""


class DiscoveryTests(unittest.TestCase):
    def inventory(self, extra="", documentation="## Dependencies\n- `cmake`\n", cmake=BASE):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CMakeLists.txt").write_text(cmake + extra)
            (root / "BUILD.md").write_text(documentation)
            return discover(root)

    def capabilities(self, **kwargs):
        return {item["capability"] for item in self.inventory(**kwargs)["requirements"]}

    def test_version_constraints(self):
        caps = self.capabilities()
        self.assertIn("pkgconfig(wayland-protocols) >= 1.41", caps)
        self.assertIn("cmake(Qt6Core) >= 6.6", caps)
        self.assertIn("cmake >= 3.20", caps)

    def test_conditional_private_qt_inventory(self):
        caps = self.capabilities()
        self.assertIn("cmake(Qt6QuickPrivate)", caps)
        self.assertIn("cmake(Qt6WaylandClientPrivate)", caps)

    def test_fedora44_wayland_packaging(self):
        caps = self.capabilities(
            extra="find_package(WaylandScanner REQUIRED)\n",
            documentation="## Dependencies\n- `qt6wayland` (for Qt before 6.10)\n",
        )
        self.assertIn("cmake(Qt6WaylandClient) >= 6.10", caps)
        self.assertIn("cmake(Qt6WaylandClientPrivate) >= 6.10", caps)
        self.assertNotIn("qt6-qtwayland-private-devel", caps)

    def test_unknown_package_blocks(self):
        with self.assertRaisesRegex(GateError, "Unmapped CMake"):
            self.inventory(extra="find_package(NewLibrary REQUIRED)\n")

    def test_new_pkgconfig_dependency_blocks(self):
        with self.assertRaisesRegex(GateError, "Unmapped pkg-config"):
            self.inventory(extra="pkg_check_modules(new REQUIRED newlib>=1.2)\n")

    def test_unknown_component_blocks(self):
        with self.assertRaisesRegex(GateError, "Unknown Qt"):
            self.inventory(extra="find_package(Qt6 REQUIRED COMPONENTS BrandNew)\n")

    def test_unknown_discovery_command_blocks(self):
        for statement in [
            "find_library(FOO foo)",
            "find_program(TOOL foo)",
            "pkg_search_module(FOO foo)",
            "ExternalProject_Add(newlib URL https://example.com/source.tar.gz)",
        ]:
            with self.subTest(statement=statement), self.assertRaises(GateError):
                self.inventory(extra=statement + "\n")

    def test_version_expression_blocks(self):
        with self.assertRaisesRegex(GateError, "modifiers"):
            self.inventory(extra="find_package(CLI11 ${CLI_VERSION} REQUIRED)\n")

    def test_exact_version_is_not_silently_relaxed(self):
        with self.assertRaises(GateError):
            self.inventory(extra="find_package(CLI11 2.6 EXACT REQUIRED)\n")

    def test_documented_new_dependency_blocks(self):
        with self.assertRaisesRegex(GateError, "documented dependency"):
            self.inventory(documentation="## Dependencies\n- `new-library`\n")

    def test_documented_flags_are_not_dependencies(self):
        caps = self.capabilities(documentation="## Dependencies\n- `DSCREENCOPY_ICC=OFF` - disable\n")
        self.assertIn("cmake", {cap.split(" >=")[0] for cap in caps})

    def test_multiline_and_comments(self):
        result = list(commands('''# find_package(IGNORED)
find_package(
    CLI11 # comment
    REQUIRED
)
message("find_package(NotARequirement) # literal")
''', "CMakeLists.txt"))
        self.assertEqual(len(result), 2)
        self.assertIn("CLI11", result[0][1])

    def test_bracket_comment_and_nested_string(self):
        result = list(commands('''#[=[ find_package(IGNORED) ]=]
message("embedded (quote)")
find_package(CLI11 REQUIRED)
''', "CMakeLists.txt"))
        self.assertEqual([item[0] for item in result], ["message", "find_package"])

    def test_unknown_feature_default_blocks(self):
        with self.assertRaisesRegex(GateError, "feature default"):
            self.inventory(extra='option(NEW "New feature" ${UNKNOWN})\n')

    def test_feature_inventory(self):
        inventory = self.inventory(extra='boption(SERVICE_POLKIT "Polkit" ON)\n')
        self.assertEqual(inventory["features"]["SERVICE_POLKIT"][0]["default"], "ON")

    def test_unexpected_vendoring_blocks(self):
        with self.assertRaisesRegex(GateError, "FetchContent"):
            self.inventory(extra="FetchContent_Declare(lib URL https://example.com/lib.tar.gz)\n")

    def test_spec_uses_same_constraints_and_offline_policy(self):
        release = {
            "version": "0.3.2",
            "commit": "a" * 40,
            "source_root": "quickshell-" + "a" * 40,
            "sha256": "b" * 64,
            "repository": "quickshell-mirror/quickshell",
        }
        spec = render_spec(release, self.inventory(), {"copr": {"distributor": "Fedora COPR (test/project)"}})
        self.assertIn("BuildRequires: pkgconfig(wayland-protocols) >= 1.41", spec)
        self.assertIn("-DVENDOR_CPPTRACE=OFF", spec)
        self.assertIn("-DFETCHCONTENT_FULLY_DISCONNECTED=ON", spec)
        self.assertIn("Release: 1%{?dist}", spec)
        self.assertIn("%check", spec)
