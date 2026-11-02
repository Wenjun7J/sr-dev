# 开发与 Git 管理

`srdev` 工具仓库与被编译的 StarRocks 源码仓库分别管理。日常操作：

```bash
cd ~/srdev
git status --short --branch
git switch -c docs/onboarding-update       # 按技术问题命名
# 修改后先检查实际差异，确认没有凭据或客户信息
git diff --check
git diff
git add <本次改动的文件>
git diff --cached
git commit -m "docs: clarify first build setup"
```

本仓库保留了此前的工具开发历史。真实站点配置及其备份、凭据、农场状态和构建产物不入库；
只维护根目录的 `srdev.conf.example` 与 `registries.conf.example`。
`files/srdev.conf.example` 是指向根模板的兼容软链接。
`.dockerignore` 仅允许 Dockerfile 的构建输入进入 Docker 上下文。

远端开发机上的 `~/srdev` 是可直接提交的工作仓库。可以从有 SSH 访问权限的机器克隆：

```bash
git clone <ssh-alias>:srdev ~/srdev
```

本地修改提交后，若要交给这台开发机，推到技术命名的分支，再在开发机上审阅、合并。
不要直接推到远端当前检出的分支；Git 默认会拒绝。Git 合并只更新工具文件，
镜像或运行容器是否需要升级，应另外核对内容标识和当前任务。
需要 GitHub/GitLab 备份时，再为双方配置经过确认的仓库 URL。

## 验证

Python 文件可以用 `ast.parse` 检查，Shell 文件用 `bash -n`；这些检查不执行构建。
测试使用 Python 标准库 unittest。部分测试依赖 GNU 工具、`/farm` 和独立网络命名空间，
必须在一次性 Linux 容器中运行，不能只凭宿主机上带 skip 的结果声称完整通过。

先取得本机已存在的工具镜像 ID，然后在项目根目录运行：

```bash
# 已有个人 sr 容器时，可用 docker inspect -f '{{.Image}}' <user>_farm 取得镜像 ID。
# 尚无镜像时先按 README 配置并运行 sr image。
test_image=<已存在的工具镜像ID>
docker run --rm --network none --cap-drop NET_ADMIN \
  -e PYTHONDONTWRITEBYTECODE=1 -e SR_TEST_NETNS=isolated \
  -v "$PWD:/repo:ro" -w /repo "$test_image" \
  bash -c 'mkdir -p /farm; python3 -m unittest discover -s tests -v'
```

不要挂入真实 `~/farm`、Docker socket 或凭据。测试使用临时模拟产物，不需要真实 FE/BE
或云平台，也不会申请农场。修改镜像内 helper 时，需使用包含对应改动的测试镜像，
或在一次性容器中覆盖相应 helper 后再测，避免误测旧镜像中的代码。

## Skill

通用 skill 位于 [`skills/srdev/SKILL.md`](../skills/srdev/SKILL.md)，包含首次安装、
配置、日常工作流和排障。它不绑定特定开发机、账号、仓库路径或 registry。

将整个 `skills/srdev/` 目录复制到 AI 客户端的 skill 目录。Codex 使用当前
`${CODEX_HOME:-$HOME/.codex}/skills/srdev`；本机长期保留此仓库时也可建立软链接。
已有同名 skill 时先比较，不要覆盖不同内容。其他 AI 可直接读取 `SKILL.md` 及其引用文件。

典型请求：`用 $srdev 检查这台开发机的配置，并完成第一次本地 BE 构建。`
skill 的格式校验不能替代实际命令和项目测试。
