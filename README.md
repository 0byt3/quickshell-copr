# Quickshell → Fedora 44 → COPR

A conservative release gate for Quickshell. It discovers requirements from the
selected stable release, checks **official Fedora 44 release and updates
repositories**, and builds RPMs offline before allowing maintained spec updates.

No third-party COPRs, Rawhide, updates-testing, vendored dependencies, or automatic
feature disabling are used to make a release pass.

**cpptrace is the one explicitly source-built prerequisite.** The pipeline builds
the reviewed version pinned by Quickshell (currently v1.0.4), runs its upstream
tests, and packages it as runtime/devel RPMs. All of cpptrace's own dependencies
must resolve from official Fedora repositories. Quickshell uses those installed
RPMs, never a vendored source tree.

**GitHub Actions is not used.** The complete unattended pipeline runs on your own
server with one command, from the checkout:

```bash
python3 -m qscopr.automate --config /etc/quickshell-copr/automation.toml
```

This command **publishes to COPR** after successful validation. Configure the
server, credentials, and alert delivery as described below before running it.
For a build/check without publication, use the local commands in the next section.

## Local use

Requirements:

- Python 3.12 or newer.
- Docker with daemon access, or Podman if Docker is not installed.
- Internet access for upstream source, the Fedora image, and official repositories.
- An x86_64 host, or working linux/amd64 container emulation.

The checker itself uses only Python's standard library. Install tools using your
distribution's repositories. It installs build packages **inside disposable
containers**, not on the host. No Git setup or COPR credentials are needed to check.

Run from this directory:

```bash
python3 -m qscopr check --output artifacts/first-check
```

Check a particular published stable release:

```bash
python3 -m qscopr check --tag v0.3.2 --output artifacts/v0.3.2
```

Use a **new output directory for every attempt**; an existing directory is refused
to prevent stale artifacts from being mistaken for a successful build.

Generate/update the maintained packaging only after success:

```bash
python3 -m qscopr check --output artifacts/update --promote
```

Alternatively, promote an unchanged, previously validated local attempt:

```bash
python3 -m qscopr promote --output artifacts/first-check
```

Promotion writes:

- `packaging/quickshell.spec`
- `packaging/cpptrace.spec`
- `packaging/validated.json`
- `packaging/dependencies.json`

There is deliberately no maintained spec until the first successful gate. A
candidate spec can exist in a blocked attempt's artifacts, but is **not approved**.
The tool does not create Git repositories, commit, push, or publish by checking or
promoting.

## How the gate works

1. Query the GitHub upstream mirror's latest stable release (or an explicit stable
   tag). Drafts/prereleases and non-semantic release tags are refused.
2. Resolve the tag to its commit, download that commit's archive, compute SHA-256,
   and safely extract it. Links, traversal paths, devices, and oversized archives
   are refused.
3. Inventory release CMake `find_package`, `pkg_check_modules`, Qt component lists,
   minimum versions, feature defaults, and documented dependency lists. Map these
   to Fedora RPM package/capability requirements with source locations.
4. Resolve Quickshell's cpptrace pin against the reviewed commit and SHA-256 in
   `config.toml`. Changed pins, moved tags, or unreviewed profiles block the gate.
   Inventory cpptrace's system library and GTest/GMock requirements.
5. Query the combined requirements with DNF inside the official Fedora 44 container. Only
   `fedora` and `updates` are enabled; unavailable repositories fail the run.
   Check both individual providers and installation of the complete transaction.
   Only `cmake(cpptrace)` is supplied by this pipeline rather than those repositories.
6. Generate both candidate specs from their inventories and parse them with
   `rpmspec`. After dependency installation, snapshot the prepared container.
7. Start an **unprivileged, capability-free, network-disabled** cpptrace build container,
   run upstream CTest and signal-safe unwinding/trace smoke checks, and generate
   runtime/devel RPMs and an SRPM. Use system libunwind, libdwarf, zstd, and test
   libraries; dependency downloads are disabled.
8. Start a separate offline installer from the clean preparation snapshot. Verify
   and install only cpptrace's runtime/devel RPMs with RPM dependency checks intact,
   then verify installed providers for every Quickshell BuildRequires.
