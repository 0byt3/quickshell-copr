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

### Server deployment walkthrough

Run the following commands **on the server**, using an administrator account with
sudo. The package/account example is for a conventional **Fedora Server x86_64**
installation using Docker/Moby; adapt package names and account tools for another
distribution. The host need not be Fedora 44: the build container always uses the
configured Fedora 44 image. The commit must have been pushed before cloning or
pulling; development-machine commits are not automatically deployed.

For an existing deployment, use [Updating an existing deployment](#updating-an-existing-deployment)
instead of recreating accounts, cloning over the checkout, or overwriting settings.

#### 1. Prepare a fresh server

Install packages from Fedora's repositories, not pip or downloaded installers:

```bash
sudo dnf install git python3 python3-copr moby-engine docker-cli \
    systemd util-linux shadow-utils ca-certificates openssl
sudo useradd --system --user-group --create-home \
    --home-dir /var/lib/quickshell-copr \
    --shell /usr/sbin/nologin quickshell-copr
sudo systemctl enable --now docker.service
sudo usermod --append --groups docker quickshell-copr
```

Docker group membership grants effectively root-level host access; this must be
a trusted service account. New service processes pick up its group membership
without an interactive login. See Fedora's
[Docker package documentation](https://docs.fedoraproject.org/en-US/quick-docs/installing-docker/)
for distribution-specific details. Do not mix Fedora's Moby packages with a
different Docker package source or the `podman-docker` compatibility wrapper.
If choosing rootless Podman instead, skip the Docker-specific commands, configure
the account's runtime/user manager and system-service environment for that
distribution, and verify container execution in that environment before enabling
the timer. The installer does not provision a rootless runtime.

Clone the repository into a new service-owned directory:

```bash
sudo install -d -o quickshell-copr -g quickshell-copr -m 0750 /srv/quickshell-copr
sudo -u quickshell-copr git clone --branch main \
    https://github.com/0byt3/quickshell-copr.git /srv/quickshell-copr
sudo -u quickshell-copr mkdir -p /srv/quickshell-copr/artifacts
cd /srv/quickshell-copr
```

The HTTPS URL avoids requiring the development machine's SSH host alias or
private key on the server. If repository access requires authentication, use an
approved credential manager or deployment credential, never a token in the URL.
Cloning as the service account gives it write access to the checkout and
`packaging/`; do not clone as root and then run the service against root-owned
files.

Verify Python, the COPR client, and actual container access as the service account:

```bash
sudo -u quickshell-copr /usr/bin/python3 -c \
    'import sys, copr.v3; assert sys.version_info >= (3, 12), "Python 3.12+ required"'
sudo -u quickshell-copr env -i \
    HOME=/var/lib/quickshell-copr PATH=/usr/bin:/bin /usr/bin/docker info
sudo -u quickshell-copr env -i \
    HOME=/var/lib/quickshell-copr PATH=/usr/bin:/bin /usr/bin/docker run \
    --rm --platform=linux/amd64 --network=none --cap-drop=ALL \
    --security-opt=no-new-privileges --user=1000:1000 \
    registry.fedoraproject.org/fedora:44 /usr/bin/uname -m
```

The last command downloads the official image if necessary and should print
`x86_64`; it does not build or publish packages. Fix daemon, permissions, network,
or architecture/emulation failures before proceeding. These checks deliberately
do not rely on an interactive shell's Docker context or runtime variables.

#### 2. Configure a fresh installation

Review `config.toml` and the COPR project as described under
[Server prerequisites and configuration](#server-prerequisites-and-configuration).
Enable `fedora-44-x86_64` and COPR's **Auto Create Repo** option so successful
builds produce public repository metadata; the publisher does not configure or
independently verify public repository availability.

The following setup commands are **first-installation only**: `install` replaces
existing destination files, so do not rerun them against populated settings or
credentials.

```bash
sudo install -d -o root -g quickshell-copr -m 0750 /etc/quickshell-copr
sudo install -o root -g quickshell-copr -m 0640 \
    automation.example.toml /etc/quickshell-copr/automation.toml
sudoedit /etc/quickshell-copr/automation.toml
sudo install -o root -g root -m 0600 /dev/null /etc/quickshell-copr/copr-login
sudo install -o root -g root -m 0600 /dev/null /etc/quickshell-copr/copr-token
sudoedit /etc/quickshell-copr/copr-login /etc/quickshell-copr/copr-token
sudo chown quickshell-copr:quickshell-copr \
    /etc/quickshell-copr/copr-login /etc/quickshell-copr/copr-token
sudo chmod 0400 /etc/quickshell-copr/copr-login /etc/quickshell-copr/copr-token
```

Populate each credential file through the editor with just its corresponding
COPR API value, on one nonempty line. Never pass secret values as command
arguments or place them in the checkout. Replace the example email addresses and
SMTP hostname in the settings.

For the example's authenticated SMTP configuration, also create its password file:

```bash
sudo install -o root -g root -m 0600 /dev/null /etc/quickshell-copr/smtp-password
sudoedit /etc/quickshell-copr/smtp-password
sudo chown quickshell-copr:quickshell-copr /etc/quickshell-copr/smtp-password
sudo chmod 0400 /etc/quickshell-copr/smtp-password
```

For a relay without authentication, omit that file and remove **both**
`smtp.username` and `smtp.password_file` from the settings instead. TLS remains
mandatory. If using different credential paths or an account name, adjust all
commands and settings consistently. Keep credentials readable only by their owner
and ensure the account can traverse their parent directories.

#### 3. Preview and install with scheduling disabled

From `/srv/quickshell-copr`, verify the generated units and install them without
starting the schedule:

```bash
./install --dry-run --python /usr/bin/python3
sudo ./install --python /usr/bin/python3 --no-enable
```

The explicit interpreter avoids accidentally using a different administrator
interpreter. The dry run verifies unit/calendar syntax but skips account and
configuration checks. The actual installation performs service-account
prerequisite checks; neither command builds, emails, or publishes. The timer
remains **disabled and stopped**, including when updating an existing installation.
See [systemd timer](#systemd-timer) below for custom paths, account, and schedule.

#### 4. Verify alerts and the full service

From the checkout, send an explicit test email as the service account:

```bash
sudo -u quickshell-copr /usr/bin/python3 -m qscopr.automate \
    --config /etc/quickshell-copr/automation.toml --test-email
```

Confirm it arrives before proceeding. Exit `0` confirms SMTP server acceptance,
not final inbox delivery. This is the first walkthrough step that sends email.

Start a complete validation/publication run through the installed service:

```bash
sudo systemctl start --no-block quickshell-copr.service
sudo journalctl --follow -u quickshell-copr.service -u quickshell-copr-alert.service
```

**This run can publish to COPR.** Following the journal does not wait for success
automatically; Ctrl+C stops viewing logs, not the service. Once the run finishes:

```bash
systemctl show --property=ActiveState --property=Result --property=ExecMainStatus \
    quickshell-copr.service
```

Expect `ActiveState=inactive`, `Result=success`, and `ExecMainStatus=0`; review the
journal and `artifacts/automation/latest.json` for the completed pipeline outcome.
`activating` is normal throughout a running `Type=oneshot` build, not proof of
successful completion. Resolve failures and retry before enabling the timer.

#### 5. Enable scheduling

Only after the manual service run succeeds:

```bash
sudo systemctl enable --now quickshell-copr.timer
systemctl is-enabled quickshell-copr.timer
systemctl list-timers --all quickshell-copr.timer
```

The default is **06:23 UTC daily**, with up to ten minutes randomized delay.
**Persistent catch-up can start a missed run immediately upon enabling.**
Use only one scheduling mechanism: remove any cron entry for this checkout before
enabling the timer.

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

After provisioning and verification, the shorthand below installs and enables
the timer on the **server** in one step. Prefer the walkthrough's `--no-enable`
sequence for a first deployment:

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

### Updating an existing deployment

Disable the timer first so it cannot start during deployment or a reboot:

```bash
sudo systemctl disable --now quickshell-copr.timer
systemctl show --property=Id --property=ActiveState \
    quickshell-copr.service quickshell-copr-alert.service
```

Wait until both services are `inactive` or `failed`. In particular, do not update
the checkout while a service is `activating`, even if `systemctl is-active` does
not report it as active. Also stop any cron or manual source of pipeline runs.
The installer guards unit replacement, not an earlier `git pull`.

Back up the checkout's `packaging/`, `config.toml`, and `artifacts/automation/`
(including `published.json`, `latest.json`, and `.lock`) while idle, along with a
separate, access-protected backup of `/etc/quickshell-copr/`. A server migration
must preserve this state and leave only one publishing server scheduled.

Inspect local changes before pulling:

```bash
cd /srv/quickshell-copr
sudo -u quickshell-copr git --no-pager status --short
sudo -u quickshell-copr git pull --ff-only origin main && \
    sudo ./install --python /usr/bin/python3 --no-enable
```

Promotion legitimately changes maintained files. If Git reports a conflict or
refuses the pull, stop and reconcile the backed-up local state with the new code;
do not use `git reset --hard`, `git clean`, or blindly replace packaging/receipts.
The same applies to locally edited `config.toml`. Do not proceed to installation
after a failed pull. Rerun the email and full-service checks in the walkthrough,
then re-enable the timer only after they pass.

To pause future scheduling, use
`sudo systemctl disable --now quickshell-copr.timer`. This does **not** stop an
already running pipeline. To remove scheduling entirely, first disable the
timer and wait for both services to finish, then remove only the three installed
unit files (`quickshell-copr.service`, `quickshell-copr-alert.service`, and
`quickshell-copr.timer`) from `/etc/systemd/system/` and run
`sudo systemctl daemon-reload`. Preserve configuration, credentials, artifacts,
and backups unless deliberately retiring them.

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
