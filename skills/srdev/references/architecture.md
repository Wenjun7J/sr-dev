# 实现与维护

```text
Linux 宿主 sr
  ├─ srdev.conf / registries.conf → 解析 tag → 完整 digest → 共享 thirdparty
  ├─ 个人 <user>_farm 容器
  │    ├─ sr-build → 目标源码树 build.sh / run-*-ut.sh
  │    │    ├─ BE：Ninja → ccache → icecc → 本机/worker
  │    │    ├─ 链接：本机 linker + 跨构建树 linkgate
  │    │    └─ FE：Maven
  │    ├─ sr-farm → tspapi.py → 云节点 → node-setup.sh
  │    └─ sr-deploy → 共用的 FE/BE 部署
  └─ sr-vscode.py → 工作区 JSONC → clangd-host / lldb-host → 容器工具
```

## 看哪些文件

| 文件 | 责任与关键点 |
|---|---|
| `sr` | 宿主入口、配置同步、镜像标识、容器限额/挂载检查、版本解析、依赖提取、打包和转发 |
| `files/sr-tp-prune.py` | 宿主依赖维护：引用登记、全用户引用扫描、来源 tag 校验、目录锁及按 digest 清理 |
| `Dockerfile` | 工具镜像。toolchains base 提供基础工具链，另装 GCC 11/12/13、icecc、ccache、LLVM 20 |
| `files/sr-build` | 源码树锁、依赖记录、独立构建树、并发/缓存/农场接线、调用上游、日志和结果 |
| `files/sr-farm` | 节点生命周期、健康检查、预热、续约、休眠；状态查询失败与资源不存在有不同含义 |
| `files/tspapi.py` | TSP API，标准库；凭据来自个人配置。替换平台需保留输出/退出码契约 |
| `files/sr-deploy`、`sr-deploy-lock.py` | 部署互斥、端口避让、产物更新、服务启动验证、FE JDWP |
| `files/ccache-pch`、`ccache.conf` | PCH 校验和与缓存设置；变更涉及构建正确性，不能只用命中率验收 |
| `files/oss-ccache-proxy.py` | ccache HTTP 请求与 OSS 签名请求适配，使用个人凭据和代理令牌 |
| `files/cmake-linkpool`、`linkgate`、`dynlib-compat.cmake` | CMake 注入、单构建/跨树链接闸门、BE UT 动态模块的 PCH 兼容；不改 SR 源码 |
| `files/clangd-host`、`lldb-host`、`lldb-dap-env`、`sr-vscode.py` | IDE 宿主入口、依赖路径、调试运行库隔离与配置合并 |
| `files/sr-image-id.py` | 对 Dockerfile、sr、COPY 输入及宿主 helper 和构建参数做内容哈希 |

## 状态与不变量

- 依赖按 **完整 SHA256 digest** 提取并发布，`.ready` 表示完成；不能用旧 tag 缓存代替 digest 引用。
  不同 GCC 必须配对应 icecc 工具链，不能拿另一版本兜底。
- 真实构建在私有 mount namespace 中把所选 thirdparty 挂到固定 `/var/local/thirdparty`。
  compdb 使用宿主可见的 digest 路径，以支持编辑器打开第三方头文件。
- `~/farm/versions/` 记录源码树 + 分支对应的版本/digest；成功取得源码树锁后才发布记录，避免失败并发请求影响 IDE。
- 同一源码树的锁覆盖 gensrc、构建和打包；不同树可并发但共用本机 iceccd 和链接槽。
- thirdparty 目录 inode 本身是全用户共享锁：宿主入口、容器构建和 IDE 在 fd 6 持读锁，prune 实际扫描/删除持排他锁。
  构建子进程须继承 fd 6，常驻服务和不使用依赖的后台任务须关闭；不删除或替换锁定的目录。
  `.sr-tp-ref-*` 保存自定义 farm 路径，不随容器删除清理。`org.srdev.tp-lock=1` 标记支持该协议的工具镜像。
  prune 在宿主以 sudo 处理跨用户权限，不通过 ensure_container，不自动升级旧容器。
- 个人 `~/farm` 存配置副本、农场状态、专用 SSH 密钥、日志和部署；`~/ccache`、`~/m2` 持久缓存。
  这些不是源码，不能入库。`BASE` / `WORK` 等覆盖项存在时以实际配置和 Mounts 为准。
- 工具版本校验失败不自动删除容器；维护入口仍允许状态、停止等恢复操作。
  Git 提交本身不重启运行环境。
- `tspapi.py exists`：0=存在，1=已确认不存在，2=查询失败；未知命令也应返回 2。
  不能把网络/鉴权失败当成已释放资源。

## 修改和验证

先读工具仓库 `AGENTS.md`、`docs/development.md` 和目标模块；检查现有未提交改动。
不因文档整理顺带重构工具或更改 StarRocks 源码。
参数、默认值和配置变更同步到 README、根模板和本 skill，避免维护第二份独立模板。

至少执行 shell 语法、Python AST、`git diff --check`；项目回归测试在一次性 Linux 容器里运行。
部署测试需要独立网络命名空间和临时 `/farm`，不能挂入真实农场状态。
当前工具仓库的测试主要覆盖配置/挂载、部署锁/失败、FE debug、镜像标识及 VS Code JSONC 合并；
它们不等同于真实 StarRocks 全量编译、云节点申请或端到端性能验证。

若修改镜像输入，核对镜像摘要是否变化，并分别验证候选镜像与运行容器的版本。
只改文档/skill/测试不应迫使用户重建容器；可对比修改前后的 `sr-image-id.py` 输出。
`.dockerignore` 仅放行 Dockerfile 的 COPY 输入，新输入需要同步允许。

skill 可整体复制到其他 AI 的 skill 目录；不要加入个人 SSH 别名、实际节点地址、凭据或客户现场材料。
