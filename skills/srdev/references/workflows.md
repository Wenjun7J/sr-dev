# 日常工作流与排障

以下 `sr` 均在目标 Linux 宿主机运行；构建、部署、IDE 命令的工作目录是选定的 StarRocks worktree。
首次使用或切分支时加 `-v <依赖版本>`；已有版本记录可省略。已有农场时保留默认 auto。
下面示例使用 auto；没有可用农场且要本地执行时改成 `--dist off`。首次安装的本地验证见 setup.md。

## 构建与单测

```bash
sr -v <版本> build --be --debug --dist auto
sr build --fe
sr build --all --dist auto
sr be-ut --dist auto --module storage --no-run
sr be-ut --dist auto --test <目标测试>
sr be-ut --dist auto --gtest_filter '<Suite>.<Case>'
sr fe-ut --test <完整测试类名>
```

正常 build 的参数用 `--` 透传给 `build.sh`；BE/FE UT 的未知参数直接透传上游 UT 脚本。
测试名称和筛选语义以选定分支的脚本为准。帮助可用 `sr build --help`、`sr be-ut --help`；
注意这些命令经过容器初始化，不是完全无副作用的预检。只读代码中 usage 可避开初始化。

- 普通默认树：`be/nj/build_Release`（Debug/ASAN 对应后缀）。
- BE UT 默认动态树：`be/nj-dyn/ut_build_ASAN`；`--no-dynlib` 使用静态 `be/nj/ut_build_ASAN`。
- `--build-prefix <目录>` 可隔离实验构建树；相对路径按源码树根解析。旧分支不支持此机制时须核对实际路径。
- `--clean` 会删除相应构建树；上游 BE UT 还可能删除 `be/output/`。有真实需要再清，不作为通用排障第一步。
- BE UT 的 `.so`、第三方运行库与产物有关联，不能把测试二进制单独拿到宿主机运行。
- `--no-java-ext` 会缺少部分连接器必需 jar，不用于准备完整部署包。

## IDE 与调试

```bash
sr compdb             # 独立 be/nj-compdb 树，configure + 代码生成，不做完整编译
sr ide                # 补齐当前 worktree 的 clangd 配置，保留已有自定义设置
```

容器 clangd/LLDB 默认使用 LLVM 20。Remote SSH 侧需要 vscode-clangd、LLVM lldb-dap；
FE 调试需要 Debugger for Java 和 Java 语言支持。
`sr compdb` 的第三方路径在宿主和容器中都可访问，适合多版本头文件跳转。
普通 BE/UT 构建会把根 compdb 软链接指向最后构建的树；恢复精确第三方跳转可再跑 compdb。
compdb 只覆盖 BE 主程序，测试代码的数据库要通过 UT 构建生成。

`sr deploy` 成功后合并 `.vscode/launch.json`（包括 `--no-start`）；FE 自动启用回环 JDWP，
BE 通过 `~/bin/lldb-dap` 进入容器附加。同名配置仅更新工具管理字段，其他配置和 JSONC 注释保留。
`--status` / `--stop` 不生成 IDE 配置。附加 BE 会暂停进程并影响 FE 心跳，应在调试任务范围内操作。

调试 BE UT 时，按仓库 README 的 LLDB 示例设置 `SR_UT_TREE` 和生成的 `.sr-lldb-env`，
目标进程使用 UT 的库路径，调试器自身不能混入 SR 自带 libLLVM。
ASAN 调试使用 `disableASLR: false`；默认关闭 ptrace 下不兼容的 LSan，普通 UT 不受此设置影响。

## 本地部署与打包

```bash
sr deploy --status
sr deploy                  # 从当前 output/ 替换共用部署，默认保留数据
sr deploy --be-only         # 只替换 BE，FE 保持原状态
sr deploy --fe-only
sr pack --name dev-build    # 仅本地打包
```

默认部署在容器 `/farm/deploy`，宿主对应 `~/farm/deploy`。`SR_DEPLOY_DIR` 可指定容器可见的另一目录。
worktree 不同并不代表部署不同；切树 deploy 会替换原部署的产物。
部署互斥按目录加锁，端口分配另有同机共享锁。
`--no-start` 只代表不启动，并不保证原服务保持运行；`--clean-data` 会删除 FE meta / BE storage。

