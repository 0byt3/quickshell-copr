import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qscopr.cpptrace import (
    FLAGS,
    PROFILE,
    inventory_cpptrace,
    prepare_cpptrace,
    repository_requirements,
    validate_profile,
)
from qscopr.packaging import render_cpptrace_spec
from qscopr.source import GateError


class CpptraceTests(unittest.TestCase):
    def config(self):
        return PROFILE | {"rpm_release": 1}

    def pin(self):
        return {
            "repository": PROFILE["repository"],
            "tag": PROFILE["tag"],
        }

    def test_reviewed_profile_is_accepted(self):
        validate_profile(self.config(), self.pin())

    def test_unknown_source_or_tag_is_refused(self):
        for field in ["repository", "tag", "commit", "sha256"]:
            with self.subTest(field=field), self.assertRaises(GateError):
                validate_profile(self.config() | {field: "changed"}, self.pin())

    def test_changed_quickshell_dependency_pin_is_refused(self):
        with self.assertRaisesRegex(GateError, "pin changed"):
            validate_profile(self.config(), self.pin() | {"tag": "v2.0.0"})

    def test_missing_quickshell_pin_is_refused(self):
        with self.assertRaises(GateError):
            validate_profile(self.config(), None)

    def test_invalid_rpm_release_is_refused(self):
        for release in [0, -1, True, "1"]:
            with self.subTest(release=release), self.assertRaises(GateError):
                validate_profile(self.config() | {"rpm_release": release}, self.pin())

    @patch("qscopr.cpptrace.select_release")
    def test_moved_dependency_tag_is_refused(self, select):
        select.return_value = {"commit": "f" * 40}
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(GateError, "tag moved"):
            prepare_cpptrace(
                self.config(), {"source_dependencies": {"cpptrace": self.pin()}}, Path(directory),
            )

    @patch("qscopr.cpptrace.download")
    @patch("qscopr.cpptrace.select_release")
    def test_archive_checksum_is_checked(self, select, download):
        release = PROFILE.copy()
        release["sha256"] = "f" * 64
        select.return_value = release
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(GateError, "checksum"):
            prepare_cpptrace(
                self.config(), {"source_dependencies": {"cpptrace": self.pin()}}, Path(directory),
            )

    def test_inventory_records_system_dependencies_and_upstream_tests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "CMakeLists.txt").write_text("find_package(PkgConfig REQUIRED)\n")
            inventory = inventory_cpptrace(root)
        capabilities = {item["capability"] for item in inventory["requirements"]}
        self.assertIn("pkgconfig(libunwind)", capabilities)
        self.assertIn("pkgconfig(libdwarf) >= 2.1", capabilities)
        self.assertIn("cmake(GTest)", capabilities)
        self.assertIn("gmock-devel", capabilities)
        self.assertEqual(inventory["declarations"][0]["source"], "CMakeLists.txt:1")

    def test_only_cpptrace_is_pipeline_supplied(self):
        inventory = {
            "requirements": [
                {"capability": "cmake(cpptrace)"},
                {"capability": "cmake(NewDependency)"},
                {"capability": "pkgconfig(wayland-protocols) >= 1.41"},
            ],
            "cpptrace": {
                "requirements": [
                    {"capability": "pkgconfig(libunwind)"},
                ],
            },
        }
        result = repository_requirements(inventory)
        self.assertNotIn("cmake(cpptrace)", result)
        self.assertIn("cmake(NewDependency)", result)
        self.assertIn("pkgconfig(libunwind)", result)

    def test_versioned_cpptrace_is_not_silently_substituted(self):
        inventory = {
            "requirements": [{"capability": "cmake(cpptrace) >= 2.0"}],
            "cpptrace": {"requirements": []},
        }
        self.assertIn("cmake(cpptrace) >= 2.0", repository_requirements(inventory))

    def test_spec_builds_shared_rpms_and_runs_both_test_layers(self):
        release = PROFILE | {
            "version": "1.0.4",
            "source_root": "cpptrace-" + PROFILE["commit"],
        }
        spec = render_cpptrace_spec(
            release,
            {"requirements": [{"capability": "pkgconfig(libunwind)"}]},
            2,
        )
        self.assertIn("Release: 2%{?dist}", spec)
        self.assertIn("%package devel", spec)
        self.assertIn("%ctest --output-on-failure --no-tests=error", spec)
        self.assertIn("can_signal_safe_unwind", spec)
        self.assertIn("safe_generate_raw_trace", spec)
        for flag in FLAGS:
            self.assertIn(flag, spec)
