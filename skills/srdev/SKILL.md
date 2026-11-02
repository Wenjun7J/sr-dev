---
name: srdev
description: 使用 srdev 的 sr 命令在 Linux 开发机上配置和管理 StarRocks 多版本构建、BE/FE 单测、icecc 编译农场、本地部署及 clangd/LLDB/Java 调试。适用于首次安装、配置、上手、日常操作和工具链排障；不替代目标源码仓库的开发规范，也不负责通用生产集群运维。
---

# srdev

## 先建立正确上下文

srdev 是 Bash + Python 的开发工具链。宿主机 `sr` 选择依赖和容器，容器内
`sr-build` 调用目标仓库自己的构建脚本；BE 编译可以接 ccache 与 icecc，FE 使用 Maven。
同一用户默认共用一个 `<user>_farm` 容器和一套 FE/BE 部署；不同 worktree 的构建目录各自独立。
切换依赖版本通常不需要换容器。

先区分 **工具目录、StarRocks 源码树、容器、部署目录**，不要在 `~/srdev` 中执行 StarRocks 构建。
不绑定特定 SSH 别名、用户、registry 或企业版/开源版仓库。沿用用户指定的目标；
未给出的路径先从现有安装和配置中发现，不从旧会话硬猜。

在目标 Linux 主机上检查：

```bash
command -v sr
readlink -f "$(command -v sr)"    # 得到工具入口；工具目录通常是其父目录
git -C <工具目录> status --short --branch
git -C <源码树> status --short --branch
git -C <源码树> remote -v
docker ps --filter name=<当前用户>_farm --format '{{.Names}} {{.Image}} {{.Status}}'
```

尚未安装时，直接从用户提供的 srdev checkout 开始。
确认目标源码树含 `build.sh`、`be/src`，并可通过容器的挂载路径访问。
`srdev.conf` 会被 shell source，不能当成不可信数据执行或把其全文贴到报告中。
读取必要的非敏感配置；密码、token、OSS 凭据只检查是否存在。

## 按任务读资料

- **新人、新机器、配置迁移**：读 [首次安装与配置](references/setup.md)。先完成明确版本的本地构建，再按需启用农场和共享缓存。
- **编译、单测、IDE、部署、打包或排障**：读 [日常工作流](references/workflows.md)。使用当前参数和实际日志，不沿用旧命令。
- **修改 srdev 本身、理解模块与状态、验证变更**：读 [实现与维护](references/architecture.md)，再读工具仓库的 `AGENTS.md` 和相关源码。

本 skill 随仓库维护，但已安装副本可能落后。遇到差异时以目标主机当前代码为准，
优先核对 `sr` 的分发、`files/sr-build` 的参数解析、`files/sr-deploy` 和根配置模板。

## 日常操作的关键约束

- 日常构建和单测走宿主机 `sr` 入口，不直接 `docker exec ... sr-build`。宿主入口负责依赖解析、挂载和传入 `SR_BRANCH`、`SR_VER`、`SR_TPDIGEST`；直接调用内部驱动容易遗漏这些步骤，history 就会出现 `@?` / `?/?`。只有诊断内部驱动的任务才考虑直接调用，并显式核对所需环境。
- 第一次非交互构建显式传 `-v <依赖版本>`；版本按源码树 + 分支记忆，tag 后续自动跟随 registry，**不是固定 digest**。
- 普通 `sr build` 默认 **BE+FE、Release**。已有农场时保留默认 `--dist auto`，不要仅因“只跑少量单测”就强制本地。明确选择本地执行、没有可用农场且本地执行符合任务意图，或做本地对照实验时才加 `--dist off`；不要为了消除提示擅自申请农场。
- `sr be-ut` / `sr build --be-ut` 默认 **ASAN、动态模块、PCH，编完接着跑**；只编用 `--no-run`。
  `ut` / `--ut` 和用户级 `-j` 已移除。并发改 `SR_LOCAL_JOBS` / `SR_NODE_JOBS`，FE UT 可单独改 `SR_FE_UT_JOBS`。
- 同一源码树的构建串行；并行任务用不同 worktree。不要删除锁文件或绕过 sr 直接运行上游 UT 脚本来规避锁。
- 不自动重试失败构建。先查当前错误、日志和内存压力；依赖变化导致的 clean 与用户主动 clean 都可能触发全量编译。
- `sr deploy` 会替换该用户共用部署并停启本次涉及的组件。`--no-start` 仍会更新产物并可能停止已有组件，不是 dry-run。
  只有部署任务才执行部署；`--clean-data` 必须有删除数据的明确授权。
- `sr down` 删除个人容器，连带中断其中的服务与构建；`sr farm down` 释放云资源。两者不可混用。
  工具版本不一致时先核对内容标识、当前任务和升级范围，不自动重建活跃容器。
- TSP 申请/释放、OSS 上传、重启和清数据按当前任务的授权执行。已有明确授权就继续，不重复索要确认。
- 不把站点配置、凭据、客户信息或生产标识写入代码、分支、提交、skill 和示例。打包名也应只描述技术用途。

## 输出与验证

交代目标主机/源码树、选用的依赖版本、构建目标与类型、执行命令、退出状态和日志位置。
把「配置检查通过」「工具回归测试通过」「真实 StarRocks 构建/运行通过」分开报告。
没有执行的构建、云操作或调试验证不算成功；历史耗时不能当成本次结果。