新部署端口候选基址为 `24000+(uid%300)*20`，冲突时自动避让，实际结果持久化。
FE HTTP/RPC/query/edit-log 为基址 +0/+1/+2/+3，BE 的四个服务端口为 +4..7，FE JDWP +8，starlet +9。
已有数据/启动过的部署保留端口；连接信息从实际部署状态和配置读取，不套用固定 9030 或历史端口。
部署失败查看对应 `fe/log` / `be/log`，不能把“脚本已复制产物”当成服务已就绪。

打包需要当前树的 `output/`，通过源码树锁避免与构建发布并发。
`sr pack --upload` 才上传；`--force` 可覆盖本地包与远端同名对象，使用前核对目标。
默认包名包含源码树名，技术命名能避免把敏感信息带入共享产物。

## 农场与状态

| 命令 | 实际作用 |
|---|---|
| `sr farm status` / `sr farm usage` | 查平台/节点状态、scheduler 槽位和负载；usage 支持 `--watch 5` |
| `sr farm init` | 申请 worker，铺密钥、配置 icecc、预热，产生云资源 |
| `sr farm resume` / `suspend` | 恢复/休眠现有云节点 |
| `sr farm rebalance` | 重启 scheduler 并预热，正在分布式编译的任务可能短暂回退本机 |
| `sr farm down` | 请求释放云资源，确认后清理本地状态 |
| `sr down` | 删除个人构建容器；不会等价地立即释放云节点 |

农场闲置约 15 分钟休眠，看门狗每约 15 分钟把到期时间续到约 3 小时后；
停止容器就停止续约，不要当成即时释放机制。
`auto` 根据重编规模、农场状态和 ccache miss 决定外发；FE 和链接不走远端编译。
初始化成功仍需确认 worker 连接与非零 speed；仅云平台 Running 不足以证明编译能力。

## 常见问题

| 现象 | 先检查什么，再做什么 |
|---|---|
| 首次非交互拒绝开工 | 明确传 `-v`；无农场且要本机编则传 `--dist off` |
| history 出现 `@?` / `?/?` | 分别是 `SR_BRANCH` 和 `SR_VER` / `SR_TPDIGEST` 缺失的回退值。检查是否绕过宿主 sr 直接调用了容器 sr-build；不代表 Git 分支或依赖不存在 |
| history 显示本地，但预期走农场 | 先查原始调用是否有 `--dist off`，再查外层日志的“模式”及原因。history 目前只保存模式简称，归档的构建日志不含外层决策表头 |
| 找不到版本/digest | registry 表、`SR_TP_REPO`、匿名 HTTP v2 可达性、tag；不要自动换仓库。tag 列表有约一小时缓存 |
| 工具版本不一致 | 比较当前工具输入摘要与容器 `/usr/local/share/sr/image-id`；准备镜像后在空闲升级窗口 `sr down && sr up` |
| `/farm` 或 `/tp` 挂载漂移 | 核对宿主目录是否改名/重建；当前实现拒绝继续。不能删除状态强行绕过；空闲时重建挂载 |
| `degraded` / worker 无任务 | 看连接数、speed、实际 iceccd 槽位和日志；必要时 resume 修复或 rebalance。要本机执行可明确 `--dist off` |
| OOM / 链接停滞 | 看 cgroup `memory.current`、`memory.events`、anon 和日志；按任务调本机槽位/链接数/内存，失败不自动重跑 |
| 源码树锁占用 | 查是否有真实构建；用另一 worktree，不删锁。`sr history` 的运行中判据是锁而非旧 PID |
| clangd 找不到头/跳错版本 | 确认 wrapper、当前版本记录和 compdb；主程序用 `sr compdb`，UT 用对应 UT 构建 |
| 删除配置后仍有旧值 | `/farm/srdev.conf` 可能是上次同步副本；直接修改可信源配置，再核对同步结果 |

日志主要在宿主 `~/farm/logs/` 与 `~/farm/builds.tsv`；配置可通过 `BASE` 改变实际根目录。
`sr history` 会读取状态并清理已确认不再持锁的残留运行记录。
`farm status` / `deploy --status` 也经过 `ensure_container`：可能同步配置、调整限额，容器不存在时甚至创建容器。
纯调查先用 `docker inspect`、文件检查和必要的 `docker exec` 读取，确认可接受这些副作用后再用 sr 状态命令。
