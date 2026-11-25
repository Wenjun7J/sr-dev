# One image and one persistent container serve all supported versions.
# Mount the shared $SR_TPDIR cache at /tp, then bind the selected digest's
# thirdparty installation to /var/local/thirdparty for each build.
# Bundle GCC 11/12/13/14 and select the compiler through STARROCKS_GCC_HOME.
# This keeps the scheduler and iceccd running while different versions build concurrently.
# Use the toolchains base rather than dev-env: dev-env adds prebuilt thirdparty,
# which we mount per version instead. The smaller base retains upstream CMake/JDK/Maven.
# sr image passes the base and apt mirror from srdev.conf as build arguments;
# the values here are fallbacks.
ARG BASE=starrocks/toolchains-ubuntu:main-latest
FROM ${BASE}
ARG APT_MIRROR=

# Use APT_MIRROR when the default Ubuntu repositories are unreachable.
# Handle both sources.list on 22.04 and deb822 *.sources on 24.04.
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|http://\(archive\|security\).ubuntu.com|http://$APT_MIRROR|g" \
        /etc/apt/sources.list /etc/apt/sources.list.d/*.sources 2>/dev/null; true; fi; \
    apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      ninja-build ccache icecc libiberty-dev binutils-dev procps bc util-linux sshpass iproute2 \
      mariadb-client \
      mold time \
      gcc-11 g++-11 gcc-12 g++-12 gcc-13 g++-13 \
 && rm -rf /var/lib/apt/lists/*

# Older branches link only -lbfd, but Ubuntu 24.04 splits sframe into a separate
# library. Merge its objects into libbfd.a to resolve sframe_* symbols; newer branches remain compatible.
RUN cd /tmp && mkdir -p mbfd && cd mbfd \
 && ar x /usr/lib/x86_64-linux-gnu/libbfd.a \
 && ar x /usr/lib/x86_64-linux-gnu/libsframe.a \
 && ar rcs /usr/lib/x86_64-linux-gnu/libbfd.a *.o \
 && cd / && rm -rf /tmp/mbfd

# env.sh uses $STARROCKS_GCC_HOME/bin/gcc; provide a separate bin directory for each version.
RUN for v in 11 12 13; do mkdir -p /opt/gcc$v/bin \
      && ln -sf /usr/bin/gcc-$v /opt/gcc$v/bin/gcc \
      && ln -sf /usr/bin/g++-$v /opt/gcc$v/bin/g++ \
      && ln -sf /usr/bin/gcc-ar-$v /opt/gcc$v/bin/gcc-ar 2>/dev/null; \
    done; true

COPY files/ccache.conf /etc/ccache.conf
COPY files/cxxlaunch   /usr/local/bin/cxxlaunch
COPY files/ccache-pch  /usr/local/bin/ccache-pch
COPY files/oss-ccache-proxy.py /usr/local/bin/oss-ccache-proxy.py
COPY files/sr-build    /usr/local/bin/sr-build
COPY files/sr-farm     /usr/local/bin/sr-farm
COPY files/sr-deploy   /usr/local/bin/sr-deploy
COPY files/sr-deploy-lock.py /usr/local/bin/sr-deploy-lock.py
COPY files/cmake-linkpool /usr/local/bin/cmake-linkpool
COPY files/dynlib-compat.cmake /usr/local/share/sr/dynlib-compat.cmake
COPY files/lldb-dap-env /usr/local/bin/lldb-dap-env
COPY files/linkgate    /usr/local/bin/linkgate
COPY files/tspapi.py   /usr/local/bin/tspapi.py
COPY files/node-setup.sh /usr/local/bin/node-setup.sh

# Build one icecc toolchain archive per GCC version. Workers must receive the
# same compiler as the client, such as GCC 11 for 3.5 and GCC 14 for main.
# Fail the image build if any archive is missing; substituting another compiler can silently miscompile code.
RUN set -e; TS=$(ls -d /opt/gcc-toolset-* 2>/dev/null); [ -n "$TS" ] || { echo "base image has no /opt/gcc-toolset-*"; exit 1; }; \
    mkdir -p /opt/icecc; \
    for g in /opt/gcc11 /opt/gcc12 /opt/gcc13 $TS; do \
      n=$(basename $g); [ -x "$g/bin/gcc" ] || { echo "toolchain $n: no gcc at $g/bin/gcc"; exit 1; }; \
      mkdir -p /opt/icecc/$n && cd /opt/icecc/$n \
      && icecc-create-env --gcc "$g/bin/gcc" "$g/bin/g++" >/dev/null 2>&1 \
      && [ "$(ls *.tar.gz 2>/dev/null | wc -l)" = 1 ] || { echo "toolchain $n FAILED"; exit 1; }; \
      echo "toolchain $n: $(ls *.tar.gz)"; \
    done

# Each build bind-mounts /tp/<digest>/installed here in a private mount namespace.
RUN mkdir -p /var/local/thirdparty/installed
# Use LLVM 20 for clangd/LLDB; clangd 18 can crash during generic-lambda constraint checks.
# libclang-rt-20-dev supplies sanitizer/*.h for ASAN configuration.
# These tool runtimes are independent of thirdparty's LLVM; signed-by scopes the key to the LLVM repository.
RUN set -e; . /etc/os-release; \
    install -d -m 755 /usr/share/keyrings; \
    curl -fsSL --retry 3 --connect-timeout 10 --max-time 60 \
      https://apt.llvm.org/llvm-snapshot.gpg.key -o /usr/share/keyrings/apt.llvm.org.asc; \
    printf 'deb [signed-by=/usr/share/keyrings/apt.llvm.org.asc] https://apt.llvm.org/%s/ llvm-toolchain-%s-20 main\n' \
      "$VERSION_CODENAME" "$VERSION_CODENAME" > /etc/apt/sources.list.d/llvm20.list; \
    apt-get update -qq -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 lldb-20 clangd-20 libclang-rt-20-dev; \
    rm -rf /var/lib/apt/lists/*; \
    ln -sf /usr/bin/lldb-20 /usr/local/bin/lldb; \
    ln -sf /usr/bin/lldb-dap-20 /usr/local/bin/lldb-dap; \
    ln -sf /usr/bin/clangd-20 /usr/local/bin/clangd; \
    lldb --version; lldb-dap --version; clangd --version

RUN mkdir -p /opt/noninja && printf '#!/bin/sh\nexit 1\n' > /opt/noninja/ninja && chmod +x /opt/noninja/ninja
RUN git config --global --add safe.directory '*'
ARG SR_IMAGE_ID=unversioned
LABEL org.srdev.image-id=$SR_IMAGE_ID org.srdev.tp-lock=1
RUN mkdir -p /usr/local/share/sr && printf '%s\n' "$SR_IMAGE_ID" > /usr/local/share/sr/image-id
RUN chmod +x /usr/local/bin/cxxlaunch /usr/local/bin/lldb-dap-env /usr/local/bin/ccache-pch /usr/local/bin/oss-ccache-proxy.py /usr/local/bin/sr-build /usr/local/bin/sr-farm /usr/local/bin/sr-deploy /usr/local/bin/cmake-linkpool /usr/local/bin/linkgate \
             /usr/local/bin/tspapi.py /usr/local/bin/node-setup.sh
