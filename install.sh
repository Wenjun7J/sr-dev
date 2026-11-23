#!/usr/bin/env bash
set -euo pipefail
[ "$(uname -s)" = Linux ] || { echo 'srdev 需要 Linux 宿主机；请在开发机上运行安装。' >&2; exit 1; }
for cmd in bash docker python3 flock readlink stat md5sum curl git; do
  command -v "$cmd" >/dev/null || { echo "缺少宿主依赖：$cmd" >&2; exit 1; }
done
stat -c %a / >/dev/null 2>&1 || { echo '需要 GNU coreutils（stat/readlink/md5sum）。' >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo '无法连接 Docker，请检查 daemon 和当前用户的访问权限。' >&2; exit 1; }
repo=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
python3 "$repo/files/sr-vscode.py" "$repo" "$repo" /farm/deploy --install-entries
mkdir -p "$HOME/.config/srdev"
if [ -w "$repo" ]; then
  for file in srdev.conf registries.conf; do
    if [ ! -e "$repo/$file" ]; then (umask 077; cp "$repo/$file.example" "$repo/$file"); fi
  done
  cat > "$HOME/.config/srdev/env" <<'ENV'
case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) export PATH="$HOME/bin:$PATH" ;;
esac
ENV
  CONF_HINT='填写 srdev.conf 和 registries.conf 后即可使用。'
else
  for file in srdev.conf registries.conf; do
    if [ ! -e "$HOME/.config/srdev/$file" ]; then (umask 077; cp "$repo/$file.example" "$HOME/.config/srdev/$file"); fi
  done
  cat > "$HOME/.config/srdev/env" <<'ENV'
: "${SR_CONF:=$HOME/.config/srdev/srdev.conf}"
: "${SR_REGISTRIES:=$HOME/.config/srdev/registries.conf}"
export SR_CONF SR_REGISTRIES
case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) export PATH="$HOME/bin:$PATH" ;;
esac
ENV
  CONF_HINT='工具目录为只读共享安装，个人配置已放在 ~/.config/srdev/（srdev.conf、registries.conf），填写后即可使用。'
fi
for profile in "$HOME/.profile" "$([ "${SHELL##*/}" = zsh ] && echo "$HOME/.zshrc" || echo "$HOME/.bashrc")"; do
  if ! grep -Fq '# srdev PATH' "$profile" 2>/dev/null; then
    printf '\n# srdev PATH\n[ ! -f "$HOME/.config/srdev/env" ] || . "$HOME/.config/srdev/env"\n' >> "$profile"
  fi
done
echo 'sr、clangd、lldb-dap、lldb 入口已安装到 ~/bin；已有入口或配置不会被覆盖。'
echo "$CONF_HINT"'当前终端运行：source ~/.config/srdev/env'
echo '新 worktree 运行 sr ide 配置 clangd；sr deploy 自动准备调试配置。'
echo 'VS Code Remote SSH 请安装 vscode-clangd、LLVM lldb-dap 和 Debugger for Java 扩展。'