9. Start a fresh **unprivileged, capability-free, network-disabled** build container,
   without host bind mounts, host Docker socket, or publishing credentials.
   Compile/run a CMake consumer against the installed cpptrace package, without
   access to cpptrace's build tree.
   Run `rpmbuild -ba`: CMake configure, Ninja compile, staged install, `%check`
   executable-version smoke test, file manifest checks, RPM and SRPM creation.
10. Verify outputs and record artifact hashes. Only a fully `validated` attempt can be
   promoted. Promotion refuses modified inputs/spec/inventory/RPMs, downgrades,
   moved already-validated tags, and non-increasing rebuild release numbers.

The dependency inventory is intentionally a **conservative union of conditional
branches**, not a general CMake interpreter. It may include a dependency for an
inactive feature/test branch; it will block rather than guess if that dependency
is unavailable. Known Fedora 44 mappings account for Qt 6.10 moving Wayland client
development files into qtbase. Unrecognized packages, Qt components, version
expressions, dependency-discovery methods, or FetchContent locations need a
reviewed handler/mapping change in `qscopr/discovery.py`.

This is not a guarantee that arbitrary future CMake logic can be inferred
statically. The offline configure/build is the final compatibility authority.
Supported upstream features retain their defaults; only vendoring is explicitly
disabled. No graphical desktop session is started, so successful compilation and
the command-line smoke test are **not desktop runtime verification**.

## Results and artifacts

`gate.json` is the attempt's authoritative report. Outcomes are:

- `validated`: the RPM build and package checks passed.
- `dependency-blocked`: official packages are missing or a recognized dependency
  solver conflict prevents installing the complete transaction.
- `discovery-blocked`: new/unhandled requirements or a promotion/publication guard
  needs human attention.
- `build-failed`: configure, compile, install, or package checks failed.
- `infrastructure-failed`: network, repository metadata, container, or unexpected
  tooling failures prevented a conclusive check.
- `unchanged`: the latest release and build inputs were already validated.

CLI exit codes:

- `0`: validated/unchanged, or successful promotion/publication-manifest generation.
- `1`: build/infrastructure failure.
- `2`: dependency/discovery blocked, or a refused operation.

Depending on the stage reached, artifacts include:

- `inventory.json`, with requirements and their source locations.
- `candidate.spec` and `spec-expanded.log`.
- `cpptrace.spec`, `cpptrace-result.json`, and prerequisite build/install/consumer logs.
- `providers.json`, repository definitions and metadata checksums.
- Container, repository, dependency resolution, and transaction logs.
- Installed package versions and a dependency fingerprint.
- Both packages' RPM build logs, file lists, runtime requirements, RPMs/SRPMs,
  and their hashes. Completed cpptrace artifacts survive a later Quickshell failure.
- The source archive checksum, commit, and Fedora image identity in `gate.json`.

The release source archive is temporary; RPM success embeds it in the SRPM.
Containers and their temporary prepared image are removed automatically on normal
exit. A killed host process may leave UUID-named `qscopr-*` resources to inspect and
remove manually. The pulled Fedora base image remains cached.

Only validated packaging is maintained. Blocked releases are tried again on the
next scheduled run, allowing Fedora package updates to unblock them. The server
runner treats blocked releases as failures and emails an alert.

## Server automation

The entry point is `python3 -m qscopr.automate`. No GitHub Actions, GitHub tokens,
automatic commits, pushes, or packaging pull requests are involved. GitHub is
still used to discover/download upstream releases.

Each invocation:

1. Acquires an exclusive project lock. An overlapping invocation skips with exit
   `0` without changing the active run's state.
2. Checks SMTP settings, private credential files, and the Python COPR client.
3. Runs the unit tests and Python syntax checks.
4. Discovers the selected/latest stable release and runs the Fedora container gate.
5. Promotes successful new/rebuilt packaging directly into `packaging/`.
6. Verifies the publication manifest and artifact hashes.
7. Publishes cpptrace to COPR, waits for overall/chroot success, then publishes
   Quickshell and waits for its success.
8. Records a publication receipt **only after both COPR builds succeed**.

The first failed or blocked stage stops the pipeline, prevents later stages,
submits a failure email, and returns `1`. The underlying checker retains its
separate `0`/`1`/`2` exit codes; the runner records that stage's exit code in
`run.json`. Success or an already-published unchanged release returns `0`.

