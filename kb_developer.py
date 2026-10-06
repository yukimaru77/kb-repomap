"""Per-launch developer notes, appended without modifying the KB store."""
import argparse
from pathlib import Path


def client_boundary(argv):
    """Find the client verb without treating an option's text/path as the verb."""
    index = 1
    valued = {"--append-dev", "--append-dev-file", "--store", "--file", "--workspace", "--rebuild"}
    while index < len(argv):
        if argv[index] in valued:
            index += 2
        elif argv[index] in ("codex", "claude"):
            return index
        else:
            index += 1
    return None


class AppendDeveloper(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        entries = list(getattr(namespace, self.dest, None) or [])
        entries.append((option_string, values))
        setattr(namespace, self.dest, entries)


def add_arguments(parser):
    parser.add_argument("--append-dev", dest="developer_additions", action=AppendDeveloper,
                        metavar="TEXT", help="既存dev.txtの後に起動時だけ追加する文章（複数指定可）")
    parser.add_argument("--append-dev-file", dest="developer_additions", action=AppendDeveloper,
                        metavar="PATH", help="既存dev.txtの後に起動時だけ追加するUTF-8ファイル（複数指定可）")


def read_additions(args):
    parts = []
    for option, value in getattr(args, "developer_additions", None) or []:
        if option == "--append-dev-file":
            try:
                value = Path(value).expanduser().read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                raise ValueError(f"--append-dev-fileを読めません: {value}: {error}") from error
        if value.strip():
            parts.append(value)
    return "\n\n".join(parts)


def append_text(existing, additions):
    if not additions:
        return existing
    return f"{existing}\n\n{additions}" if existing and existing.strip() else additions
