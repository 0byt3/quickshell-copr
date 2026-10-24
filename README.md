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

Only validated state is tracked. Blocked releases are tried again on the next
scheduled run, allowing Fedora package updates to unblock them.

## GitHub Actions setup

1. Host this project in a GitHub repository and enable Actions. Initializing Git,
   committing, and creating a remote repository are separate setup actions.
2. Review `config.toml`. Initially only Fedora 44 x86_64 and its official image are
   supported. `jobs` limits parallel compilation (default 2).
3. Permit GitHub Actions to create pull requests in the repository's Actions
   settings. The proposal job uses the repository's `GITHUB_TOKEN`; build jobs have
   read-only repository access and checkout credentials are not persisted.
4. Run **Fedora 44 release gate** manually once to verify the setup.

Workflows:

- `tests.yml`: unit tests and Python syntax checks on pushes and pull requests.
- `releases.yml`: daily polling at **06:23 UTC**, manual tag/force inputs, optional
  `quickshell-release` repository dispatch, and relevant pull-request checks.
  A release in another repository does not trigger this repository directly.
- `publish.yml`: opt-in COPR publishing after a maintained packaging update lands
  on the default branch, or by manual dispatch on that branch.

The release workflow proposes changes on `automation/quickshell-release`.
Automatic PRs created using `GITHUB_TOKEN` do not normally trigger other workflows;
review the originating build's report or use a narrowly scoped GitHub App token if
your repository requires independent PR-triggered CI. No App token is configured
by this project. Merge the validated update through your normal review policy.

Dependency/discovery blocks are reported without advancing state or publishing.
Scheduled blocks do not mark the workflow failed; build/infrastructure failures
do. Pull-request gate blocks fail the PR check. Artifacts are retained for 14 days.

## COPR setup and opt-in publication

Publication is disabled initially. To enable it:

1. Create/configure your own COPR project with `fedora-44-x86_64` enabled. Prefer a
   dedicated project. Do not add third-party repositories or injected buildroot
   packages. This tool does not edit COPR project configuration.
2. Set `copr.project = "YOUR_OWNER/YOUR_PROJECT"` and an identifying
   `copr.distributor` in `config.toml` **before validating**.
3. Create a GitHub environment named `copr`, restrict it to the default branch,
   and configure required reviewers if desired.
4. Store `COPR_LOGIN` and `COPR_TOKEN` as environment secrets. Obtain them from your
   COPR API page. Do not put credentials in this repository.
5. Set the repository variable `ENABLE_COPR_PUBLISH` to `true`.
6. Obtain and merge a successful validated packaging update.

Publishing first rebuilds the **tracked release**, with no COPR secrets present.
It verifies both maintained specs, source identities, build-input hash, artifact
checksums, and installed dependency fingerprint. Changed inputs/dependencies
require a newly validated update rather than publishing stale approval.

The separate protected publisher job installs `python3-copr` from Fedora's
repositories and downloads only the fresh verified SRPMs/manifest from that same
trusted workflow run. It uploads cpptrace first and waits for overall and Fedora 44
chroot success before submitting Quickshell, recording the prerequisite build ID
in Quickshell's result. It does not use COPR's batch dependency option: COPR cannot
create a batch around an already finished build, and batch completion alone does
not guarantee prerequisite success.
Build networking is disabled for both. No PR-supplied artifact is consumed.
COPR imports the submitted SRPM's spec; there is no direct edit of COPR's generated
dist-git.

The publisher checks paginated build history to avoid resubmitting the same
name/version/release/chroot, and serializes its workflow runs. Already submitted
builds are monitored instead of uploaded again. Failed/canceled/skipped versions
require a validated higher RPM Release. Avoid independent publishers racing for
the same NVR: COPR has no transaction-wide exactly-once upload lock here.

Results and COPR build links are in `cpptrace-copr-result.json` and
`copr-result.json`. COPR can still fail
despite the container gate (for example, if Fedora repositories change in between).
Local validation is not a promise of publication or of a successful COPR build.
Live COPR publication is not part of the unit tests.

## Qt updates and rebuilds

Quickshell uses private Qt APIs and upstream requires rebuilding against Qt
releases. New-Quickshell-release polling alone does not detect every Qt ABI change.

Force a same-version rebuild after Fedora/Qt updates:

```bash
python3 -m qscopr check --force --output artifacts/qt-rebuild --promote
```

This increases RPM `Release` for an already validated version. Use the workflow's
`force` input for the same operation in CI. Changes to project build inputs also
trigger revalidation automatically; dependency-only changes need a forced check.
Each newly validated pipeline also increases cpptrace's RPM `Release`, allowing
safe retries/rebuilds without reusing a previously failed COPR NVR.
Generated RPM requirements retain Fedora's automatic Qt private-API ABI guards.

## Development

Run the offline unit suite and syntax checks:

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q qscopr tests
```

Run the real repository/container gate separately with a fresh output directory.
Do not treat mocked success tests as evidence that Quickshell compiled.