An `unchanged` release is skipped only when the maintained validation/spec hashes
match `artifacts/automation/published.json`. If promotion succeeded but publication
failed or was interrupted, the next run **revalidates the maintained release and
retries publication**, rather than silently treating it as finished. Existing
COPR builds are monitored/reused instead of blindly uploaded again.

### Server prerequisites and configuration

Use a dedicated service account and a persistent, writable checkout, such as
`/srv/quickshell-copr`. Install software from your server distribution's
repositories:

- Python 3.12 or newer.
- The Python COPR client (`python3-copr` on Fedora), available to the same Python
  interpreter used by the service. No pip installation is needed on Fedora.
- Docker with daemon access, or Podman if Docker is absent. The checker prefers
  Docker when both are installed, so it must actually work for the service account.
- An SMTP provider or trusted relay that supports STARTTLS or implicit TLS.
  Alerts use Python's standard library directly; no sendmail, Postfix, local mail
  service, or extra Python email package is required.
- System CA certificates for verifying the SMTP server, or a configured private
  CA certificate file when using your own relay.
- Network access to upstream GitHub, Fedora repositories/container registry,
  COPR, and the SMTP server, plus sufficient disk/RAM for the builds.

For rootless Podman, configure the service account's runtime directory/user
manager as required by the server distribution. The supplied system units do not
provision that runtime. Docker daemon access is effectively root-level access;
grant it only to a trusted service account.

Configure the server:

1. Review `config.toml`: Fedora 44 x86_64, compilation `jobs` (default `2`), and the
   destination COPR project/distributor. The current destination is
   `byte0/quickshell`; change it before validating if using another project.
2. Configure the COPR project with `fedora-44-x86_64` enabled, without third-party
   repositories or injected buildroot packages. The tool does not change the
   project's COPR configuration.
3. Copy `automation.example.toml` to `/etc/quickshell-copr/automation.toml`, edit
   the sender/recipient addresses, `[smtp]` connection settings, and credential
   paths, and make it readable by the service account. Keep server configuration
   outside the checkout. Remove the old `sendmail` key when migrating.
4. Obtain credentials from your COPR API page. Store the login and token in
   separate files at the configured paths, each containing **only its value**,
   not `KEY=value`. If your SMTP provider requires authentication, also store its
   password/app password in the separate `smtp.password_file`. Create/populate
   these files using your editor or secret manager, never an inline shell command
   that leaves credentials in history.
5. Make credential files owned by the service account with mode `0400` or `0600`,
   and protect their parent directory. Group/world-readable files are refused.
6. Make the checkout, `packaging/`, and `artifacts/` writable by the service account.
   Do not run independent manual packaging/publishing commands concurrently with
   the automation runner; they do not participate in its lock.

COPR credentials are supplied only in the separate publisher subprocess's
environment, never in command arguments, test/check subprocess environments,
container build environments, email bodies, or log attachments. The runner itself
also privately reads the credential files during preflight. The SMTP password is
read directly from its private file and is never supplied to build, test, or
publisher subprocesses.

### Direct SMTP alerts

Configure the `[smtp]` table in `automation.example.toml` for your provider:

- `host`: SMTP server hostname, not an `smtp://` URL.
- `security = "starttls"`: require STARTTLS before authentication or sending.
  The default port is `587`.
- `security = "tls"`: establish TLS immediately. The default port is `465`.
- `username` and `password_file`: configure both for authenticated submission.
  Use an application password if your provider requires one. The sender address
  must be permitted by that provider.
- For a trusted relay without authentication, omit **both** authentication keys.
  TLS is still required.
- `timeout_seconds`: socket-operation timeout, default `10`, allowed `1`–`60`.
- `ca_file`: optional absolute path to a private relay's CA certificate bundle;
  otherwise Python uses the system CA trust store.

Certificate-chain and hostname verification are always enabled. There is no
plaintext mode, automatic TLS downgrade, or certificate-verification bypass.
SMTP authentication failures, connection/TLS errors, and rejection of **any**
recipient are failures, not successful notification delivery. SMTP error details
are not printed because a server response may contain sensitive information.

Before enabling a schedule, run this **as the service account**, from the checkout:

```bash
python3 -m qscopr.automate --config /etc/quickshell-copr/automation.toml --test-email
```

Verify the email actually arrives. Exit `0` only confirms acceptance by the SMTP
server for every configured recipient, not final inbox delivery. Then run the
full command manually once:

