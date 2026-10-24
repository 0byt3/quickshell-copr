import json
import shutil
import smtplib
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import patch

from qscopr.automate import main, send_email, smtp_settings


class SMTPSettingsTests(unittest.TestCase):
    def test_secure_defaults(self):
        settings = smtp_settings({"host": "smtp.example.com"})
        self.assertEqual(settings["security"], "starttls")
        self.assertEqual(settings["port"], 587)
        self.assertEqual(settings["timeout_seconds"], 10)

    def test_implicit_tls_default_port(self):
        settings = smtp_settings({
            "host": "smtp.example.com",
            "security": "tls",
        })
        self.assertEqual(settings["port"], 465)

    def test_invalid_or_insecure_settings_are_refused(self):
        for change in [
            {"host": ""},
            {"host": "smtp.example.com\n"},
            {"host": "smtp://smtp.example.com"},
            {"host": 25},
            {"security": "none"},
            {"security": False},
            {"port": 0},
            {"port": 65536},
            {"port": True},
            {"timeout_seconds": 0},
            {"timeout_seconds": 61},
            {"timeout_seconds": "10"},
            {"username": "user"},
            {"password_file": "/etc/smtp-password"},
            {
                "username": "",
                "password_file": "/etc/smtp-password",
            },
            {
                "username": "user\n",
                "password_file": "/etc/smtp-password",
            },
            {
                "username": "user",
                "password_file": "relative",
            },
            {"ca_file": "relative"},
            {"password": "must-not-be-inline"},
            {"verify": False},
        ]:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    smtp_settings({"host": "smtp.example.com"} | change)
        for value in [None, [], "smtp.example.com"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                smtp_settings(value)


class SMTPDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.password = Path(temporary.name) / "smtp-password"
        self.password.write_text("test-smtp-password\n")
        self.password.chmod(0o600)
        self.settings = {
            "alert_from": "builder@example.com",
            "alert_to": [
                "first@example.com",
                "second@example.com",
            ],
            "smtp": {
                "host": "smtp.example.com",
                "username": "smtp-user",
                "password_file": str(self.password),
            },
        }
        smtp_patcher = patch("qscopr.automate.smtplib.SMTP")
        self.smtp = smtp_patcher.start()
        self.addCleanup(smtp_patcher.stop)
        tls_patcher = patch("qscopr.automate.smtplib.SMTP_SSL")
        self.tls = tls_patcher.start()
        self.addCleanup(tls_patcher.stop)
        self.client = self.smtp.return_value.__enter__.return_value
        self.tls_client = self.tls.return_value.__enter__.return_value
        for client in [self.client, self.tls_client]:
            client.ehlo.return_value = (250, b"ready")
            client.send_message.return_value = {}

    def test_starttls_precedes_authentication_and_mail(self):
        send_email(self.settings, "Test subject", "Test body")
        self.smtp.assert_called_once_with("smtp.example.com", 587, timeout=10)
        self.tls.assert_not_called()
        self.assertEqual([call[0] for call in self.client.method_calls], [
            "ehlo",
            "starttls",
            "ehlo",
            "login",
            "send_message",
        ])
        context = self.client.starttls.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.client.login.assert_called_once_with("smtp-user", "test-smtp-password")
        call = self.client.send_message.call_args
        self.assertEqual(call.kwargs["from_addr"], "builder@example.com")
        self.assertEqual(call.kwargs["to_addrs"], self.settings["alert_to"])
        message = call.args[0]
        self.assertEqual(message["Subject"], "Test subject")
        self.assertEqual(message.get_content(), "Test body\n")
        self.assertNotIn("test-smtp-password", message.as_string())

    def test_implicit_tls_uses_verified_context(self):
        self.settings["smtp"]["security"] = "tls"
        send_email(self.settings, "Test", "Body")
        self.smtp.assert_not_called()
        self.assertEqual(self.tls.call_args.args, ("smtp.example.com", 465))
        context = self.tls.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.tls_client.starttls.assert_not_called()
        self.tls_client.login.assert_called_once()
        self.tls_client.send_message.assert_called_once()

    def test_missing_starttls_never_sends_credentials_or_mail(self):
        self.client.starttls.side_effect = smtplib.SMTPNotSupportedError("STARTTLS unavailable")
        with self.assertRaises(smtplib.SMTPNotSupportedError):
            send_email(self.settings, "Test", "Body")
        self.client.login.assert_not_called()
        self.client.send_message.assert_not_called()
        self.tls.assert_not_called()

    def test_bad_certificate_never_sends_credentials_or_mail(self):
        self.client.starttls.side_effect = ssl.SSLCertVerificationError("untrusted")
        with self.assertRaises(ssl.SSLCertVerificationError):
            send_email(self.settings, "Test", "Body")
        self.client.login.assert_not_called()
        self.client.send_message.assert_not_called()

    def test_authentication_failure_stops_mail_submission(self):
        self.client.login.side_effect = smtplib.SMTPAuthenticationError(535, b"denied")
        with self.assertRaises(smtplib.SMTPAuthenticationError):
            send_email(self.settings, "Test", "Body")
        self.client.send_message.assert_not_called()

    def test_rejected_ehlo_before_or_after_tls_stops_delivery(self):
        for replies in [
            [(550, b"rejected")],
            [(250, b"ready"), (550, b"rejected")],
        ]:
            with self.subTest(replies=replies):
                self.client.ehlo.side_effect = replies
                with self.assertRaises(smtplib.SMTPHeloError):
                    send_email(self.settings, "Test", "Body")
        self.client.login.assert_not_called()
        self.client.send_message.assert_not_called()

    def test_partial_recipient_refusal_is_not_success(self):
        self.client.send_message.return_value = {
            "second@example.com": (550, b"rejected"),
        }
        with self.assertRaises(smtplib.SMTPRecipientsRefused):
            send_email(self.settings, "Test", "Body")

    def test_relay_without_authentication_still_requires_tls(self):
        self.settings["smtp"] = {"host": "relay.example.com"}
        send_email(self.settings, "Test", "Body")
        self.client.starttls.assert_called_once()
        self.client.login.assert_not_called()
        self.client.send_message.assert_called_once()

    def test_insecure_password_permissions_fail_before_connecting(self):
        self.password.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "private"):
            send_email(self.settings, "Test", "Body")
        self.smtp.assert_not_called()
        self.tls.assert_not_called()

    def test_cli_redacts_smtp_exception_details(self):
        self.client.login.side_effect = smtplib.SMTPAuthenticationError(
            535, b"server echoed test-smtp-password",
        )
        with patch.object(sys, "argv", [
            "automate", "--config", "/unused", "--test-email",
        ]), patch("qscopr.automate.load_settings", return_value=self.settings), patch("sys.stderr") as stderr:
            self.assertEqual(main(), 1)
        output = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("SMTPAuthenticationError", output)
        self.assertNotIn("test-smtp-password", output)


