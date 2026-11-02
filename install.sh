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
for file in srdev.conf registries.conf; do
  if [ ! -e "$repo/$file" ]; then (umask 077; cp "$repo/$file.example" "$repo/$file"); fi
done
mkdir -p "$HOME/.config/srdev"
cat > "$HOME/.config/srdev/env" <<'ENV'
case ":$PATH:" in
  *":$HOME/bin:"*) ;;
  *) export PATH="$HOME/bin:$PATH" ;;
esac
ENV
for profile in "$HOME/.profile" "$([ "${SHELL##*/}" = zsh ] && echo "$HOME/.zshrc" || echo "$HOME/.bashrc")"; do
  if ! grep -Fq '# srdev PATH' "$profile" 2>/dev/null; then
    printf '\n# srdev PATH\n[ ! -f "$HOME/.config/srdev/env" ] || . "$HOME/.config/srdev/env"\n' >> "$profile"
  fi
done
echo 'sr、clangd、lldb-dap、lldb 入口已安装到 ~/bin；已有入口或配置不会被覆盖。'
echo '填写 srdev.conf 和 registries.conf 后即可使用。当前终端运行：source ~/.config/srdev/env'
echo '新 worktree 运行 sr ide 配置 clangd；sr deploy 自动准备调试配置。'
echo 'VS Code Remote SSH 请安装 vscode-clangd、LLVM lldb-dap 和 Debugger for Java 扩展。'