```bash
python3 -m qscopr.automate --config /etc/quickshell-copr/automation.toml
```

### systemd timer

The executable `install` script renders the templates in `systemd/`, verifies
them with `systemd-analyze`, and installs the pipeline, fallback alert, and timer
units under `/etc/systemd/system/`. Provision the service account, dependencies,
checkout permissions, settings, and credentials described above first: the
installer does **not** create accounts, install packages, change ownership, or
configure a container runtime.

Preview the units without root or credentials; this does not install anything,
check service-account prerequisites, or change schedules:

```bash
./install --dry-run
```

Install on the **server**, from its persistent checkout:

```bash
sudo ./install
```

Defaults are the installer's checkout, existing user `quickshell-copr` (with its
primary group), `/etc/quickshell-copr/automation.toml`, and the Python interpreter
running the installer. Select a different interpreter explicitly if necessary;
it must provide Python 3.12+ and the COPR client for the service account.

Customize the paths, account, or systemd calendar:

```bash
sudo ./install \
    --project /srv/quickshell-copr \
    --config /etc/quickshell-copr/automation.toml \
    --user quickshell-copr \
    --python /usr/bin/python3 \
    --calendar '*-*-* 06:23:00 UTC'
```

Before installing, prerequisite checks run as the selected non-root account.
They validate configuration, credential-file metadata/readability without reading
the secrets, SMTP CA trust, checkout write access, and container-tool availability.
They do not contact COPR/SMTP, test container daemon access, build, or publish.
Test email delivery and container access separately as described above.

Installation reloads systemd and enables/starts **only the timer**. Because the
timer is persistent, a missed run can start immediately after enabling it and can
publish to COPR. Use `sudo ./install --no-enable` to install with the timer
**disabled and stopped**, including on an existing installation.

Rerun the installer to update units. It refuses to update while either service is
running, stops an existing timer during updates, and backs up changed existing
units to a private `quickshell-copr-backup-*` directory under
`/etc/systemd/system/`. Unchanged files are left intact. Symlinked/special unit
files are refused; resolve those manually. A failed installation returns nonzero
and may leave the timer stopped; inspect the error and retained backups before
retrying. There is no automatic rollback.

The timer runs daily at **06:23 UTC**, with up to ten minutes of randomized delay,
and catches up after downtime. The service has an eight-hour overall timeout;
individual stages default to five hours (`stage_timeout_seconds` in settings).
Adjust both limits together for slower servers. The alert service allows three
minutes for SMTP negotiation/submission; review that limit too if increasing
SMTP socket timeouts.

Inspect or trigger it:

```bash
systemctl list-timers quickshell-copr.timer
sudo systemctl start quickshell-copr.service
journalctl -u quickshell-copr.service -u quickshell-copr-alert.service
```

Ordinary failures are emailed by the runner. `OnFailure` starts the alert service
as a fallback if the main process is terminated, times out, or cannot submit its
alert. It avoids a duplicate alert when the runner already recorded successful
mail submission for that systemd invocation, without suppressing a new failure
using an older run's notification.

Email cannot be guaranteed during a host/network outage, a broken Python/settings
installation, or an unavailable SMTP provider. A missing/malformed settings file
exits nonzero but cannot provide its own email configuration. Monitor the server
and mail provider independently if delivery guarantees are important. There is no
local mail queue: failed SMTP submission returns nonzero and leaves the failure
unacknowledged, allowing the systemd fallback to try again. Repeated provider
outages still need independent monitoring.

### cron alternative

Use the dedicated account's crontab **instead of** the timer. Configure cron's
`PATH` so Docker/Podman can be found. This example uses the server's cron timezone:

```cron
PATH=/usr/local/bin:/usr/bin:/bin
MAILTO=""
23 6 * * * cd /srv/quickshell-copr && /usr/bin/python3 -m qscopr.automate --config /etc/quickshell-copr/automation.toml >> /srv/quickshell-copr/artifacts/cron.log 2>&1
```

The Python command owns the full flow and alerts on ordinary failures; no chain of
separate build/publish shell commands is needed. Create the writable `artifacts/`
directory before scheduling. Cron's own mail is disabled; alerts go directly over
SMTP and command output is retained in `artifacts/cron.log`. Unlike the supplied
systemd setup, plain cron has no `OnFailure` fallback if the runner itself is
killed. Prefer systemd for timeout/termination monitoring.

