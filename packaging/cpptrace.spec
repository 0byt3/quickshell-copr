# Validated pipeline prerequisite; not Quickshell FetchContent vendoring.
# Source SHA-256: 77d689fd7956ff80351a079d83e86a03865dbbe2433b4559cc6cea50bed77390
%global commit 3db8da80111171c219ab5839905771386bee06b3
Name: cpptrace
Version: 1.0.4
Release: 3%{?dist}
Summary: C++ stack traces with signal-safe libunwind support
License: MIT
URL: https://github.com/jeremy-rifkin/cpptrace
Source0: %{url}/archive/%{commit}/cpptrace-%{commit}.tar.gz

BuildRequires: cmake >= 3.15
BuildRequires: cmake(GTest)
BuildRequires: cmake-rpm-macros
BuildRequires: gcc-c++
BuildRequires: gmock-devel
BuildRequires: ninja-build
BuildRequires: pkgconf-pkg-config
BuildRequires: pkgconfig(libdwarf) >= 2.1
BuildRequires: pkgconfig(libunwind)
BuildRequires: pkgconfig(libzstd)
BuildRequires: pkgconfig(zlib)

%description
A C++ stacktrace library built with system libdwarf and signal-safe libunwind.

%package devel
Summary: Development files for cpptrace
Requires: cpptrace%{?_isa} = %{version}-%{release}
Requires: pkgconfig(libzstd)

%description devel
Headers and CMake configuration for cpptrace.

%prep
%autosetup -n cpptrace-3db8da80111171c219ab5839905771386bee06b3

%build
%cmake -GNinja \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DCPPTRACE_BUILD_SHARED=ON \
    -DCPPTRACE_UNWIND_WITH_LIBUNWIND=ON \
    -DCPPTRACE_GET_SYMBOLS_WITH_LIBDWARF=ON \
    -DCPPTRACE_USE_EXTERNAL_LIBDWARF=ON \
    -DCPPTRACE_FIND_LIBDWARF_WITH_PKGCONFIG=ON \
    -DCPPTRACE_USE_EXTERNAL_ZSTD=ON \
    -DCPPTRACE_BUILD_TESTING=ON \
    -DCPPTRACE_USE_EXTERNAL_GTEST=ON \
    -DCPPTRACE_BUILD_TOOLS=OFF \
    -DCPPTRACE_BUILD_BENCHMARKING=OFF \
    -DCPPTRACE_DISABLE_CXX_20_MODULES=ON \
    -DFETCHCONTENT_FULLY_DISCONNECTED=ON \
    -DFETCHCONTENT_UPDATES_DISCONNECTED=ON
%cmake_build

%install
%cmake_install

%check
%ctest --output-on-failure --no-tests=error
cat > pipeline-smoke.cpp <<'CPPTRACE_SMOKE'
#include <cpptrace/cpptrace.hpp>
#include <iostream>
int main() {
    if (!cpptrace::can_signal_safe_unwind()) {
        return 1;
    }
    cpptrace::frame_ptr frames[32];
    if (cpptrace::safe_generate_raw_trace(frames, 32) == 0) {
        return 2;
    }
    auto trace = cpptrace::generate_trace();
    if (trace.frames.empty()) {
        return 3;
    }
    std::cout << "cpptrace signal-safe unwinding and symbol resolution passed\n";
    return 0;
}
CPPTRACE_SMOKE
g++ -std=c++20 -g -fno-omit-frame-pointer \
    -Iinclude -I%{_vpath_builddir}/include pipeline-smoke.cpp \
    -L%{_vpath_builddir} -Wl,-rpath,"$PWD/%{_vpath_builddir}" \
    -lcpptrace -o pipeline-smoke
./pipeline-smoke

%files
%license LICENSE
%{_libdir}/libcpptrace.so.1*

%files devel
%license LICENSE
%{_includedir}/cpptrace/
%{_includedir}/ctrace/
%{_libdir}/libcpptrace.so
%{_libdir}/cmake/cpptrace/

%changelog