class CaptureSMTPHandler(socketserver.StreamRequestHandler):
    """Local test server: capture mail in memory, never relay it."""

    def setup(self):
        if self.server.security == "tls":
            self.request = self.server.context.wrap_socket(self.request, server_side=True)
        super().setup()
        self.connection.settimeout(5)

    def reply(self, data):
        self.wfile.write(data)
        self.wfile.flush()

    def handle(self):
        self.reply(b"220 localhost SMTP test server\r\n")
        while line := self.rfile.readline():
            verb = line.split(maxsplit=1)[0].upper()
            self.server.commands.append(verb)
            if verb == b"EHLO":
                self.reply(b"250-localhost\r\n250 STARTTLS\r\n")
            elif verb == b"STARTTLS":
                self.reply(b"220 Begin TLS\r\n")
                self.rfile.close()
                self.wfile.close()
                self.connection = self.server.context.wrap_socket(self.connection, server_side=True)
                self.rfile = self.connection.makefile("rb")
                self.wfile = self.connection.makefile("wb")
            elif verb in [b"MAIL", b"RSET"]:
                self.reply(b"250 OK\r\n")
            elif verb == b"RCPT":
                self.reply(b"550 Recipient refused\r\n" if self.server.refuse else b"250 OK\r\n")
            elif verb == b"DATA":
                self.reply(b"354 End with dot\r\n")
                lines = []
                while (data := self.rfile.readline()) not in [b".\r\n", b""]:
                    lines.append(data[1:] if data.startswith(b"..") else data)
                self.server.messages.append(b"".join(lines))
                self.reply(b"250 Accepted\r\n")
            elif verb == b"QUIT":
                self.reply(b"221 Bye\r\n")
                return
            else:
                self.reply(b"500 Unsupported\r\n")

    def finish(self):
        try:
            super().finish()
        finally:
            self.connection.close()