### Artifacts and recovery

Each run creates a unique directory under `artifacts/automation/` containing
`pipeline.log`, `run.json`, and `check/` or `verify/` gate artifacts.
`latest.json` records the latest run/stage. `published.json` is the last successful
publication receipt. COPR links and outcomes are in the gate attempt's
`cpptrace-copr-result.json` and `copr-result.json`.

- Dependency/discovery blocks and build failures stop and alert. Review the logs;
  the next scheduled run retries, without promoting a blocked build.
- Interrupted/incomplete publication is retried even when the release is
  unchanged. A failed/canceled/skipped COPR NVR cannot be reused: run the full
  command with `--force` to validate a higher RPM Release.
- If Fedora dependencies changed before an incomplete publication could be
  revalidated, use `--force` for a fresh validated rebuild instead of stale approval.
- Do not delete or edit maintained validation, hashes, or receipts to bypass
  guards. Preserve the checkout's `packaging/` and `published.json` when moving
  servers. Removing a success receipt causes revalidation, not blind trust.
- Code/configuration changes, including this migration away from Actions,
  invalidate previous build inputs and require a fresh validated build.
- Artifacts have **no automatic retention limit**. Prune/archive old per-run
  directories when no run is active, but preserve `published.json`, `latest.json`,
  and `.lock`. A forcibly killed build can also leave UUID-named container
  resources, as described above.

The local lock protects this checkout only. Avoid multiple servers/checkouts
publishing the same NVR concurrently: COPR has no transaction-wide exactly-once
upload lock here.

### Publication safeguards

Checking/promoting alone never publishes. The automation command intentionally
opts into publication; the separate `qscopr.publish` module still requires
`ENABLE_COPR_PUBLISH=true` and `COPR_LOGIN`/`COPR_TOKEN` in its environment when
invoked independently.

Publication verifies maintained specs, source identities, build-input hashes,
artifact checksums, and installed dependency fingerprints. The fresh successful
gate can be published directly after promotion; retrying an unchanged, incomplete
publication first rebuilds the tracked release with `--verify-tracked`.

The publisher uploads cpptrace first and waits for overall and Fedora 44 chroot
success before submitting Quickshell, recording the prerequisite build ID. It
does not use COPR's batch dependency option: batch completion alone does not
guarantee prerequisite success. Build networking is disabled for both. COPR
imports the submitted SRPM's spec; there is no direct edit of generated dist-git.

Paginated build history prevents blind duplicate uploads; existing builds are
monitored instead. COPR can still fail despite local validation, for example if
Fedora repositories change. Live publication and real email delivery are not
part of the unit tests.

## Qt updates and rebuilds

Quickshell uses private Qt APIs and upstream requires rebuilding against Qt
releases. New-Quickshell-release polling alone does not detect every Qt ABI change.

Force a same-version rebuild after Fedora/Qt updates:

```bash
python3 -m qscopr check --force --output artifacts/qt-rebuild --promote
```

This increases RPM `Release` for an already validated version. To run the **entire
pipeline including COPR publication** after Fedora/Qt updates:

```bash
python3 -m qscopr.automate --config /etc/quickshell-copr/automation.toml --force
```

Use `--tag v0.3.2` to select a particular published stable release; downgrades and
moved tags remain refused. Changes to project build inputs trigger revalidation
automatically; dependency-only changes need a forced check.
Each newly validated pipeline also increases cpptrace's RPM `Release`, allowing
safe retries/rebuilds without reusing a previously failed COPR NVR.
Generated RPM requirements retain Fedora's automatic Qt private-API ABI guards.

## Development

Run the offline unit suite and syntax checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q qscopr tests
python3 -m py_compile install
```

Run the real repository/container gate separately with a fresh output directory.
Do not treat mocked success tests as evidence that Quickshell compiled.
SMTP integration tests use a local capture-only TLS server and ephemeral
certificates, never an external provider or real recipient. They use the
`openssl` executable to generate test certificates and are skipped if it is not
installed; the production SMTP sender itself needs only Python's standard library.
Installer tests mock service-management commands and write only to temporary
directories. Additional dry-run unit/calendar verification tests use
`systemd-analyze` when available; they never activate a real timer.
