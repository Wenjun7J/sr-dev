# 首次安装与配置

## 先确认基本条件

需要 Linux x86_64 开发机、Bash、GNU coreutils（readlink/stat/md5sum）、flock、Python 3、
curl、Git 和可访问的 Docker daemon。当前 Dockerfile 有 x86_64 路径，不能把 macOS/ARM
本机视为等价执行环境；可从本机经 SSH 操作 Linux 主机。

容器使用 host 网络和 privileged 模式，并将用户源码路径挂入容器。这套工具面向同机开发者互相信任的开发环境。
不需要为了安装工具修改宿主机防火墙。已有容器、数据和配置应先核对再复用。

还需要用户有权访问的镜像仓库：

- `SR_BASE_IMAGE` 指向 **toolchains** 镜像，为工具镜像提供基础编译器/JDK/Maven。
- `registries.conf` 指向各版本的 **dev-env** 仓库，用于提取 thirdparty 并识别所需 GCC。
- 当前解析器走匿名 **HTTP v2 API**，使用 `tags/list` 与 `HEAD manifests`。
  仅做 Docker login 不能让解析器支持需要 token 的 registry；不能直接套用 Docker Hub 或 HTTPS-only 地址。
  Docker daemon 也必须能拉取该仓库，HTTP registry 可能需要管理员配置对应 insecure-registry。

## 最小安装路径

在目标主机的工具 checkout 中运行：

```bash
cd <srdev目录>
./install.sh
source ~/.config/srdev/env
```

安装器检查宿主依赖和 Docker，建立 `~/bin/sr`、`clangd`、`lldb`、`lldb-dap` 入口；
不存在的配置从根目录模板复制并设为 0600，已有配置保留。PATH 片段加入 `.profile`
以及当前 shell 对应的 `.bashrc` 或 `.zshrc`。遇到同名入口冲突，检查指向，不用强制软链接覆盖。

编辑 `<srdev目录>/srdev.conf` 和 `registries.conf`。下面只是匿名示意，必须替换占位值后再执行：

```bash
# srdev.conf：实际值只放被忽略的配置文件
SR_BASE_IMAGE=registry.example.invalid:5000/starrocks/toolchains-ubuntu:<已存在的tag>
SR_APT_MIRROR=
SR_TPDIR=/srv/srdev/thirdparty
CPUS=16
MEM=64g
```

```text
# registries.conf：命名空间  仓库（不带 http://，不带 tag）
ee registry.example.invalid:5000/enterprise/dev-env-ubuntu
os registry.example.invalid:5000/starrocks/dev-env-ubuntu
```

只配置确实存在的仓库。`ee` / `os` 是可自定义的解析标签，普通版本按表中顺序取第一个匹配；
要限定仓库用 `-v ee:main`、`-v os:3.5`。不要替用户切换企业版与开源版。
`SR_TP_REPO` 非空时会覆盖整张表，包括显式命名空间，排查选错镜像时优先检查它。

安装后先检查：

```bash
sr help
sr versions
cd <StarRocks源码树>
sr -v <已核对的版本> build --be --dist off
```

`sr versions` 访问 registry 并写入 tag 缓存。第一次构建可能自动构建工具镜像并拉取数 GB 依赖，
确保依赖目录、源码盘和 Docker 数据目录有空间。首次版本必须由用户目标和实际镜像共同确定，
不凭分支名称自动猜。源码默认需要在 HOME 挂载范围内；若不在，核对 `WORK` 以及容器实际 Mounts。

完成后用构建退出码、`output/be` 和构建日志确认结果，再按需执行：

```bash
sr build --all --dist off
sr compdb
sr ide
```

这条路径不要求 TSP、OSS 或编译农场。`compdb` 仍依赖版本配置、工具镜像与目标脚本支持。

## 配置地图

| 目的 | 配置 | 生效方式和注意事项 |
|---|---|---|
| 工具镜像 | `SR_BASE_IMAGE`、`SR_APT_MIRROR`，可选 `IMG` | 影响镜像标识。`sr image` 后，空闲时切换容器；换依赖版本无需换容器 |
| 依赖仓库 | `SR_REGISTRIES`、`SR_TP_REPO` | 默认读工具目录的 registries.conf；后者覆盖解析表 |
| 依赖缓存 | `SR_TPDIR` | 全机共享完整 digest 目录；不可写时默认退回个人 thirdparty。改变路径需核对现有挂载 |
| 容器限额 | `CPUS`、`MEM` | 默认 16/64g。`sr up` 可热更新；忙时缩容会被拒绝或提示 |
| 本机编译 | `SR_LOCAL_JOBS` | 默认 CPU 配额；影响常驻 iceccd 槽位，改变后空闲时重建个人容器 |
| FE 单测 | `SR_FE_UT_JOBS` | 默认跟本机份额；控制 Maven `-T`，不等于 JVM 数 |
| 链接并发 | `SR_LINK_JOBS`、`SR_LINK_MEM_GB` | 后者默认 15 GiB 经验估值；不能保证避免 OOM |
| BE UT | `SR_DYNLIB_PCH` | 默认 1；命令行 `--[no-]dynlib-pch` 优先，切换会重编 |
| 部署 | `SR_FE_HEAP`、`SR_BE_HEAP`、`SR_PORT_BASE`、`SR_FE_DEBUG_PORT` | 默认 JVM 堆各 2g。已有部署优先保留端口；不要为避让而迁移已有数据 |
| IDE | `SR_CC_LINK` | 默认生成根目录 compdb 软链接；真实文件不会被覆盖 |

默认站点配置在工具目录；`SR_CONF` 可改配置路径。宿主加载配置后，每次进入容器工作流前
原子同步到 `~/farm/srdev.conf`（0600）；容器优先读 `/farm/srdev.conf`，旧容器兼容 `/etc/srdev.conf`。
删除源配置不会自动清空已同步内容，应明确修改已有文件。普通 Shell 配置赋值可覆盖环境同名值，
容器也会再次读同步配置；临时环境赋值不保证覆盖站点文件。

## 按需开启云功能

**编译农场**：已有 TSP 身份配置 `~/.tsp/config.json`（`server_url`、`token`），
并设置 `SR_NODE_USER`、首次铺密钥所需的 `SR_NODE_PASSWORD`、`WORKERS`、`ITYPE`。
`WORKERS` 指申请的总台数，平台自带的一台 FE 也用作 worker，不额外再加一台。
可选 `SR_NODE_JOBS` 控制节点槽数；改变 worker 槽数目前需要释放后重新申请，不能只改配置后假定 resume 已更新。
具体 TSP 登录方式以所在环境已安装 CLI 为准；凭据留在个人配置中。

在用户要使用云农场的任务中执行 `sr farm init`，检查实际节点连接和预热。
若提示尚未预热，先让一次构建启动本机 iceccd，再 `sr farm rebalance`；集群已创建不等于 worker 已接活。

**OSS 共享缓存**：个人 `~/.ossutilconfig` + `SR_CCACHE_REMOTE_BUCKET`，可选
`SR_CCACHE_REMOTE_PREFIX`。只在本地 miss 后查远端并回填。源码可能借此产生共享缓存写入，按用户意图启用。

**上传构建包**：另设 `SR_PKG_BUCKET`、`SR_PKG_PREFIX`（空或以 `/` 结尾），
宿主安装并配置 ossutil；只有 `sr pack --upload` 才上传。`SR_PKG_DIR` 控制本地包目录。