class CaptureSMTPServer(socketserver.TCPServer):
    allow_reuse_address = True

    def __init__(self, context, security):
        self.context = context
        self.security = security
        self.commands = []
        self.messages = []
        self.errors = []
        self.refuse = False
        super().__init__(("127.0.0.1", 0), CaptureSMTPHandler)

    def handle_error(self, request, client_address):
        self.errors.append(type(sys.exception()).__name__)


@unittest.skipUnless(shutil.which("openssl"), "Local TLS integration tests require the openssl executable")
class SMTPCLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name)
        cls.certificate = cls.directory / "certificate.pem"
        key = cls.directory / "key.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(cls.certificate),
                "-days", "1", "-subj", "/CN=localhost",
                "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(cls.certificate, key)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)

    @contextmanager
    def server(self, security="starttls"):
        server = CaptureSMTPServer(self.context, security)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def invoke(self, server, *arguments, trust=True):
        config = self.project / "automation.toml"
        content = (
            'alert_from = "builder@example.com"\n'
            'alert_to = ["operator@example.com"]\n'
            f"copr_login_file = {json.dumps(str(self.project / 'absent-login'))}\n"
            f"copr_token_file = {json.dumps(str(self.project / 'absent-token'))}\n"
            '[smtp]\nhost = "127.0.0.1"\n'
            f"port = {server.server_address[1]}\n"
            f"security = {json.dumps(server.security)}\n"
            "timeout_seconds = 5\n"
        )
        if trust:
            content += f"ca_file = {json.dumps(str(self.certificate))}\n"
        config.write_text(content)
        return subprocess.run(
            [
                sys.executable, "-m", "qscopr.automate",
                "--project", str(self.project), "--config", str(config),
                *arguments,
            ],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_cli_test_email_over_starttls_and_implicit_tls(self):
        for security in ["starttls", "tls"]:
            with self.subTest(security=security), self.server(security) as server:
                result = self.invoke(server, "--test-email")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len(server.messages), 1)
                message = BytesParser(policy=policy.default).parsebytes(server.messages[0])
                self.assertEqual(message["Subject"], "Quickshell COPR alert test")
                self.assertEqual(message["To"], "operator@example.com")
                self.assertEqual(server.commands.count(b"STARTTLS"), int(security == "starttls"))
                self.assertEqual(server.errors, [])
                self.assertFalse((self.project / "artifacts").exists())

    def test_real_pipeline_preflight_failure_sends_smtp_alert(self):
        with self.server() as server:
            result = self.invoke(server)
            self.assertEqual(result.returncode, 1)
            record = json.loads((self.project / "artifacts/automation/latest.json").read_text())
            self.assertEqual(record["stage"], "preflight")
            self.assertEqual(record["status"], "failed")
            self.assertTrue(record["alert_sent"])
            self.assertEqual(len(server.messages), 1)

    def test_real_recipient_refusal_returns_nonzero(self):
        with self.server() as server:
            server.refuse = True
            result = self.invoke(server, "--test-email")
            self.assertEqual(result.returncode, 1)
            self.assertIn("SMTPRecipientsRefused", result.stderr)
            self.assertEqual(server.messages, [])

    def test_untrusted_certificate_is_rejected_without_sending_mail(self):
        with self.server() as server:
            result = self.invoke(server, "--test-email", trust=False)
            self.assertEqual(result.returncode, 1)
            self.assertIn("SSLCertVerificationError", result.stderr)
            self.assertNotIn(b"MAIL", server.commands)
            self.assertEqual(server.messages, [])


if __name__ == "__main__":
    unittest.main()
