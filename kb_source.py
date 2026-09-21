"""Git source addresses, including SSH aliases with absolute remote paths."""
from pathlib import Path
import re
from urllib.parse import quote, urlsplit


def source_address(value=None, *, host=None, path=None):
    if host is not None or path is not None:
        if value or not host or not path:
            raise ValueError("--source または --host と --path の組を指定してください")
        if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*", host):
            raise ValueError("SSHマシン名が不正です")
        if not path.startswith("/") or any(c in path for c in "\r\n\0"):
            raise ValueError("リモートのリポジトリには絶対パスを指定してください")
        return "ssh://" + host + quote(path, safe="/")
    if not value:
        raise ValueError("元Gitリポジトリを指定してください")
    local = Path(value).expanduser()
    if local.exists():
        return str(local.resolve())
    match = re.fullmatch(r"((?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*):(/.*)", value)
    if match and "://" not in value:
        return source_address(host=match[1], path=match[2])
    if value.startswith("git@") and ":" in value:
        return value
    parsed = urlsplit(value)
    if parsed.scheme not in ("https", "http", "git", "ssh") or not parsed.hostname:
        raise ValueError("Git URL、ローカルGitパス、または host:/絶対パス を指定してください")
    if parsed.password or (parsed.scheme != "ssh" and parsed.username):
        raise ValueError("URLに認証情報を埋め込まずGitの認証設定を使用してください")
    return value
