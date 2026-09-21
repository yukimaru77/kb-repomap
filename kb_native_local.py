"""Adapt fresh native Codex launches to a locally seeded KB session.

Remote KBs do not need this translation. Locally, native Codex must resume the
session into which the app-server injected the KB. Discover option arity from
the installed CLI so new Codex options do not need a parallel allowlist here.
"""
from functools import lru_cache
import json
import os
import re
import subprocess


@lru_cache(maxsize=4)
def _metadata(path=()):
    result = subprocess.run(
        ["codex", *path, "--help"], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode:
        raise ValueError("Codex のオプションを確認できません: " + result.stderr.strip())
    options, commands = {}, {}
    section = None
    for line in result.stdout.splitlines():
        if line and not line[0].isspace() and line.endswith(":"):
            section = line[:-1]
            continue
        value = line.strip()
        if section == "Options" and re.match(r"--?[A-Za-z0-9]", value):
            # Only declaration lines begin with an option, not their indented
            # descriptions. Restrict the scan to text preceding the value.
            names = re.findall(r"(?<!\S)(--?[A-Za-z0-9][A-Za-z0-9-]*)(?=[, =<\[]|$)", value)
            for name in names:
                options[name] = "many" if re.search(r"<[^>]+>\.\.\.", value) else "<" in value
        elif section == "Commands" and re.match(r"[A-Za-z0-9][A-Za-z0-9-]*\s{2,}", value):
            name = value.split()[0]
            commands[name] = name
            aliases = re.search(r"\[aliases: ([^]]+)\]", value)
            if aliases:
                for alias in aliases[1].split(","):
                    commands[alias.strip()] = name
    if not options:
        raise ValueError("Codex の --help からオプションを読み取れません。--remote を使用してください")
    return options, commands


def _option_info(token, options):
    if token.startswith("--"):
        name, attached, _value = token.partition("=")
        return name, options.get(name, False), bool(attached)
    name = token[:2]
    return name, options.get(name, False), len(token) > 2


def _option_width(args, index, options):
    """Return the number of tokens occupied by an option, or zero for a prompt."""
    token = args[index]
    if token == "-" or not token.startswith("-"):
        return 0
    _name, arity, attached = _option_info(token, options)
    if arity == "many":
        end = index + 1
        while end < len(args) and (args[end] == "-" or not args[end].startswith("-")):
            end += 1
        if end == index + 1 and not attached:
            raise ValueError(f"{token} に値が必要です")
        return end - index
    else:
        # -mMODEL and -ckey=value already carry their value. Boolean clusters
        # and unknown flags remain intact for Codex's own validation.
        takes_value = arity and not attached
    if takes_value:
        if index + 1 == len(args):
            raise ValueError(f"{token} に値が必要です")
        return 2
    return 1


def _first_positional(args, options):
    index = 0
    while index < len(args):
        if args[index] == "--":
            return None
        width = _option_width(args, index, options)
        if not width:
            return index
        index += width
    return None


def _remote_required(command_name):
    raise ValueError(
        f"ローカル KB では codex {command_name} のセッションを置き換えられません。"
        "kb NAME --remote codex ... を使用してください"
    )


def subcommand_index(native_args):
    """Locate a real root subcommand without mistaking option values for one."""
    options, commands = _metadata()
    index = _first_positional(native_args, options)
    return index if index is not None and native_args[index] in commands else None


def seed_overrides(native_args):
    """Mirror explicit config/model settings into the initial local KB session.

    Profiles and --ignore-user-config still belong to the final native process;
    this helper does not synthesize or merge their configuration files.
    """
    args = list(native_args)
    options, commands = _metadata()
    boundary = subcommand_index(args)
    overrides = []
    model = None
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            break
        if index == boundary:
            options, _commands = _metadata((commands[token],))
            index += 1
            continue
        width = _option_width(args, index, options)
        if token in ("-c", "--config", "-m", "--model"):
            value = args[index + 1]
            if token in ("-c", "--config"):
                overrides.append(value)
            else:
                model = value
        elif token.startswith("--config="):
            overrides.append(token[len("--config="):])
        elif token.startswith("--model="):
            model = token[len("--model="):]
        elif token.startswith("-c") and not token.startswith("--") and len(token) > 2:
            overrides.append(token[2:])
        elif token.startswith("-m") and not token.startswith("--") and len(token) > 2:
            model = token[2:]
        index += width or 1
    # Native --model is a harness override and wins over -c model=... even when
    # the latter occurs later in argv (Config::load uses model.or(cfg.model)).
    if model is not None:
        overrides.append("model=" + json.dumps(model))
    return overrides


def command(native_args, session_id):
    """Return a native launch argv without reading the prompt from stdin."""
    args = list(native_args)
    root_options, root_commands = _metadata()
    boundary = _first_positional(args, root_options)
    subcommand = root_commands.get(args[boundary]) if boundary is not None else None
    if subcommand is None:
        return ["codex", "resume", session_id, *args]
    if subcommand != "exec":
        _remote_required(subcommand)

    prefix, tail = args[:boundary], args[boundary + 1:]
    exec_options, exec_commands = _metadata(("exec",))
    positional = _first_positional(tail, exec_options)
    if positional is not None and tail[positional] in exec_commands:
        _remote_required("exec " + tail[positional])

    # Exec-only options such as -C and --color are not all accepted by resume.
    # Keep those in the enclosing exec parser; preserve their relative order.
    options, resume_options, prompt = [], [], []
    index = 0
    while index < len(tail):
        if tail[index] == "--":
            prompt.extend(tail[index:])
            break
        width = _option_width(tail, index, exec_options)
        if width:
            name, arity, attached = _option_info(tail[index], exec_options)
            if arity == "many":
                # A greedy exec option would eat the inserted `resume ID`.
                # Native resume accepts repeated single-value image options.
                child_options, _child_commands = _metadata(("exec", "resume"))
                if child_options.get(name) is not True:
                    _remote_required("exec " + name)
                if attached:
                    resume_options.append(tail[index])
                for value in tail[index + 1:index + width]:
                    resume_options.extend((name, value))
            else:
                options.extend(tail[index:index + width])
            index += width
        else:
            prompt.append(tail[index])
            index += 1
    return ["codex", *prefix, args[boundary], *options, "resume", session_id, *resume_options, *prompt]
