import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qscopr.gate import (
    check,
    container_gate,
    inputs_hash,
    load_state,
    promote,
    publication,
    verify_report,
    write_json,
)
from qscopr.source import GateError, sha256


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        (self.project / "config.toml").write_text(
            'fedora = 44\narchitecture = "x86_64"\n'
            'image = "registry.fedoraproject.org/fedora:44"\n'
            'upstream = "owner/quickshell"\njobs = 2\n'
            '[copr]\nproject = "test/quickshell"\ndistributor = "Fedora COPR (test/quickshell)"\n'
        )
        self.output = self.project / "artifacts"
        self.output.mkdir()
        (self.output / "candidate.spec").write_text("Version: 0.3.2\n")
        write_json(self.output / "inventory.json", {"requirements": []})
        (self.output / "quickshell.src.rpm").write_bytes(b"mock SRPM")
        self.report = {
            "status": "validated",
            "release": {
                "version": "0.3.2",
                "tag": "v0.3.2",
                "commit": "a" * 40,
            },
            "rpm_release": 1,
            "spec_sha256": sha256(self.output / "candidate.spec"),
            "inventory_sha256": sha256(self.output / "inventory.json"),
            "build_inputs_sha256": inputs_hash(self.project),
            "rpm_sha256": {
                "quickshell.src.rpm": sha256(self.output / "quickshell.src.rpm"),
            },
            "srpm": "quickshell.src.rpm",
            "environment": {
                "dependency_fingerprint": "test-environment",
            },
        }
        self.save_report()

    def save_report(self):
        write_json(self.output / "gate.json", self.report)

    def test_successful_promotion(self):
        promote(self.project, self.output)
        self.assertEqual(load_state(self.project), self.report)
        self.assertTrue((self.project / "packaging" / "quickshell.spec").is_file())

    def test_blocked_gate_never_promotes(self):
        for status in ["dependency-blocked", "discovery-blocked", "build-failed", "infrastructure-failed"]:
            self.report["status"] = status
            self.save_report()
            with self.subTest(status=status), self.assertRaises(GateError):
                promote(self.project, self.output)
        self.assertIsNone(load_state(self.project))

    def test_spec_tampering_refused(self):
        (self.output / "candidate.spec").write_text("Changed")
        with self.assertRaisesRegex(GateError, "spec changed"):
            verify_report(self.project, self.output)

    def test_inventory_tampering_refused(self):
        (self.output / "inventory.json").write_text("{}")
        with self.assertRaisesRegex(GateError, "inventory changed"):
            verify_report(self.project, self.output)

    def test_source_input_change_refused(self):
        (self.project / "config.toml").write_text("changed")
        with self.assertRaisesRegex(GateError, "inputs changed"):
            verify_report(self.project, self.output)

    def test_srpm_tampering_refused(self):
        (self.output / "quickshell.src.rpm").write_bytes(b"changed")
        with self.assertRaisesRegex(GateError, "checksum"):
            verify_report(self.project, self.output)

    def test_missing_srpm_refused(self):
        self.report["rpm_sha256"] = {}
        self.save_report()
        with self.assertRaises(GateError):
            verify_report(self.project, self.output)

    def test_same_release_cannot_promote_twice(self):
        promote(self.project, self.output)
        with self.assertRaisesRegex(GateError, "increase"):
            promote(self.project, self.output)

    def test_publication_requires_tracked_state(self):
        with self.assertRaisesRegex(GateError, "tracked"):
            publication(self.project, self.output)

    def test_publication_matches_validated_state(self):
        promote(self.project, self.output)
        publication(self.project, self.output)
        manifest = json.loads((self.output / "publication.json").read_text())
        self.assertEqual(manifest["project"], "test/quickshell")
        self.assertEqual(manifest["chroot"], "fedora-44-x86_64")

    def test_changed_dependencies_block_publication(self):
        promote(self.project, self.output)
        self.report["environment"]["dependency_fingerprint"] = "changed"
        self.save_report()
        with self.assertRaisesRegex(GateError, "fingerprint"):
            publication(self.project, self.output)

    def test_edited_maintained_spec_blocks_publication(self):
        promote(self.project, self.output)
        (self.project / "packaging" / "quickshell.spec").write_text("changed")
        with self.assertRaisesRegex(GateError, "Maintained spec"):
            publication(self.project, self.output)

    @patch("qscopr.gate.select_release")
    def test_unchanged_does_not_download_or_build(self, release):
        promote(self.project, self.output)
        release.return_value = self.report["release"].copy()
        with patch("qscopr.gate.download") as download:
            report = check(self.project, self.project / "attempt")
        self.assertEqual(report["status"], "unchanged")
        download.assert_not_called()

    @patch("qscopr.gate.select_release")
    def test_downstream_failure_does_not_advance_state(self, release):
        release.side_effect = GateError("discovery-blocked", "unknown requirement")
        report = check(self.project, self.project / "attempt")
        self.assertEqual(report["status"], "discovery-blocked")
        self.assertIsNone(load_state(self.project))
        self.assertTrue((self.project / "attempt" / "gate.json").exists())

    @patch("qscopr.gate.select_release")
    def test_changed_inputs_trigger_revalidation(self, release):
        promote(self.project, self.output)
        (self.project / "qscopr").mkdir()
        (self.project / "qscopr" / "new.py").write_text("# new input\n")
        release.return_value = self.report["release"].copy()
        with patch("qscopr.gate.download", side_effect=GateError("discovery-blocked", "test stop")) as download:
            report = check(self.project, self.project / "attempt")
        self.assertEqual(report["status"], "discovery-blocked")
        download.assert_called_once()

    @patch("qscopr.gate.subprocess.run")
    @patch("qscopr.gate.shutil.which", side_effect=lambda program: f"/usr/bin/{program}")
    def test_offline_builder_has_no_capabilities_or_host_mounts(self, which, cleanup):
        commands = []

        def run(command, log, capture=False):
            commands.append(command)
            if command[1:3] == ["image", "inspect"]:
                return '[{"Id": "sha256:base", "RepoDigests": []}]'
            if command[1] == "commit":
                return "sha256:prepared"
            if command[1] == "cp" and ":/work/artifacts/." in command[2]:
                status = "prepared" if "qscopr-prepare-" in command[2] else "validated"
                write_json(self.output / "result.json", {"status": status})
            return ""

        with patch("qscopr.gate.run", side_effect=run):
            result = container_gate(
                self.project, self.project, self.output,
                {"image": "registry.fedoraproject.org/fedora:44"},
            )
        self.assertEqual(result["status"], "validated")
        builder = next(command for command in commands if "--network=none" in command)
        self.assertIn("--user=1000:1000", builder)
        self.assertIn("--cap-drop=ALL", builder)
        self.assertIn("--security-opt=no-new-privileges", builder)
        self.assertFalse(any("-v" in command or "--mount" in command for command in commands))
        self.assertEqual(cleanup.call_count, 3)
        which.assert_called_once_with("podman")
        for command in commands:
            self.assertEqual(command[0], "/usr/bin/podman")
        for call in cleanup.call_args_list:
            self.assertEqual(call.args[0][0], "/usr/bin/podman")

    def test_missing_podman_does_not_fall_back_to_docker(self):
        with patch(
            "qscopr.gate.shutil.which",
            side_effect=lambda program: "/usr/bin/docker" if program == "docker" else None,
        ) as which, patch("qscopr.gate.run") as run, patch("qscopr.gate.subprocess.run") as cleanup:
            with self.assertRaisesRegex(GateError, "Podman is required") as error:
                container_gate(
                    self.project,
                    self.project,
                    self.output,
                    {
                        "image": "registry.fedoraproject.org/fedora:44",
                    },
                )
        self.assertEqual(error.exception.status, "infrastructure-failed")
        which.assert_called_once_with("podman")
        run.assert_not_called()
        cleanup.assert_not_called()

    @patch("qscopr.gate.subprocess.run")
    @patch("qscopr.gate.shutil.which", return_value="/usr/bin/podman")
    def test_blocked_dependencies_never_launch_builder(self, which, cleanup):
        commands = []

        def run(command, log, capture=False):
            commands.append(command)
            if command[1:3] == ["image", "inspect"]:
                return '[{"Id": "sha256:base", "RepoDigests": []}]'
            if command[1] == "cp" and ":/work/artifacts/." in command[2]:
                write_json(self.output / "result.json", {"status": "dependency-blocked"})
            return ""

        with patch("qscopr.gate.run", side_effect=run):
            result = container_gate(
                self.project, self.project, self.output,
                {"image": "registry.fedoraproject.org/fedora:44"},
            )
        self.assertEqual(result["status"], "dependency-blocked")
        self.assertFalse(any("--network=none" in command or "commit" in command for command in commands))

    def add_cpptrace_artifacts(self):
        (self.output / "cpptrace.spec").write_text("Name: cpptrace\n")
        (self.output / "cpptrace.src.rpm").write_bytes(b"cpptrace SRPM")
        self.report["cpptrace_spec_sha256"] = sha256(self.output / "cpptrace.spec")
        self.report["cpptrace_release"] = {
            "version": "1.0.4",
            "commit": "b" * 40,
        }
        self.report["cpptrace_rpm_release"] = 1
        self.report["cpptrace"] = {"srpm": "cpptrace.src.rpm"}
        self.report["rpm_sha256"]["cpptrace.src.rpm"] = sha256(self.output / "cpptrace.src.rpm")
        self.save_report()

    def test_dual_spec_promotion(self):
        self.add_cpptrace_artifacts()
        promote(self.project, self.output)
        self.assertEqual((self.project / "packaging/cpptrace.spec").read_text(), "Name: cpptrace\n")

    def test_cpptrace_spec_tampering_blocks_promotion(self):
        self.add_cpptrace_artifacts()
        (self.output / "cpptrace.spec").write_text("tampered")
        with self.assertRaisesRegex(GateError, "cpptrace spec"):
            promote(self.project, self.output)
        self.assertIsNone(load_state(self.project))

    def test_cpptrace_srpm_tampering_blocks_promotion(self):
        self.add_cpptrace_artifacts()
        (self.output / "cpptrace.src.rpm").write_bytes(b"tampered")
        with self.assertRaisesRegex(GateError, "checksum"):
            promote(self.project, self.output)

    def test_publication_includes_dependency_manifest(self):
        self.add_cpptrace_artifacts()
        promote(self.project, self.output)
        publication(self.project, self.output)
        manifest = json.loads((self.output / "publication.json").read_text())
        self.assertEqual(manifest["prerequisites"][0]["name"], "cpptrace")
        self.assertEqual(manifest["prerequisites"][0]["srpm"], "cpptrace.src.rpm")

    @patch("qscopr.gate.subprocess.run")
    @patch("qscopr.gate.shutil.which", return_value="/usr/bin/podman")
    def test_cpptrace_build_install_and_quickshell_are_separate_stages(self, which, cleanup):
        (self.project / "cpptrace-release.json").write_text("{}")
        commands = []
        dependency = {
            "runtime_rpms": ["cpptrace.rpm", "cpptrace-devel.rpm"],
            "rpm_sha256": {},
        }
        for filename in dependency["runtime_rpms"]:
            (self.output / filename).write_bytes(filename.encode())
            dependency["rpm_sha256"][filename] = sha256(self.output / filename)
        write_json(self.output / "cpptrace-result.json", dependency)

        def run(command, log, capture=False):
            commands.append(command)
            if command[1:3] == ["image", "inspect"]:
                return '[{"Id": "sha256:base", "RepoDigests": []}]'
            if command[1] == "commit":
                return "sha256:" + command[2]
            if command[1] == "cp" and ":/work/artifacts/." in command[2]:
                stage = command[2].split("-")[1]
                statuses = {
                    "prepare": "prepared",
                    "cpptrace": "cpptrace-validated",
                    "install": "cpptrace-installed",
                    "build": "validated",
                }
                write_json(self.output / "result.json", {
                    "status": statuses[stage],
                    "cpptrace": dependency,
                    "rpm_sha256": {},
                })
            return ""

        with patch("qscopr.gate.run", side_effect=run):
            result = container_gate(
                self.project, self.project, self.output,
                {"image": "registry.fedoraproject.org/fedora:44"},
            )
        self.assertEqual(result["status"], "validated")
        stages = [
            command[-1]
            for command in commands
            if command[1] == "exec" and "/work/container.py" in command
        ]
        self.assertEqual(stages, ["prepare", "build-cpptrace", "install-cpptrace", "build"])
        builders = [
            command for command in commands
            if command[1] == "run" and "--user=1000:1000" in command
        ]
        self.assertEqual(len(builders), 2)
        for command in builders:
            self.assertIn("--network=none", command)
            self.assertIn("--cap-drop=ALL", command)
        installer = next(
            command for command in commands
            if command[1] == "run" and "--user=0:0" in command
        )
        self.assertEqual(installer[-3], builders[0][-3])
        self.assertNotEqual(installer[-3], builders[1][-3])
        self.assertFalse(any("--mount" in command or "-v" in command for command in commands))
        self.assertEqual(cleanup.call_count, 6)
