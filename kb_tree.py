"""Render a repository file list as an indented `tree`-style outline."""

MAX_TREE_CHARS = 60_000


def _nest(paths):
    root = {}
    for path in paths:
        parts = [part for part in path.split("/") if part]
        if not parts:
            continue
        node = root
        for directory in parts[:-1]:
            child = node.setdefault(directory, {})
            if child is None:  # a path is both a file and a directory: keep the directory
                child = node[directory] = {}
            node = child
        node.setdefault(parts[-1], None)
    return root


def _lines(node, depth, files):
    for name in sorted(node):
        child = node[name]
        if child is None:
            if files:
                yield "  " * depth + name
        else:
            yield "  " * depth + name + "/"
            yield from _lines(child, depth + 1, files)


def _render(root, files):
    return "\n".join([".", *_lines(root, 1, files)]) + "\n"


def render_tree(paths, max_chars=MAX_TREE_CHARS):
    """Tree with files if it fits in `max_chars`; otherwise directories only.

    The directories-only form starts with `(files omitted: N files; directories only)`
    and a blank line, so it can sit directly under a section heading.
    """
    root = _nest(paths)
    full = _render(root, files=True)
    if len(full) <= max_chars:
        return full
    count = sum(1 for line in _lines(root, 0, True) if not line.endswith("/"))
    return f"(files omitted: {count} files; directories only)\n\n" + _render(root, files=False)
