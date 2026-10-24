import fcntl
import json
import os
import smtplib
import subprocess
import sys
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from qscopr.automate import (
    atomic_json,
    credential,
    load_settings,
    main,
    notify_failure,
    run,
    validation_identity,
)


class AutomationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.root = self.project / "artifacts" / "automation"
        packaging = self.project / "packaging"
        packaging.mkdir()
        for name in ["validated.json", "quickshell.spec", "cpptrace.spec"]:
            (packaging / name).write_text("{}")
        login = self.project / "login"
        token = self.project / "token"
        for path, content in [(login, "test-login"), (token, "test-token")]:
            path.write_text(content)
            path.chmod(0o600)
        self.settings = {
            "alert_from": "builder@example.com",
            "alert_to": [
                "operator@example.com",
            ],
            "smtp": {
                "host": "smtp.example.com",
            },
            "copr_login_file": str(login),
            "copr_token_file": str(token),
            "stage_timeout_seconds": 30,
        }
        self.status = "validated"
        self.failure = None
        self.exception = None
        self.commands = []
        self.mails = []
        self.results_succeeded = True
        patcher = patch("qscopr.automate.subprocess.run", side_effect=self.execute)
        self.mock_run = patcher.start()
        self.addCleanup(patcher.stop)
        mail_patcher = patch("qscopr.automate.send_email", side_effect=self.capture_email)
        self.mock_send = mail_patcher.start()
        self.addCleanup(mail_patcher.stop)

    def capture_email(self, settings, subject, body):
        message = EmailMessage()
        message["From"] = settings["alert_from"]
        message["To"] = ", ".join(settings["alert_to"])
        message["Subject"] = subject
        message.set_content(body)
        self.mails.append(message.as_bytes())

    def execute(self, command, **kwargs):
        self.commands.append((command, kwargs))
        stage = command[3] if command[1:3] == ["-m", "qscopr"] else command[2]
        if command[1:3] == ["-m", "qscopr.publish"]:
            stage = "publish"
        if stage == self.failure:
            if self.exception:
                raise self.exception
            raise subprocess.CalledProcessError(2, command)
        if "--output" in command:
            output = Path(command[command.index("--output") + 1])
            if stage == "check":
                output.mkdir()
                status = "validated" if "--verify-tracked" in command else self.status
                (output / "gate.json").write_text(json.dumps({"status": status}))
            if stage == "publish":
                for name in ["cpptrace-copr-result.json", "copr-result.json"]:
                    (output / name).write_text(json.dumps({
                        "state": "succeeded" if self.results_succeeded else "failed",
                        "chroot_state": "succeeded",
                        "build_id": 42,
                    }))
        return subprocess.CompletedProcess(command, 0)

    def stages(self):
        stages = []
        for command, kwargs in self.commands:
            if command[1:3] == ["-m", "qscopr"]:
                stages.append("revalidate" if "--verify-tracked" in command else command[3])
            elif command[1:3] == ["-m", "qscopr.publish"]:
                stages.append("publish")
            else:
                stages.append(command[2])
        return stages

    def latest(self):
        return json.loads((self.root / "latest.json").read_text())

    def receipt(self):
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_json(self.root / "published.json", {
            "validation": validation_identity(self.project),
        })

    def test_complete_pipeline_in_order(self):
        self.assertEqual(run(self.project, self.settings), 0)
        self.assertEqual(self.stages(), [
            "import copr.v3",
            "unittest",
            "compileall",
            "check",
            "promote",
            "publication",
            "publish",
        ])
        self.assertEqual(self.latest()["status"], "published")
        receipt = json.loads((self.root / "published.json").read_text())
        self.assertEqual(receipt["validation"], validation_identity(self.project))
        self.assertEqual(self.mails, [])

    def test_each_failed_stage_stops_and_sends_alert(self):
        for failure in [
            "import copr.v3",
            "unittest",
            "compileall",
            "check",
            "promote",
            "publication",
            "publish",
        ]:
            with self.subTest(stage=failure):
                self.failure = failure
                self.commands.clear()
                self.assertEqual(run(self.project, self.settings), 1)
                self.assertEqual(self.stages()[-1], failure)
                self.assertTrue(self.latest()["alert_sent"])
                self.assertEqual(self.latest()["exit_code"], 2)
                self.assertFalse((self.root / "published.json").exists())
        self.assertEqual(len(self.mails), 7)

    def test_blocked_report_cannot_advance_even_with_zero_exit(self):
        for status in ["dependency-blocked", "discovery-blocked", "build-failed", "infrastructure-failed"]:
            with self.subTest(status=status):
                self.status = status
                self.commands.clear()
                self.assertEqual(run(self.project, self.settings), 1)
                self.assertEqual(self.stages()[-1], "check")
                self.assertFalse((self.root / "published.json").exists())

    def test_unchanged_with_success_receipt_skips_publication(self):
        self.status = "unchanged"
        self.receipt()
        self.assertEqual(run(self.project, self.settings), 0)
        self.assertEqual(self.latest()["status"], "unchanged")
        self.assertEqual(self.stages()[-1], "check")
        self.assertEqual(self.mails, [])

    def test_unchanged_without_receipt_revalidates_and_publishes(self):
        self.status = "unchanged"
        self.assertEqual(run(self.project, self.settings), 0)
        self.assertEqual(self.stages()[-3:], ["revalidate", "publication", "publish"])
        self.assertNotIn("promote", self.stages())

    def test_changed_packaging_does_not_match_old_receipt(self):
        self.status = "unchanged"
        self.receipt()
        (self.project / "packaging" / "quickshell.spec").write_text("changed")
        self.assertEqual(run(self.project, self.settings), 0)
        self.assertIn("revalidate", self.stages())

    def test_failed_publish_is_retried_after_promotion(self):
        self.failure = "publish"
        self.assertEqual(run(self.project, self.settings), 1)
        self.status = "unchanged"
        self.failure = None
        self.commands.clear()
        self.assertEqual(run(self.project, self.settings), 0)
        self.assertIn("revalidate", self.stages())
        self.assertEqual(self.latest()["status"], "published")

    def test_revalidation_failure_prevents_publication(self):
        self.status = "unchanged"
        original = self.execute

        def execute(command, **kwargs):
            if "--verify-tracked" in command:
                self.commands.append((command, kwargs))
                raise subprocess.CalledProcessError(2, command)
            return original(command, **kwargs)

        self.mock_run.side_effect = execute
        self.assertEqual(run(self.project, self.settings), 1)
        self.assertIn("revalidate", self.stages())
        self.assertNotIn("publish", self.stages())

    def test_unsuccessful_copr_result_cannot_create_receipt(self):
        self.results_succeeded = False
        self.assertEqual(run(self.project, self.settings), 1)
        self.assertFalse((self.root / "published.json").exists())
        self.assertEqual(len(self.mails), 1)

    def test_failed_rebuild_preserves_previous_success_receipt(self):
        self.receipt()
        before = (self.root / "published.json").read_bytes()
        self.failure = "publish"
        self.assertEqual(run(self.project, self.settings, force=True), 1)
        self.assertEqual((self.root / "published.json").read_bytes(), before)

    def test_publishing_is_the_only_stage_with_copr_credentials(self):
        with patch.dict(os.environ, {
            "COPR_LOGIN": "inherited-login",
            "COPR_TOKEN": "inherited-token",
            "ENABLE_COPR_PUBLISH": "true",
            "SMTP_PASSWORD": "inherited-smtp-password",
        }):
            self.assertEqual(run(self.project, self.settings), 0)
        for command, kwargs in self.commands:
            environment = kwargs["env"]
            if command[1:3] == ["-m", "qscopr.publish"]:
                self.assertEqual(environment["COPR_LOGIN"], "test-login")
                self.assertEqual(environment["COPR_TOKEN"], "test-token")
                self.assertEqual(environment["ENABLE_COPR_PUBLISH"], "true")
            else:
                self.assertNotIn("COPR_LOGIN", environment)
                self.assertNotIn("COPR_TOKEN", environment)
                self.assertNotIn("ENABLE_COPR_PUBLISH", environment)
            self.assertNotIn("test-token", " ".join(command))
            self.assertNotIn("SMTP_PASSWORD", environment)
            self.assertTrue(kwargs["check"])
            self.assertEqual(kwargs["timeout"], 30)

    def test_failure_email_omits_credentials_and_exception_values(self):
        self.failure = "publish"
        self.exception = RuntimeError("test-token inherited-token")
        self.assertEqual(run(self.project, self.settings), 1)
        message = BytesParser(policy=policy.default).parsebytes(self.mails[0])
        self.assertEqual(message["To"], "operator@example.com")
        self.assertIn("publish", message["Subject"])
        self.assertIn("pipeline.log", message.get_content())
        self.assertNotIn("test-token", str(message))
        self.assertNotIn("inherited-token", str(message))
        self.assertNotIn("test-token", (self.root / "latest.json").read_text())

    def test_smtp_failure_returns_nonzero_and_leaves_fallback_pending(self):
        self.failure = "check"
        self.mock_send.side_effect = smtplib.SMTPException("SMTP submission failed")
        self.assertEqual(run(self.project, self.settings), 1)
        self.assertFalse(self.latest()["alert_sent"])
        self.assertEqual(self.latest()["status"], "failed")

    def test_subprocess_timeout_sends_alert(self):
        self.failure = "check"
        self.exception = subprocess.TimeoutExpired(["python"], 30)
        self.assertEqual(run(self.project, self.settings), 1)
        self.assertEqual(self.latest()["error_type"], "TimeoutExpired")
        self.assertEqual(len(self.mails), 1)

    def test_unique_output_directories(self):
        self.assertEqual(run(self.project, self.settings), 0)
        first = self.latest()["output"]
        self.assertEqual(run(self.project, self.settings), 0)
        self.assertNotEqual(first, self.latest()["output"])
        self.assertTrue((Path(first) / "pipeline.log").is_file())

    def test_project_lock_skips_overlapping_run_without_alert(self):
        self.root.mkdir(parents=True)
        with (self.root / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(run(self.project, self.settings), 0)
        self.assertEqual(self.commands, [])
        self.assertEqual(self.mails, [])
        self.assertFalse((self.root / "latest.json").exists())

    def test_tag_and_force_are_forwarded_without_shell_interpolation(self):
        self.assertEqual(run(self.project, self.settings, tag="v0.3.2", force=True), 0)
        command = next(command for command, _ in self.commands if "check" in command)
        self.assertEqual(command[command.index("--tag") + 1], "v0.3.2")
        self.assertIn("--force", command)

    def test_missing_credentials_fail_before_building_and_email(self):
        Path(self.settings["copr_token_file"]).unlink()
        self.assertEqual(run(self.project, self.settings), 1)
        self.assertEqual(self.commands, [])
        self.assertEqual(len(self.mails), 1)

    def test_world_readable_credentials_are_refused(self):
        path = Path(self.settings["copr_token_file"])
        for mode in [0o644, 0o640, 0o700]:
            with self.subTest(mode=mode):
                path.chmod(mode)
                with self.assertRaisesRegex(ValueError, "private"):
                    credential(path)

    def test_smtp_password_is_checked_before_building(self):
        self.settings["smtp"].update({
            "username": "smtp-user",
            "password_file": str(self.project / "missing-smtp-password"),
        })
        self.assertEqual(run(self.project, self.settings), 1)
        self.assertEqual(self.latest()["stage"], "preflight")
        self.assertEqual(self.commands, [])

    def test_empty_or_multiline_credentials_are_refused(self):
        path = Path(self.settings["copr_token_file"])
        for value in ["", "one\ntwo\n"]:
            with self.subTest(value=value):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    credential(path)

    def test_notify_fallback_sends_for_interrupted_run(self):
        self.root.mkdir(parents=True)
        atomic_json(self.root / "latest.json", {
            "stage": "publish",
            "status": "running",
        })
        notify_failure(self.project, self.settings)
        self.assertEqual(len(self.mails), 1)

    def test_notify_fallback_does_not_duplicate_delivered_failure(self):
        self.failure = "check"
        self.assertEqual(run(self.project, self.settings), 1)
        notify_failure(self.project, self.settings)
        self.assertEqual(len(self.mails), 1)

    def test_notify_fallback_handles_missing_or_corrupt_state(self):
        notify_failure(self.project, self.settings)
        self.root.mkdir(parents=True)
        (self.root / "latest.json").write_text("{")
        notify_failure(self.project, self.settings)
        self.assertEqual(len(self.mails), 2)

    def test_old_alert_cannot_suppress_new_systemd_startup_failure(self):
        self.root.mkdir(parents=True)
        atomic_json(self.root / "latest.json", {
            "invocation_id": "previous-run",
            "stage": "check",
            "status": "failed",
            "alert_sent": True,
        })
        with patch.dict(os.environ, {
            "MONITOR_INVOCATION_ID": "new-run",
            "MONITOR_SERVICE_RESULT": "exit-code",
            "MONITOR_EXIT_STATUS": "200",
        }):
            notify_failure(self.project, self.settings)
        self.assertEqual(len(self.mails), 1)
        message = BytesParser(policy=policy.default).parsebytes(self.mails[0])
        self.assertIn("Stage: service", message.get_content())
        self.assertIn("Exit code: 200", message.get_content())

    def test_matching_systemd_invocation_does_not_duplicate_alert(self):
        self.root.mkdir(parents=True)
        atomic_json(self.root / "latest.json", {
            "invocation_id": "same-run",
            "status": "failed",
            "alert_sent": True,
        })
        with patch.dict(os.environ, {
            "MONITOR_INVOCATION_ID": "same-run",
        }):
            notify_failure(self.project, self.settings)
        self.assertEqual(self.mails, [])

    def test_state_write_failure_does_not_prevent_email(self):
        with patch("qscopr.automate.atomic_json", side_effect=OSError("disk full")):
            self.assertEqual(run(self.project, self.settings), 1)
        self.assertEqual(len(self.mails), 1)

    def test_artifact_directory_failure_still_emails(self):
        (self.project / "artifacts").write_text("not a directory")
        self.assertEqual(run(self.project, self.settings), 1)
        self.assertEqual(self.commands, [])
        self.assertEqual(len(self.mails), 1)

    def test_test_email_mode_does_not_run_pipeline(self):
        with patch.object(sys, "argv", [
            "automate", "--config", "/unused.toml", "--test-email",
        ]), patch("qscopr.automate.load_settings", return_value=self.settings):
            self.assertEqual(main(), 0)
        self.assertEqual(self.commands, [])
        self.assertEqual(len(self.mails), 1)

    def test_invalid_config_fails_without_leaking_contents(self):
        with patch.object(sys, "argv", [
            "automate", "--config", "/missing.toml",
        ]), patch("sys.stderr") as stderr:
            self.assertEqual(main(), 1)
        self.assertIn("FileNotFoundError", "".join(call.args[0] for call in stderr.write.call_args_list))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "automation.toml"
        self.content = (
            'alert_from = "builder@example.com"\n'
            'alert_to = ["operator@example.com"]\n'
            'copr_login_file = "/etc/quickshell-copr/login"\n'
            'copr_token_file = "/etc/quickshell-copr/token"\n'
            '[smtp]\nhost = "smtp.example.com"\n'
        )

    def test_valid_settings_and_defaults(self):
        self.path.write_text(self.content)
        settings = load_settings(self.path)
        self.assertEqual(settings["smtp"]["security"], "starttls")
        self.assertEqual(settings["smtp"]["port"], 587)
        self.assertEqual(settings["stage_timeout_seconds"], 18000)

    def test_sendmail_configuration_requires_migration(self):
        self.path.write_text('sendmail = "/usr/sbin/sendmail"\n' + self.content)
        with self.assertRaisesRegex(ValueError, "Replace the sendmail"):
            load_settings(self.path)

    def test_invalid_addresses_paths_and_timeouts_are_refused(self):
        for before, after in [
            ('["operator@example.com"]', "[]"),
            ('["operator@example.com"]', '"operator@example.com"'),
            ('"builder@example.com"', '"builder"'),
            ('"builder@example.com"', '"builder@example.com\\nBcc: other@example.com"'),
            ('"/etc/quickshell-copr/token"', '"relative-token"'),
        ]:
            with self.subTest(after=after):
                self.path.write_text(self.content.replace(before, after))
                with self.assertRaises(ValueError):
                    load_settings(self.path)
        for value in ["0", "-1", "true", '"30"']:
            with self.subTest(timeout=value):
                self.path.write_text(f"stage_timeout_seconds = {value}\n" + self.content)
                with self.assertRaises(ValueError):
                    load_settings(self.path)


if __name__ == "__main__":
    unittest.main()
