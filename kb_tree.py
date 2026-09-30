"""Render a repository file list as an indented `tree`-style outline."""


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


def render_tree(paths, include_files=False):
    """`.` then sorted entries, 2 spaces per level, directories suffixed `/`.

    Directories only unless `include_files`.
    """
    return "\n".join([".", *_lines(_nest(paths), 1, include_files)]) + "\n"
