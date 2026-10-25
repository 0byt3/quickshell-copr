"""Single-command, fail-fast release automation for systemd and cron."""

import argparse
import fcntl
import json
import os
import smtplib
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import uuid
from datetime import datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

from .source import sha256


SECRET_ENVIRONMENT = (
    "COPR_LOGIN",
    "COPR_TOKEN",
    "ENABLE_COPR_PUBLISH",
    "SMTP_PASSWORD",
)


def clean_environment():
    return {
        key: value
        for key, value in os.environ.items()
        if key not in SECRET_ENVIRONMENT
    }


def validate_address(address):
    if not isinstance(address, str) or "\n" in address or "\r" in address:
        raise ValueError("Invalid alert address")
    parsed = Address(addr_spec=address)
    if not parsed.username or not parsed.domain:
        raise ValueError("Alert addresses must include a domain")


def load_settings(path):
    settings = tomllib.loads(path.read_text())
    if "alert_from" in settings and settings["alert_from"] is not None:
        validate_address(settings["alert_from"])
    if "alert_to" in settings and settings["alert_to"] is not None:
        recipients = settings["alert_to"]
        if not isinstance(recipients, list) or not recipients:
            raise ValueError("alert_to must be a nonempty list")
        for address in recipients:
            validate_address(address)
    if "sendmail" in settings:
        raise ValueError("Replace the sendmail setting with an smtp table")
    if settings.get("smtp"):
        settings["smtp"] = smtp_settings(settings["smtp"])
    else:
        settings["smtp"] = {}
    settings.setdefault("stage_timeout_seconds", 18000)
    for key in ["copr_login_file", "copr_token_file"]:
        if not isinstance(settings[key], str) or not Path(settings[key]).is_absolute():
            raise ValueError(f"{key} must be an absolute path")
    timeout = settings["stage_timeout_seconds"]
    if type(timeout) is not int or timeout < 1:
        raise ValueError("stage_timeout_seconds must be a positive integer")
    return settings


def smtp_settings(value):
    if not isinstance(value, dict):
        raise ValueError("An smtp table is required")
    allowed = (
        "host",
        "port",
        "security",
        "timeout_seconds",
        "username",
        "password_file",
        "ca_file",
    )
    if any(key not in allowed for key in value):
        raise ValueError("Unsupported SMTP setting; passwords belong in a private file")
    smtp = value.copy()
    host = smtp.get("host")
    if not isinstance(host, str) or not host or any(character.isspace() for character in host) or "/" in host:
        raise ValueError("smtp.host must be a hostname or IP address")
    smtp.setdefault("security", "starttls")
    if smtp["security"] not in ["starttls", "tls"]:
        raise ValueError("smtp.security must be starttls or tls; plaintext SMTP is not supported")
    smtp.setdefault("port", 465 if smtp["security"] == "tls" else 587)
    smtp.setdefault("timeout_seconds", 10)
    for key, maximum in [("port", 65535), ("timeout_seconds", 60)]:
        if type(smtp[key]) is not int or not 1 <= smtp[key] <= maximum:
            raise ValueError(f"smtp.{key} must be an integer between 1 and {maximum}")
    if ("username" in smtp) != ("password_file" in smtp):
        raise ValueError("smtp.username and smtp.password_file must be configured together")
    if "username" in smtp:
        username = smtp["username"]
        if not isinstance(username, str) or not username.strip() or "\r" in username or "\n" in username:
            raise ValueError("smtp.username must be a nonempty single-line string")
    for key in ["password_file", "ca_file"]:
        if key in smtp and (not isinstance(smtp[key], str) or not Path(smtp[key]).is_absolute()):
            raise ValueError(f"smtp.{key} must be an absolute path")
    return smtp


def credential(path):
    path = Path(path)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) not in [
        0o400,
        0o600,
    ]:
        raise ValueError("Credential files must be private regular files (0400 or 0600)")
    value = path.read_text().strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("Credential files must contain one nonempty line")
    return value


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, indent=4, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def email_configured(settings):
    if not settings.get("alert_from") or not settings.get("alert_to"):
        return False
    smtp = settings.get("smtp")
    if not isinstance(smtp, dict) or not smtp.get("host"):
        return False
    smtp = smtp_settings(smtp)
    if "password_file" in smtp and not Path(smtp["password_file"]).is_file():
        return False
    if "ca_file" in smtp and not Path(smtp["ca_file"]).is_file():
        return False
    return True


