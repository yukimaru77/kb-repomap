#!/usr/bin/env bash
# Install kb and what `kb NAME --remote codex|claude` needs (uv, mitmproxy).
# Re-running is safe. Set KB_NO_AUTO_INSTALL=1 to only report missing tools.
set -euo pipefail
source_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
install_dir="${KB_INSTALL_DIR:-$HOME/.local/bin}"
auto="${KB_NO_AUTO_INSTALL:-0}"

say() { printf 'kb install: %s\n' "$*" >&2; }
die() { say "$*"; exit 1; }

user_path="$PATH"
# uv's installer and `uv tool install` put binaries here; make them usable now.
export PATH="$HOME/.local/bin:$install_dir:$PATH"

if ! command -v uv >/dev/null 2>&1; then
  [ "$auto" = 1 ] && die "uv が見つかりません。導入: curl -LsSf https://astral.sh/uv/install.sh | sh"
  command -v curl >/dev/null 2>&1 || die "uv も curl も見つかりません。https://docs.astral.sh/uv/ から uv を導入してください"
  say "uv が無いため公式インストーラで導入します (https://astral.sh/uv)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  command -v uv >/dev/null 2>&1 || die "uv の導入後も uv が見つかりません。シェルを開き直して再実行してください"
fi

# Run uv; if the user's own uv.toml cannot be parsed (e.g. an option written
# for another uv version), retry once ignoring user-level config only.
# --no-config is not used because it would also skip this project's [tool.uv].
uv_safe() {
  local log status
  log="$(mktemp)"
  set +e
  uv "$@" 2>"$log"
  status=$?
  set -e
  cat "$log" >&2
  if [ "$status" -ne 0 ] && grep -q "Failed to parse" "$log" && grep -q "uv.toml" "$log"; then
    say "あなたの uv.toml を読めなかったため、ユーザー設定を無視して再実行します（uv.toml 自体は変更しません）"
    local empty
    empty="$(mktemp)"
    UV_CONFIG_FILE="$empty" uv "$@"
    status=$?
    rm -f "$empty"
  fi
  rm -f "$log"
  return "$status"
}

command -v npm >/dev/null 2>&1 || die "npm が見つかりません（KB作成に使う repomix 用）。Node.js を導入してから再実行してください: https://nodejs.org/"

uv_safe sync --locked --project "$source_dir"
npm ci --prefix "$source_dir" --ignore-scripts

# --remote (stealth mode) runs a per-session mitmdump.
if ! command -v mitmdump >/dev/null 2>&1; then
  if [ "$auto" = 1 ]; then
    say "mitmdump が見つかりません（--remote に必要）。導入: uv tool install mitmproxy"
  else
    say "mitmproxy を導入します（--remote に必要）"
    uv_safe tool install mitmproxy
  fi
fi

mkdir -p "$install_dir"
"$source_dir/.venv/bin/python" - "$source_dir" "$install_dir/kb" <<'PY'
import os
from pathlib import Path
import shlex
import sys
root, output = map(Path, sys.argv[1:])
output.write_text('#!/usr/bin/env bash\nexec ' + shlex.quote(str(root / '.venv/bin/python'))
                  + ' ' + shlex.quote(str(root / 'kb_cli.py')) + ' "$@"\n')
os.chmod(output, 0o755)
print(f'installed: {output}')
PY

# Put the install dir on PATH for future shells (idempotent, marked block).
# Set KB_NO_PATH_EDIT=1 to skip editing shell startup files.
add_path_line() {
  local rc="$1" line="$2"
  [ -f "$rc" ] && grep -qF "# >>> kb PATH >>>" "$rc" && return 0
  mkdir -p "$(dirname "$rc")"
  printf '\n# >>> kb PATH >>>\n%s\n# <<< kb PATH <<<\n' "$line" >> "$rc"
  say "PATH を追記しました: $rc"
}
if ! printf '%s' ":$user_path:" | grep -q ":$install_dir:"; then
  if [ "${KB_NO_PATH_EDIT:-0}" = 1 ]; then
    say "$install_dir を PATH に追加してください（例: export PATH=\"$install_dir:\$PATH\"）"
  else
    case "$(basename "${SHELL:-}")" in
      zsh)  add_path_line "${ZDOTDIR:-$HOME}/.zshrc" "export PATH=\"$install_dir:\$PATH\"" ;;
      bash) add_path_line "$HOME/.bashrc" "export PATH=\"$install_dir:\$PATH\""
            add_path_line "$HOME/.bash_profile" "export PATH=\"$install_dir:\$PATH\"" ;;
      fish) add_path_line "${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish" "fish_add_path -g \"$install_dir\"" ;;
      *)    add_path_line "$HOME/.profile" "export PATH=\"$install_dir:\$PATH\"" ;;
    esac
    say "新しいシェルを開くと kb が使えます（今のシェルでは: export PATH=\"$install_dir:\$PATH\"）"
  fi
fi
say "完了。Codex の --remote を使う場合は一度だけ 'kb ca-setup' の案内に従って CA を信頼登録してください（Claude だけなら不要）"
