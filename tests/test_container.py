import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qscopr import container


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)
        self.artifacts = self.work / "artifacts"
        self.artifacts.mkdir()
        self.work_patch = patch.object(container, "WORK", self.work)
        self.artifacts_patch = patch.object(container, "ARTIFACTS", self.artifacts)
        self.work_patch.start()
        self.artifacts_patch.start()
        self.addCleanup(self.work_patch.stop)
        self.addCleanup(self.artifacts_patch.stop)

    def inventory(self):
        (self.work / "inventory.json").write_text(json.dumps({
            "requirements": [
                {
                    "capability": "cmake(cpptrace)",
                },
            ],
        }))

    @patch("qscopr.container.shutil.copytree")
    def test_missing_capability_stops_before_install(self, copytree):
        self.inventory()
        calls = []

        def execute(command, log, capture=False):
            calls.append(command)
            return ""

        with patch.object(container, "execute", side_effect=execute):
            container.prepare()
        result = json.loads((self.artifacts / "result.json").read_text())
        self.assertEqual(result["status"], "dependency-blocked")
        self.assertEqual(result["missing"], ["cmake(cpptrace)"])
        self.assertFalse(any("install" in command for command in calls))

    @patch("qscopr.container.shutil.copytree")
    def test_metadata_failure_is_not_missing_dependency(self, copytree):
        self.inventory()
        with patch.object(
            container,
            "execute",
            side_effect=subprocess.CalledProcessError(1, ["dnf", "makecache"]),
        ), self.assertRaises(subprocess.CalledProcessError):
            container.prepare()
        self.assertFalse((self.artifacts / "providers.json").exists())

    @patch("qscopr.container.shutil.copytree")
    def test_transaction_conflict_blocks_build(self, copytree):
        self.inventory()

        def execute(command, log, capture=False):
            if "repoquery" in command:
                return "cpptrace-devel-1.0.x86_64 [fedora]\n"
            if "install" in command:
                (self.artifacts / "transaction.log").write_text("Problem: conflicting requests")
                raise subprocess.CalledProcessError(1, command)
            return ""

        with patch.object(container, "execute", side_effect=execute):
            container.prepare()
        result = json.loads((self.artifacts / "result.json").read_text())
        self.assertEqual(result["status"], "dependency-blocked")

    @patch("qscopr.container.shutil.copytree")
    def test_result_is_written_before_chown(self, copytree):
        self.inventory()

        def execute(command, log, capture=False):
            if "repoquery" in command:
                return "cpptrace-devel-1.0.x86_64 [fedora]\n"
            if command[0] == "rpm":
                return "cpptrace-devel-0:1.0-1.fc44.x86_64\n"
            if command[0] == "chown":
                result = json.loads((self.artifacts / "result.json").read_text())
                self.assertEqual(result["status"], "prepared")
            return ""

        with patch.object(container, "execute", side_effect=execute) as mock:
            container.prepare()
        self.assertEqual(mock.call_args.args[0][0], "chown")

    @patch("qscopr.container.os.getuid", return_value=0)
    def test_root_cannot_compile(self, getuid):
        with self.assertRaisesRegex(RuntimeError, "root"):
            container.build()

    @patch("qscopr.container.os.getuid", return_value=1000)
    def test_build_checks_source_and_produces_hashed_artifacts(self, getuid):
        commit = "a" * 40
        source = b"test source archive"
        (self.work / "source.tar.gz").write_bytes(source)
        (self.work / "release.json").write_text(json.dumps({
            "commit": commit,
            "sha256": hashlib.sha256(source).hexdigest(),
        }))
        (self.work / "build-options.json").write_text('{"jobs": 2}')
        (self.work / "candidate.spec").write_text("mock spec")

        def execute(command, log, capture=False):
            if command[0] == "rpmbuild":
                top = self.work / "rpmbuild"
                (top / "RPMS" / "quickshell.x86_64.rpm").write_bytes(b"rpm")
                (top / "SRPMS" / "quickshell.src.rpm").write_bytes(b"srpm")
            return ""

        with patch.object(container, "execute", side_effect=execute) as mock:
            container.build()
        result = json.loads((self.artifacts / "result.json").read_text())
        self.assertEqual(result["status"], "validated")
        self.assertEqual(result["rpm_sha256"]["quickshell.src.rpm"], hashlib.sha256(b"srpm").hexdigest())
        self.assertIn("_smp_mflags -j2", mock.call_args_list[0].args[0])

    @patch("qscopr.container.shutil.copytree")
    def test_cpptrace_transitive_dependency_blocks_preparation(self, copytree):
        (self.work / "inventory.json").write_text(json.dumps({
            "requirements": [{"capability": "cmake(cpptrace)"}],
            "repository_requirements": ["pkgconfig(libunwind)"],
        }))
        with patch.object(container, "execute", return_value="") as execute:
            container.prepare()
        result = json.loads((self.artifacts / "result.json").read_text())
        self.assertEqual(result["missing"], ["pkgconfig(libunwind)"])
        self.assertFalse(any("install" in call.args[0] for call in execute.call_args_list))

    def dependency_artifacts(self):
        result = {
            "runtime_rpms": [
                "cpptrace.rpm",
                "cpptrace-devel.rpm",
            ],
            "rpm_sha256": {},
        }
        for filename in result["runtime_rpms"]:
            (self.artifacts / filename).write_bytes(filename.encode())
            result["rpm_sha256"][filename] = hashlib.sha256(filename.encode()).hexdigest()
        (self.artifacts / "cpptrace-result.json").write_text(json.dumps(result))
        return result

    @patch("qscopr.container.os.getuid", return_value=0)
    def test_installed_rpms_recheck_quickshell_providers(self, getuid):
        self.inventory()
        self.dependency_artifacts()
        with patch.object(container, "execute", return_value="cpptrace-devel-1.0.4-1.fc44.x86_64\n") as execute:
            container.install_cpptrace()
        commands = [call.args[0] for call in execute.call_args_list]
        self.assertEqual(commands[0][:2], ["rpm", "-Uvh"])
        self.assertNotIn("--nodeps", commands[0])
        query = next(command for command in commands if "repoquery" in command)
        self.assertIn("--installed", query)
        self.assertIn("--cacheonly", query)
        self.assertIn("cmake(cpptrace)", query)
        result = json.loads((self.artifacts / "result.json").read_text())
        self.assertEqual(result["status"], "cpptrace-installed")

    @patch("qscopr.container.os.getuid", return_value=0)
    def test_missing_installed_provider_is_not_accepted(self, getuid):
        self.inventory()
        self.dependency_artifacts()
        with patch.object(container, "execute", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "No installed provider"):
                container.install_cpptrace()

    @patch("qscopr.container.os.getuid", return_value=0)
    def test_tampered_dependency_is_not_installed(self, getuid):
        self.dependency_artifacts()
        (self.artifacts / "cpptrace.rpm").write_bytes(b"tampered")
        with patch.object(container, "execute") as execute:
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                container.install_cpptrace()
        execute.assert_not_called()

    @patch("qscopr.container.os.getuid", return_value=1000)
    def test_consumer_failure_prevents_quickshell_build(self, getuid):
        self.dependency_artifacts()
        with patch.object(container, "validate_cpptrace_consumer", side_effect=RuntimeError("consumer failed")):
            with patch.object(container, "build_package") as build:
                with self.assertRaisesRegex(RuntimeError, "consumer failed"):
                    container.build()
        build.assert_not_called()

    def test_consumer_uses_installed_cmake_package(self):
        (self.work / "cpptrace-smoke.cpp").write_text("int main() { return 0; }\n")
        with patch.object(container, "execute", return_value="") as execute:
            container.validate_cpptrace_consumer()
        cmake = (self.work / "cpptrace-consumer/CMakeLists.txt").read_text()
        self.assertIn("find_package(cpptrace 1.0.4 REQUIRED)", cmake)
        self.assertIn("cpptrace::cpptrace", cmake)
        self.assertNotIn("rpmbuild-cpptrace", cmake)
        self.assertEqual(len(execute.call_args_list), 3)
