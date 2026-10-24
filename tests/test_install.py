import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from qscopr import automate, gate


ROOT = Path(__file__).resolve().parents[1]
LOADER = importlib.machinery.SourceFileLoader("quickshell_copr_installer", str(ROOT / "install"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
installer = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.units = self.directory / "units"
        self.units.mkdir()
        self.config = self.directory / "automation.toml"
        self.config.write_text("test configuration, never read by mocked commands")
        self.options = types.SimpleNamespace(
            project=ROOT,
            config=self.config,
            python=Path(sys.executable),
            user="build-user",
            calendar="*-*-* 06:23:00 UTC",
        )
        self.commands = []
        self.active = {}
        self.loaded = False
        self.failure = None
        unit_patcher = patch.object(installer, "UNIT_DIRECTORY", self.units)
        unit_patcher.start()
        self.addCleanup(unit_patcher.stop)
        command_patcher = patch.object(installer.subprocess, "run", side_effect=self.command)
        self.mock_command = command_patcher.start()
        self.addCleanup(command_patcher.stop)

    def command(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if self.failure and self.failure in command:
            raise subprocess.CalledProcessError(1, command)
        output = ""
        if "--property=ActiveState" in command:
            output = self.active.get(command[-1], "inactive") + "\n"
        if "--property=LoadState" in command:
            output = "loaded\n" if self.loaded else "not-found\n"
        return subprocess.CompletedProcess(command, 0, stdout=output)

    def render(self):
        return installer.render_units(self.options)

    def invoke(self, *arguments, uid=0):
        with patch.object(installer.os, "geteuid", return_value=uid), patch.object(
            installer, "check_prerequisites",
        ) as prerequisites, patch.object(installer, "verify_units") as verify, patch.object(
            installer.shutil, "which", side_effect=lambda value: str(value) if "/" in str(value) else f"/usr/bin/{value}",
        ):
            result = installer.main([
                "--config", str(self.config),
                "--user", self.options.user,
                *arguments,
            ])
        return result, prerequisites, verify

    def test_render_replaces_service_account_project_and_interpreter(self):
        units = self.render()
        for name in installer.UNITS[:2]:
            self.assertIn("User=build-user\n", units[name])
            self.assertNotIn("Group=", units[name])
            self.assertIn(f"WorkingDirectory={ROOT}\n", units[name])
            self.assertIn(f'ExecStart="{sys.executable}" "-m" "qscopr.automate"', units[name])
            self.assertIn(f'"--config" "{self.config}"', units[name])
        self.assertIn('"--notify-only"', units["quickshell-copr-alert.service"])
        self.assertNotIn('"--notify-only"', units["quickshell-copr.service"])
        self.assertIn("OnFailure=quickshell-copr-alert.service", units["quickshell-copr.service"])

    def test_render_escapes_systemd_expansion_and_preserves_spaces(self):
        self.options.project = Path("/srv/build %u $project")
        self.options.config = Path("/etc/build %u $settings/automation.toml")
        units = self.render()
        self.assertIn("WorkingDirectory=/srv/build %%u $project\n", units["quickshell-copr.service"])
        self.assertIn('"/etc/build %%u $$settings/automation.toml"', units["quickshell-copr.service"])

    def test_custom_calendar_preserves_catchup_and_jitter(self):
        self.options.calendar = "hourly"
        timer = self.render()["quickshell-copr.timer"]
        self.assertIn("OnCalendar=hourly\n", timer)
        self.assertIn("Persistent=true\n", timer)
        self.assertIn("RandomizedDelaySec=10m\n", timer)

    def test_control_characters_quotes_and_backslashes_in_paths_are_refused(self):
        for value in ["/tmp/a\nb", "/tmp/a\rb", '/tmp/"quoted"', "/tmp/a\\b", "/tmp/a'b"]:
            with self.subTest(value=value), self.assertRaises(installer.InstallError):
                installer.checked_path(value)

    def test_symlink_cannot_hide_a_path_with_control_characters(self):
        link = self.directory / "link"
        link.symlink_to(self.directory / "name\nUser=root")
        with self.assertRaises(installer.InstallError):
            installer.checked_path(link)

    def test_exec_argument_protects_literal_dollars_and_specifiers(self):
        self.assertEqual(installer.exec_argument("/tmp/$VAR/%u/path with spaces"), '"/tmp/$$VAR/%%u/path with spaces"')

    def test_dry_run_does_not_check_credentials_write_units_or_call_systemctl(self):
        self.config.unlink()
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result, prerequisites, verify = self.invoke("--dry-run", uid=1000)
        self.assertEqual(result, 0)
        prerequisites.assert_not_called()
        verify.assert_called_once()
        self.assertEqual(self.commands, [])
        self.assertEqual(list(self.units.iterdir()), [])
        self.assertIn("Dry run only", output.getvalue())
        self.assertIn("ExecStart=", output.getvalue())

    def test_install_requires_root(self):
        result, prerequisites, verify = self.invoke(uid=1000)
        self.assertEqual(result, 1)
        prerequisites.assert_not_called()
        verify.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_invalid_account_and_calendar_cannot_inject_unit_directives(self):
        for arguments in [
            ["--user", "root"],
            ["--user", "builder\nUser=root"],
            ["--calendar", "daily\nUnit=other.service"],
            ["--calendar", ""],
        ]:
            with self.subTest(arguments=arguments):
                result, prerequisites, verify = self.invoke(*arguments)
                self.assertEqual(result, 1)
                prerequisites.assert_not_called()
                verify.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_invalid_project_is_refused_before_installation(self):
        result, prerequisites, verify = self.invoke("--project", str(self.directory))
        self.assertEqual(result, 1)
        prerequisites.assert_not_called()
        verify.assert_not_called()
        self.assertEqual(self.commands, [])

    def test_python_interpreter_symlink_is_preserved_for_virtual_environments(self):
        executable = self.directory / "venv-python"
        executable.symlink_to(sys.executable)
        result, prerequisites, verify = self.invoke("--python", str(executable))
        self.assertEqual(result, 0)
        self.assertEqual(prerequisites.call_args.args[0].python, executable)
        self.assertIn(f'ExecStart="{executable}"', (self.units / installer.UNITS[0]).read_text())

    def test_default_install_writes_units_and_enables_timer_only(self):
        result, prerequisites, verify = self.invoke()
        self.assertEqual(result, 0)
        prerequisites.assert_called_once()
        verify.assert_called_once()
        for name in installer.UNITS:
            path = self.units / name
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)
        commands = [command for command, _ in self.commands]
        self.assertIn(["systemctl", "daemon-reload"], commands)
        self.assertEqual(commands[-1], ["systemctl", "enable", "--now", "quickshell-copr.timer"])
        self.assertFalse(any(command[1] in ["start", "restart"] for command in commands))
        self.assertNotIn(["systemctl", "stop", "quickshell-copr.timer"], commands)

    def test_no_enable_disables_and_stops_timer(self):
        result, _, _ = self.invoke("--no-enable")
        self.assertEqual(result, 0)
        commands = [command for command, _ in self.commands]
        self.assertEqual(commands[-1], ["systemctl", "disable", "--now", "quickshell-copr.timer"])
        self.assertFalse(any("enable" in command for command in commands))

    def test_running_oneshot_or_alert_service_blocks_installation(self):
        for service in installer.UNITS[:2]:
            for state in ["active", "activating", "deactivating", "reloading"]:
                with self.subTest(service=service, state=state):
                    self.active = {service: state}
                    with self.assertRaises(installer.InstallError):
                        installer.install_units(self.render(), enable=True)
                    self.assertEqual(list(self.units.iterdir()), [])
        self.assertFalse(any("daemon-reload" in command for command, _ in self.commands))

    def test_failed_previous_run_does_not_prevent_installation(self):
        self.active["quickshell-copr.service"] = "failed"
        installer.install_units(self.render(), enable=False)
        self.assertTrue((self.units / installer.UNITS[0]).is_file())

    def test_existing_timer_stops_before_units_are_reloaded(self):
        self.loaded = True
        installer.install_units(self.render(), enable=True)
        commands = [command for command, _ in self.commands]
        self.assertLess(
            commands.index(["systemctl", "stop", "quickshell-copr.timer"]),
            commands.index(["systemctl", "daemon-reload"]),
        )
        self.assertEqual(sum("--property=ActiveState" in command for command in commands), 4)

    def test_timer_firing_during_install_is_detected_before_units_are_written(self):
        self.loaded = True
        original_command = self.command

        def timer_fired(command, **kwargs):
            result = original_command(command, **kwargs)
            if command == ["systemctl", "stop", "quickshell-copr.timer"]:
                self.active["quickshell-copr.service"] = "activating"
            return result

        self.mock_command.side_effect = timer_fired
        with self.assertRaises(installer.InstallError):
            installer.install_units(self.render(), enable=True)
        self.assertEqual(list(self.units.iterdir()), [])
        self.assertFalse(any("daemon-reload" in command for command, _ in self.commands))

    def test_changed_units_are_backed_up_and_identical_reinstall_is_idempotent(self):
        path = self.units / installer.UNITS[0]
        path.write_text("old unit contents\n")
        installer.install_units(self.render(), enable=False)
        backups = list(self.units.glob("quickshell-copr-backup-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / path.name).read_text(), "old unit contents\n")
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o700)
        before = path.stat().st_mtime_ns
        installer.install_units(self.render(), enable=False)
        self.assertEqual(path.stat().st_mtime_ns, before)
        self.assertEqual(list(self.units.glob("quickshell-copr-backup-*")), backups)

    def test_symlinked_unit_is_not_overwritten(self):
        target = self.directory / "manual.service"
        target.write_text("manual configuration\n")
        (self.units / installer.UNITS[0]).symlink_to(target)
        with self.assertRaises(installer.InstallError):
            installer.install_units(self.render(), enable=True)
        self.assertEqual(target.read_text(), "manual configuration\n")
        self.assertEqual(self.commands, [])

    def test_reload_failure_does_not_enable_timer_and_returns_nonzero(self):
        self.failure = "daemon-reload"
        result, _, _ = self.invoke()
        self.assertEqual(result, 1)
        self.assertFalse(any("enable" in command for command, _ in self.commands))

    def test_missing_or_root_account_is_refused_before_probe(self):
        for account in [KeyError("missing"), types.SimpleNamespace(pw_uid=0)]:
            with self.subTest(account=account), patch.object(installer.pwd, "getpwnam") as lookup:
                if isinstance(account, Exception):
                    lookup.side_effect = account
                else:
                    lookup.return_value = account
                with self.assertRaises(installer.InstallError):
                    installer.check_prerequisites(self.options)
        self.assertEqual(self.commands, [])

    def test_missing_settings_refuse_installation_before_probe(self):
        self.config.unlink()
        with patch.object(installer.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=1234)):
            with self.assertRaises(installer.InstallError):
                installer.check_prerequisites(self.options)
        self.assertEqual(self.commands, [])

    def test_prerequisite_probe_drops_privileges_and_does_not_inherit_secrets(self):
        with patch.object(installer.pwd, "getpwnam", return_value=types.SimpleNamespace(pw_uid=1234)), patch.dict(
            os.environ,
            {
                "COPR_TOKEN": "dummy-copr-token",
                "SMTP_PASSWORD": "dummy-smtp-password",
                "PYTHONPATH": "/untrusted",
            },
        ):
            installer.check_prerequisites(self.options)
        command, kwargs = self.commands[0]
        self.assertEqual(command[:4], ["runuser", "--user", "build-user", "--"])
        self.assertEqual(command[4], sys.executable)
        self.assertEqual(kwargs["cwd"], ROOT)
        self.assertEqual(kwargs["env"], {
            "PATH": installer.SERVICE_PATH,
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.assertNotIn("dummy-copr-token", " ".join(command))
        self.assertNotIn("dummy-smtp-password", " ".join(command))


class InstallerPreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.settings = {
            "copr_login_file": str(self.directory / "login"),
            "copr_token_file": str(self.directory / "token"),
            "smtp": {
                "password_file": str(self.directory / "smtp-password"),
            },
        }
        self.paths = [
            Path(self.settings["copr_login_file"]),
            Path(self.settings["copr_token_file"]),
            Path(self.settings["smtp"]["password_file"]),
        ]
        for path in self.paths:
            path.write_text("dummy fixture")
            path.chmod(0o600)
        copr = types.ModuleType("copr")
        copr.v3 = types.ModuleType("copr.v3")
        patches = [
            patch.dict(sys.modules, {
                "copr": copr,
                "copr.v3": copr.v3,
            }),
            patch.object(sys, "argv", ["-c", str(self.directory / "automation.toml")]),
            patch.object(sys, "version_info", (3, 12)),
            patch.object(automate, "load_settings", return_value=self.settings),
            patch.object(gate, "load_config", return_value={}),
            patch.object(installer.shutil, "which", return_value="/usr/bin/podman"),
            patch("ssl.create_default_context"),
            patch.object(Path, "is_dir", return_value=True),
            patch.object(os, "access", return_value=True),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.output = io.StringIO()
        self.errors = io.StringIO()

    def probe(self):
        with contextlib.redirect_stdout(self.output), contextlib.redirect_stderr(self.errors), patch.object(
            Path, "read_text", side_effect=AssertionError("Credential contents must not be read"),
        ), patch.object(
            Path, "read_bytes", side_effect=AssertionError("Credential contents must not be read"),
        ), patch.object(
            Path, "open", side_effect=AssertionError("Credential contents must not be opened"),
        ):
            exec(compile(installer.PREFLIGHT, "<installer preflight>", "exec"), {})

    def test_probe_inspects_metadata_only(self):
        self.probe()
        self.assertIn("prerequisite checks passed", self.output.getvalue())
        self.assertEqual(self.errors.getvalue(), "")

    def test_readonly_credentials_are_accepted(self):
        for path in self.paths:
            path.chmod(0o400)
        self.probe()
        self.assertEqual(self.errors.getvalue(), "")

    def test_optional_smtp_password_is_not_required_for_unauthenticated_relay(self):
        self.settings["smtp"] = {}
        self.paths[-1].unlink()
        self.probe()
        self.assertEqual(self.errors.getvalue(), "")

    def test_empty_missing_public_or_directory_credentials_are_rejected(self):
        for problem in ["empty", "missing", "public", "directory"]:
            with self.subTest(problem=problem):
                path = self.paths[0]
                if path.is_file():
                    path.unlink()
                if problem == "directory":
                    path.mkdir()
                elif problem != "missing":
                    path.write_text("" if problem == "empty" else "dummy fixture")
                    path.chmod(0o644 if problem == "public" else 0o600)
                with self.assertRaises(SystemExit) as error:
                    self.probe()
                self.assertEqual(error.exception.code, 1)
                self.assertIn("credential-file permissions", self.errors.getvalue())

    def test_configuration_error_details_are_not_printed(self):
        with patch.object(automate, "load_settings", side_effect=ValueError("sensitive error details")):
            with self.assertRaises(SystemExit):
                self.probe()
        self.assertIn("automation and build configuration (ValueError)", self.errors.getvalue())
        self.assertNotIn("sensitive error details", self.errors.getvalue())

    def test_missing_runtime_is_rejected_without_launching_containers(self):
        with patch.object(installer.shutil, "which", return_value=None):
            with self.assertRaises(SystemExit):
                self.probe()
        self.assertIn("container runtime", self.errors.getvalue())

    def test_podman_is_checked_even_when_docker_is_installed(self):
        with patch.object(
            installer.shutil,
            "which",
            side_effect=lambda program: f"/usr/bin/{program}",
        ) as which:
            self.probe()
        which.assert_called_once_with("podman")
        self.assertIn("prerequisite checks passed", self.output.getvalue())

    def test_docker_alone_does_not_satisfy_the_runtime_prerequisite(self):
        with patch.object(
            installer.shutil,
            "which",
            side_effect=lambda program: "/usr/bin/docker" if program == "docker" else None,
        ) as which:
            with self.assertRaises(SystemExit) as error:
                self.probe()
        self.assertEqual(error.exception.code, 1)
        which.assert_called_once_with("podman")
        self.assertIn("container runtime", self.errors.getvalue())


@unittest.skipUnless(shutil.which("systemd-analyze"), "Unit verification requires systemd-analyze")
class InstallerCLITests(unittest.TestCase):
    def test_real_dry_run_verifies_units_with_spaces_and_literal_specifiers(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "install"),
                "--dry-run",
                "--config",
                "/etc/with spaces %u $literal/automation.toml",
                "--calendar",
                "hourly",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"--config" "/etc/with spaces %%u $$literal/automation.toml"', result.stdout)
        self.assertIn("OnCalendar=hourly", result.stdout)
        self.assertIn("no files or schedules changed", result.stdout)

    def test_invalid_calendar_is_rejected_by_systemd_without_installation(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "install"), "--dry-run", "--calendar", "invalid-calendar"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("ExecStart=", result.stdout)


if __name__ == "__main__":
    unittest.main()
