# 单镜像：一个容器编所有版本。
#   版本差异靠两样东西解决，都不需要换容器：
#     - thirdparty: 公共目录 $SR_TPDIR/<digest>/ 整体挂成 /tp，编译时把所选版本 bind 到规范路径 /var/local/thirdparty
#     - 编译器:     镜像里备齐 gcc 11/12/13/14，用哪个由 STARROCKS_GCC_HOME 指
#   这样 scheduler 和 iceccd 常驻不被杀，多版本还能并发编。
# base 用 toolchains 而不是 dev-env：官方 dev-env 自己就是 FROM toolchains 再加编好的 thirdparty，
# 我们按版本挂 thirdparty，所以只要 toolchains 那层（2.56G vs 14G），cmake/JDK/maven 版本由上游维护。
# base 镜像与 apt 源由 sr image 从 srdev.conf 以 --build-arg 传入，这里只是兜底默认值
ARG BASE=starrocks/toolchains-ubuntu:main-latest
FROM ${BASE}
ARG APT_MIRROR=

# 内网机器常连不上 archive.ubuntu.com（apt-get update 会静默挂很久）：APT_MIRROR 非空时换源。
# 22.04 用 sources.list，24.04 用 deb822 的 *.sources，两种都处理。
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

# 老分支（如 3.x）的 CMake 只链 -lbfd，而 24.04 的 binutils 把 sframe 拆了出去，
# 会缺 sframe_* 符号。把 libsframe 的 object 合进 libbfd.a，对新分支无害。
RUN cd /tmp && mkdir -p mbfd && cd mbfd \
 && ar x /usr/lib/x86_64-linux-gnu/libbfd.a \
 && ar x /usr/lib/x86_64-linux-gnu/libsframe.a \
 && ar rcs /usr/lib/x86_64-linux-gnu/libbfd.a *.o \
 && cd / && rm -rf /tmp/mbfd

# env.sh 用 $STARROCKS_GCC_HOME/bin/gcc，给每个版本造一个只含自己的 bin 目录
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

# 每个 gcc 版本各生成一份 icecc 工具链包：客户端把自己的编译器传给 worker，
# 所以编 3.5 时传 gcc11 的包、编 main 时传 gcc14 的包，不能混
# 任何一份生成失败即镜像构建失败——运行时绝不拿别的 gcc 的包顶替（worker 用错编译器且不报错）
RUN set -e; TS=$(ls -d /opt/gcc-toolset-* 2>/dev/null); [ -n "$TS" ] || { echo "base image has no /opt/gcc-toolset-*"; exit 1; }; \
    mkdir -p /opt/icecc; \
    for g in /opt/gcc11 /opt/gcc12 /opt/gcc13 $TS; do \
      n=$(basename $g); [ -x "$g/bin/gcc" ] || { echo "toolchain $n: no gcc at $g/bin/gcc"; exit 1; }; \
      mkdir -p /opt/icecc/$n && cd /opt/icecc/$n \
      && icecc-create-env --gcc "$g/bin/gcc" "$g/bin/g++" >/dev/null 2>&1 \
      && [ "$(ls *.tar.gz 2>/dev/null | wc -l)" = 1 ] || { echo "toolchain $n FAILED"; exit 1; }; \
      echo "toolchain $n: $(ls *.tar.gz)"; \
    done

# 挂载点：每次编译在私有 mount ns 里把 /tp/<digest>/installed bind 到这里（thirdparty 的规范路径）
RUN mkdir -p /var/local/thirdparty/installed
# clangd / LLDB 使用 LLVM 20；clangd 18 在泛型 lambda 的约束检查中会崩溃。
# libclang-rt-20-dev 提供 ASAN 配置需要的 sanitizer/*.h。
# 工具运行库独立于 SR thirdparty 中的 LLVM；signed-by 将密钥限定于官方 LLVM 源。
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