def send_email(settings, subject, body):
    if not settings.get("alert_from") or not settings.get("alert_to"):
        raise ValueError("alert_from and alert_to are required to send email")
    smtp = smtp_settings(settings.get("smtp"))
    # Never put passwords in a subprocess environment, command line, debug log,
    # notification body, or the sanitized exception reports used by the CLI.
    password = credential(smtp["password_file"]) if "password_file" in smtp else None
    context = ssl.create_default_context(cafile=smtp.get("ca_file"))
    message = EmailMessage()
    message["From"] = settings["alert_from"]
    message["To"] = ", ".join(settings["alert_to"])
    message["Date"] = formatdate(localtime=True)
    message["Subject"] = subject
    message.set_content(body)
    factory = smtplib.SMTP_SSL if smtp["security"] == "tls" else smtplib.SMTP
    options = {"context": context} if smtp["security"] == "tls" else {}
    with factory(smtp["host"], smtp["port"], timeout=smtp["timeout_seconds"], **options) as client:
        code, response = client.ehlo()
        if code != 250:
            raise smtplib.SMTPHeloError(code, response)
        if smtp["security"] == "starttls":
            # Fail closed if STARTTLS is absent or certificate validation fails.
            # Authentication and mail submission happen only after TLS.
            client.starttls(context=context)
            code, response = client.ehlo()
            if code != 250:
                raise smtplib.SMTPHeloError(code, response)
        if password is not None:
            client.login(smtp["username"], password)
        refused = client.send_message(
            message,
            from_addr=settings["alert_from"],
            to_addrs=settings["alert_to"],
        )
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def failure_email(settings, record):
    if not email_configured(settings):
        print(
            "Alert email skipped: SMTP alert settings or credentials are not configured.",
            file=sys.stderr,
            flush=True,
        )
        return False
    stage = record.get("stage", "service")
    send_email(
        settings,
        f"Quickshell COPR pipeline failed on {socket.gethostname()}: {stage}",
        f"Host: {socket.gethostname()}\n"
        f"Stage: {stage}\n"
        f"Status: {record.get('status', 'interrupted')}\n"
        f"Error type: {record.get('error_type', 'service failure or interruption')}\n"
        f"Exit code: {record.get('exit_code', 'unknown')}\n"
        f"Artifacts: {record.get('output', 'See the service journal')}\n\n"
        "The pipeline stopped. Inspect pipeline.log, gate.json, and COPR result "
        "files where present. No later stage was started after the failure.\n",
    )
    return True


def validation_identity(project):
    return {
        name: sha256(project / "packaging" / name)
        for name in [
            "validated.json",
            "quickshell.spec",
            "cpptrace.spec",
        ]
    }


def execute_stage(project, output, settings, stage, arguments, update, publishing=False):
    update(stage=stage)
    environment = clean_environment()
    if publishing:
        environment.update({
            "ENABLE_COPR_PUBLISH": "true",
            "COPR_LOGIN": credential(settings["copr_login_file"]),
            "COPR_TOKEN": credential(settings["copr_token_file"]),
        })
    print(f"Stage: {stage}; log: {output / 'pipeline.log'}", flush=True)
    with (output / "pipeline.log").open("a") as log:
        log.write(f"\n=== {stage} ===\n")
        log.flush()
        read_fd, write_fd = os.pipe()

        def forward():
            with os.fdopen(read_fd, "r", errors="replace") as stream:
                for line in stream:
                    log.write(line)
                    log.flush()
                    print(line, end="", flush=True)

        reader = threading.Thread(target=forward, daemon=True)
        reader.start()
        try:
            subprocess.run(
                [sys.executable, *arguments],
                cwd=project,
                env=environment,
                stdout=write_fd,
                stderr=subprocess.STDOUT,
                timeout=settings["stage_timeout_seconds"],
                check=True,
            )
        finally:
            os.close(write_fd)
            reader.join(timeout=5)


def log_gate_report(report):
    release = report.get("release") or {}
    tag = release.get("tag", "unknown")
    commit = release.get("commit", "unknown")
    message = report.get("message", "")
    summary = f"Gate status: {report.get('status', 'unknown')} (release: {tag}, commit: {commit})"
    if message:
        summary += f" - {message}"
    print(summary, flush=True)


