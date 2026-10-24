"""Generate the exact spec used both for validation and COPR."""

import re

from .source import GateError
from .cpptrace import FLAGS, SMOKE_SOURCE


def render_spec(release, inventory, config, rpm_release=1):
    distributor = config["copr"]["distributor"]
    if not re.fullmatch(r"[A-Za-z0-9 /()_.-]+", distributor):
        raise GateError("discovery-blocked", "Distributor contains unsafe spec characters")
    root = release["source_root"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", root):
        raise GateError("discovery-blocked", "Unsafe source root")
    build_requires = "\n".join(
        f"BuildRequires: {capability}"
        for capability in sorted({item["capability"] for item in inventory["requirements"]})
    )
    return f"""# Generated only after the Fedora release gate validates this candidate.
# Upstream source SHA-256: {release["sha256"]}
%global commit {release["commit"]}

Name: quickshell
Version: {release["version"]}
Release: {rpm_release}%{{?dist}}
Summary: Flexible QtQuick based desktop shell toolkit
License: LGPL-3.0-only AND GPL-3.0-only
URL: https://quickshell.org/
Source0: https://github.com/{release["repository"]}/archive/%{{commit}}/quickshell-%{{commit}}.tar.gz

{build_requires}
Recommends: qt6-qtsvg
Recommends: qt6-qtimageformats
# RPM's automatic Qt_*_PRIVATE_API requirements guard private Qt ABI changes.

%description
Quickshell is a toolkit for building status bars, widgets, lock screens,
and other desktop components using QtQuick, for Wayland and X11.

%prep
%autosetup -n {root}

%build
%cmake -GNinja \\
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \\
    -DBUILD_SHARED_LIBS=OFF \\
    -DVENDOR_CPPTRACE=OFF \\
    -DFETCHCONTENT_FULLY_DISCONNECTED=ON \\
    -DFETCHCONTENT_UPDATES_DISCONNECTED=ON \\
    -DDISTRIBUTOR="{distributor}" \\
    -DGIT_REVISION=%{{commit}} \\
    -DINSTALL_QML_PREFIX=%{{_lib}}/qt6/qml
%cmake_build

%install
%cmake_install

%check
test -x %{{buildroot}}%{{_bindir}}/quickshell
QT_QPA_PLATFORM=offscreen %{{buildroot}}%{{_bindir}}/quickshell --version

%files
%license LICENSE LICENSE-GPL
%doc README.md BUILD.md CONTRIBUTING.md
%{{_bindir}}/qs
%{{_bindir}}/quickshell
%{{_datadir}}/applications/org.quickshell.desktop
%{{_datadir}}/icons/hicolor/scalable/apps/org.quickshell.svg
%{{_libdir}}/qt6/qml/Quickshell/

%changelog
"""


def render_cpptrace_spec(release, inventory, rpm_release):
    requirements = "\n".join(
        f"BuildRequires: {capability}"
        for capability in sorted({item["capability"] for item in inventory["requirements"]})
    )
    flags = " \\\n    ".join(FLAGS)
    return f"""# Validated pipeline prerequisite; not Quickshell FetchContent vendoring.
# Source SHA-256: {release["sha256"]}
%global commit {release["commit"]}
Name: cpptrace
Version: {release["version"]}
Release: {rpm_release}%{{?dist}}
Summary: C++ stack traces with signal-safe libunwind support
License: MIT
URL: https://github.com/jeremy-rifkin/cpptrace
Source0: %{{url}}/archive/%{{commit}}/cpptrace-%{{commit}}.tar.gz

{requirements}

%description
A C++ stacktrace library built with system libdwarf and signal-safe libunwind.

%package devel
Summary: Development files for cpptrace
Requires: cpptrace%{{?_isa}} = %{{version}}-%{{release}}
Requires: pkgconfig(libzstd)

%description devel
Headers and CMake configuration for cpptrace.

%prep
%autosetup -n {release["source_root"]}

%build
%cmake -GNinja \\
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \\
    {flags}
%cmake_build

%install
%cmake_install

%check
%ctest --output-on-failure --no-tests=error
cat > pipeline-smoke.cpp <<'CPPTRACE_SMOKE'
{SMOKE_SOURCE}CPPTRACE_SMOKE
g++ -std=c++20 -g -fno-omit-frame-pointer \\
    -Iinclude -I%{{_vpath_builddir}}/include pipeline-smoke.cpp \\
    -L%{{_vpath_builddir}} -Wl,-rpath,"$PWD/%{{_vpath_builddir}}" \\
    -lcpptrace -o pipeline-smoke
./pipeline-smoke

%files
%license LICENSE
%{{_libdir}}/libcpptrace.so.1*

%files devel
%license LICENSE
%{{_includedir}}/cpptrace/
%{{_includedir}}/ctrace/
%{{_libdir}}/libcpptrace.so
%{{_libdir}}/cmake/cpptrace/

%changelog
"""
