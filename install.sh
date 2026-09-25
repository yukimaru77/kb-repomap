#!/usr/bin/env bash
set -euo pipefail
source_dir="$(cd -- "$(dirname -- "$0")" && pwd -P)"
install_dir="${KB_INSTALL_DIR:-$HOME/.local/bin}"
uv sync --locked --project "$source_dir"
npm ci --prefix "$source_dir" --ignore-scripts
mkdir -p "$install_dir"
python3 - "$source_dir" "$install_dir/kb" <<'PY'
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