def pipeline(project, root, output, settings, update, tag=None, force=False):
    def stage(name, arguments, publishing=False):
        execute_stage(project, output, settings, name, arguments, update, publishing)

    update(stage="preflight")
    if email_configured(settings):
        smtp = smtp_settings(settings["smtp"])
        if "password_file" in smtp:
            credential(smtp["password_file"])
    # Check credential availability before an expensive build; values stay private.
    credential(settings["copr_login_file"])
    credential(settings["copr_token_file"])
    stage("publisher-dependency", ["-c", "import copr.v3"])
    stage("tests", ["-m", "unittest", "discover", "-s", "tests", "-v"])
    stage("syntax", ["-m", "compileall", "-q", "qscopr", "tests"])
    checked = output / "check"
    arguments = ["-m", "qscopr", "check", "--output", str(checked)]
    if tag:
        arguments.extend(["--tag", tag])
    if force:
        arguments.append("--force")
    stage("check", arguments)
    report = json.loads((checked / "gate.json").read_text())
    log_gate_report(report)
    if report["status"] == "unchanged":
        identity = validation_identity(project)
        receipt = root / "published.json"
        if receipt.exists() and json.loads(receipt.read_text())["validation"] == identity:
            print("Release already validated and published to COPR; skipping.", flush=True)
            return "unchanged"
        # Promotion is not proof of publication. Rebuild the maintained release
        # when there is no matching success receipt, even after an interruption.
        checked = output / "verify"
        stage("revalidate", [
            "-m", "qscopr", "check", "--verify-tracked", "--output", str(checked),
        ])
        report = json.loads((checked / "gate.json").read_text())
        log_gate_report(report)
    elif report["status"] == "validated":
        stage("promote", ["-m", "qscopr", "promote", "--output", str(checked)])
    if report["status"] != "validated":
        raise ValueError("Only a validated build can be published")
    stage("publication", ["-m", "qscopr", "publication", "--output", str(checked)])
    identity = validation_identity(project)
    stage("publish", ["-m", "qscopr.publish", "--output", str(checked)], publishing=True)
    results = {}
    for filename in ["cpptrace-copr-result.json", "copr-result.json"]:
        result = json.loads((checked / filename).read_text())
        print(
            f"COPR result ({result.get('name', filename)}): build_id={result.get('build_id')} "
            f"state={result.get('state')} chroot_state={result.get('chroot_state')} "
            f"url={result.get('url', 'n/a')}",
            flush=True,
        )
        if result["state"] != "succeeded" or result["chroot_state"] != "succeeded":
            raise ValueError("Both COPR builds must succeed before recording publication")
        results[filename] = result
    atomic_json(root / "published.json", {
        "validation": identity,
        "results": results,
        "output": str(output),
        "published_at": datetime.now(timezone.utc).isoformat(),
    })
    return "published"


def run(project, settings, tag=None, force=False):
    root = project / "artifacts" / "automation"
    record = {
        "status": "running",
        "stage": "startup",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "alert_sent": False,
        "invocation_id": os.environ.get("INVOCATION_ID"),
    }
    output = None

    def update(**changes):
        record.update(changes)
        if output is not None:
            atomic_json(output / "run.json", record)
            atomic_json(root / "latest.json", record)

    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("Another automation run holds the project lock; skipping.", flush=True)
                return 0
            try:
                name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
                output = root / name
                output.mkdir()
                update(output=str(output))
                status = pipeline(project, root, output, settings, update, tag, force)
                update(status=status, stage="complete")
                print(f"Pipeline {status}; artifacts: {output}", flush=True)
                return 0
            except Exception as error:
                code = error.returncode if isinstance(error, subprocess.CalledProcessError) else 1
                record.update(status="failed", error_type=type(error).__name__, exit_code=code)
                try:
                    update()
                except OSError:
                    print("Could not persist failure metadata; attempting email anyway.", file=sys.stderr)
                if failure_email(settings, record):
                    update(alert_sent=True)
                print(
                    f"Pipeline failed at {record['stage']} ({record['error_type']}, exit_code={record['exit_code']}); "
                    f"artifacts: {output}",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
    except Exception as error:
        # This also covers log/state-directory errors and failed SMTP submission.
        # Never print exception values: external clients may include credentials.
        print(f"Automation stopped ({type(error).__name__}); stage: {record['stage']}", file=sys.stderr)
        if record["status"] != "failed":
            try:
                record.update(status="failed", error_type=type(error).__name__)
                failure_email(settings, record)
            except Exception as mail_error:
                print(f"Failure email could not be submitted ({type(mail_error).__name__}).", file=sys.stderr)
        return 1


def notify_failure(project, settings):
    path = project / "artifacts" / "automation" / "latest.json"
    try:
        record = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        record = {}
    if not isinstance(record, dict):
        record = {}
    invocation = os.environ.get("MONITOR_INVOCATION_ID")
    if invocation and invocation != record.get("invocation_id"):
        # The service may have failed before Python started. An older run's
        # successful alert must not suppress a new startup-failure notification.
        record = {}
    if record.get("status") == "failed" and record.get("alert_sent"):
        return
    record = record | {
        "status": "failed",
        "error_type": os.environ.get("MONITOR_SERVICE_RESULT", "service failure or interruption"),
    }
    if "MONITOR_EXIT_STATUS" in os.environ:
        record["exit_code"] = os.environ["MONITOR_EXIT_STATUS"]
    failure_email(settings, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path, required=True, help="Server automation TOML settings")
    parser.add_argument("--tag", help="Select a published stable release instead of latest")
    parser.add_argument("--force", action="store_true", help="Validate a higher RPM Release for a rebuild/retry")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--test-email", action="store_true", help="Submit a test email without building or publishing")
    mode.add_argument("--notify-only", action="store_true", help="systemd OnFailure fallback for terminated jobs")
    args = parser.parse_args()
    try:
        settings = load_settings(args.config.resolve())
        project = args.project.resolve()
        if args.test_email:
            send_email(settings, "Quickshell COPR alert test", "This alert was submitted directly over SMTP.\n")
            print("Test email accepted by the SMTP server; verify receipt.")
            return 0
        if args.notify_only:
            notify_failure(project, settings)
            return 0
        return run(project, settings, args.tag, args.force)
    except Exception as error:
        print(f"Automation setup/notification failed ({type(error).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
