# Generated only after the Fedora release gate validates this candidate.
# Upstream source SHA-256: 6806c64680b8058918f3c79d9af0603e99004601bc49bd1828029335673774c6
%global commit 4f508be500dea6e5732cc3d50382a0048b17e7b1

Name: quickshell
Version: 0.3.2
Release: 2%{?dist}
Summary: Flexible QtQuick based desktop shell toolkit
License: LGPL-3.0-only AND GPL-3.0-only
URL: https://quickshell.org/
Source0: https://github.com/quickshell-mirror/quickshell/archive/%{commit}/quickshell-%{commit}.tar.gz

BuildRequires: cmake
BuildRequires: cmake >= 3.20
BuildRequires: cmake(CLI11)
BuildRequires: cmake(Qt6Core)
BuildRequires: cmake(Qt6Core) >= 6.6
BuildRequires: cmake(Qt6DBus)
BuildRequires: cmake(Qt6Gui)
BuildRequires: cmake(Qt6Network)
BuildRequires: cmake(Qt6Qml)
BuildRequires: cmake(Qt6Quick)
BuildRequires: cmake(Qt6QuickControls2)
BuildRequires: cmake(Qt6QuickPrivate)
BuildRequires: cmake(Qt6ShaderTools)
BuildRequires: cmake(Qt6Test)
BuildRequires: cmake(Qt6WaylandClient)
BuildRequires: cmake(Qt6WaylandClient) >= 6.10
BuildRequires: cmake(Qt6WaylandClientPrivate)
BuildRequires: cmake(Qt6WaylandClientPrivate) >= 6.10
BuildRequires: cmake(Qt6Widgets)
BuildRequires: cmake(VulkanHeaders)
BuildRequires: cmake(cpptrace)
BuildRequires: cmake-rpm-macros
BuildRequires: gcc-c++
BuildRequires: ninja-build
BuildRequires: pam-devel
BuildRequires: pkgconf-pkg-config
BuildRequires: pkgconfig(egl)
BuildRequires: pkgconfig(gbm)
BuildRequires: pkgconfig(glib-2.0)
BuildRequires: pkgconfig(glib-2.0) >= 2.36
BuildRequires: pkgconfig(gobject-2.0)
BuildRequires: pkgconfig(jemalloc)
BuildRequires: pkgconfig(libdrm)
BuildRequires: pkgconfig(libpipewire-0.3)
BuildRequires: pkgconfig(polkit-agent-1)
BuildRequires: pkgconfig(polkit-gobject-1)
BuildRequires: pkgconfig(wayland-client)
BuildRequires: pkgconfig(wayland-protocols)
BuildRequires: pkgconfig(wayland-protocols) >= 1.41
BuildRequires: pkgconfig(xcb)
BuildRequires: qt6-qtbase-private-devel
BuildRequires: qt6-qtdeclarative-private-devel
BuildRequires: spirv-tools
BuildRequires: vulkan-headers
BuildRequires: wayland-devel
Recommends: qt6-qtsvg
Recommends: qt6-qtimageformats
# RPM's automatic Qt_*_PRIVATE_API requirements guard private Qt ABI changes.

%description
Quickshell is a toolkit for building status bars, widgets, lock screens,
and other desktop components using QtQuick, for Wayland and X11.

%prep
%autosetup -n quickshell-4f508be500dea6e5732cc3d50382a0048b17e7b1

%build
%cmake -GNinja \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DBUILD_SHARED_LIBS=OFF \
    -DVENDOR_CPPTRACE=OFF \
    -DFETCHCONTENT_FULLY_DISCONNECTED=ON \
    -DFETCHCONTENT_UPDATES_DISCONNECTED=ON \
    -DDISTRIBUTOR="Fedora COPR (byte0/quickshell)" \
    -DGIT_REVISION=%{commit} \
    -DINSTALL_QML_PREFIX=%{_lib}/qt6/qml
%cmake_build

%install
%cmake_install

%check
test -x %{buildroot}%{_bindir}/quickshell
QT_QPA_PLATFORM=offscreen %{buildroot}%{_bindir}/quickshell --version

%files
%license LICENSE LICENSE-GPL
%doc README.md BUILD.md CONTRIBUTING.md
%{_bindir}/qs
%{_bindir}/quickshell
%{_datadir}/applications/org.quickshell.desktop
%{_datadir}/icons/hicolor/scalable/apps/org.quickshell.svg
%{_libdir}/qt6/qml/Quickshell/

%changelog
