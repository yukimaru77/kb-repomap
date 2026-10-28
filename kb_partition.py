"""Domain windows: cut a repository by business/technical domain across layers.

A window groups every file that has to be understood together (UI, BFF, Gateway,
migrations, tests, docs, domain-specific infra) and is compacted as one pack.
The model designs the windows once; the result is stored with the KB and reused
by later rebuilds so unchanged windows keep their blobs.
"""
import json
import re
import sys
import time
from pathlib import PurePosixPath

import kb_api

DESIGN_INSTRUCTIONS = "You design reading windows for building a repository knowledge base. Output JSON only."
INDEX_LIMIT = 4


def design_prompt(files, tokens, knowledge, count, cap):
    listing = "\n".join(f"{index}\t{tokens[path]}\t{path}" for index, path in enumerate(files))
    if knowledge:
        context = ("=== Existing knowledge (summarized per DIRECTORY window, so cross-layer links are missing) ===\n"
                   + knowledge)
    else:
        context = "=== No previous knowledge; use paths and the per-file index above to infer domains ==="
    return f"""Below is the full list of {len(files)} repository files (index, tokens, path [:: exports/tests/sql/headings]), then context about the repository.

Goal: partition ALL files into about {count} DOMAIN windows. Each window will be read in full by one model at once and compacted, so a window must group files that must be understood TOGETHER: one business/technical domain across ALL layers — UI pages/components/hooks/parsers + their tests/stories, BFF routes, server modules, SQL migrations that define their tables, unit/HTTP tests, DB tests, e2e cases, user docs/runbooks, infra pieces specific to that domain. Prefer vertical (feature) slices over horizontal (layer) slices. Shared/generic infrastructure (design system, generic libs, CI, base infra, tooling) can form their own windows.

Hard constraints:
- Every file index appears in exactly one window.
- Sum of tokens per window <= {cap}. (Very large standalone data files may form their own window.)
- About {count} windows.

Output JSON: {{"windows":[{{"name":"...","rationale":"one line: what cross-layer story this window captures","files":[indices...]}}]}}

=== Files ===
{listing}

{context}
"""


def file_index_line(path, text):
    """Mechanical hints for the designer when there is no previous KB."""
    def first(pattern, flags=0):
        found = []
        for match in re.findall(pattern, text, flags):
            value = match if isinstance(match, str) else " ".join(part for part in match if part)
            if value not in found:
                found.append(value)
        return found[:INDEX_LIMIT]
    parts = []
    exports = first(r'export\s+(?:default\s+)?(?:async\s+)?(?:function\*?|const|class|type|interface|enum)\s+([A-Za-z_]\w*)')
    if exports:
        parts.append("exports " + ",".join(exports))
    tests = first(r'\b(?:test|it|describe)\(\s*[\'"`]([^\'"`\n]{4,80})[\'"`]')
    if tests:
        parts.append("tests " + " | ".join(tests))
    sql = first(r'CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|FUNCTION|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([\w."]+)', re.I)
    if sql:
        parts.append("sql " + ",".join(sql))
    if path.endswith((".md", ".mdx")):
        headings = first(r'^#{1,3}\s+(.+)$', re.M)
        if headings:
            parts.append("headings " + " | ".join(h.strip()[:60] for h in headings))
    return "; ".join(parts)


def parse_design(text, files):
    data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    windows = []
    for window in data.get("windows", []):
        indices = [index for index in window.get("files", []) if isinstance(index, int) and 0 <= index < len(files)]
        windows.append({"name": str(window.get("name") or "domain window"),
                        "rationale": str(window.get("rationale") or ""),
                        "files": [files[index] for index in indices]})
    return windows


def affinity(path, members):
    """Length of the longest shared directory prefix between path and any member."""
    parts = PurePosixPath(path).parts[:-1]
    best = 0
    for member in members:
        other = PurePosixPath(member).parts[:-1]
        shared = 0
        for left, right in zip(parts, other):
            if left != right:
                break
            shared += 1
        best = max(best, shared)
    return best


def repair(windows, files, tokens, cap):
    """Make windows an exact partition of files within cap; deterministic."""
    known = set(files)
    seen = set()
    fixed = []
    for window in windows:
        members = [path for path in window["files"] if path in known and path not in seen]
        seen.update(members)
        if members:
            fixed.append({**window, "files": members})
    if not fixed:
        fixed = [{"name": "repository", "rationale": "", "files": []}]
    for path in files:
        if path in seen:
            continue
        sizes = [sum(tokens[member] for member in window["files"]) for window in fixed]
        target = max(range(len(fixed)), key=lambda index: (affinity(path, fixed[index]["files"]), -sizes[index]))
        fixed[target]["files"].append(path)
        seen.add(path)
    order = {path: index for index, path in enumerate(files)}
    result = []
    for window in fixed:
        members = sorted(window["files"], key=order.get)
        parts, current, used = [], [], 0
        for path in members:
            if current and used + tokens[path] > cap:
                parts.append(current)
                current, used = [], 0
            current.append(path)
            used += tokens[path]
        if current:
            parts.append(current)
        for number, part in enumerate(parts, 1):
            name = window["name"] if len(parts) == 1 else f"{window['name']} (part {number})"
            result.append({"name": name, "rationale": window.get("rationale", ""), "files": part})
    return result


def design(files, tokens, texts, *, knowledge=None, count=11, cap=400_000,
           model=kb_api.DEFAULT_MODEL, effort=kb_api.DEFAULT_EFFORT, attempts=3, caller=None):
    """Ask the model for domain windows, then repair them into a valid partition."""
    if knowledge:
        listed = files
    else:
        listed = [f"{path} :: {hint}" if (hint := file_index_line(path, texts[path])) else path for path in files]
    prompt = design_prompt(listed, {name: tokens[path] for name, path in zip(listed, files)}, knowledge, count, cap)
    caller = caller or _call
    last = None
    for attempt in range(attempts):
        try:
            text = caller(prompt, model, effort)
            windows = parse_design(text, files)
            if windows:
                return repair(windows, files, tokens, cap)
            last = ValueError("design returned no windows")
        except (ValueError, KeyError) as error:
            last = error
        print(f"retry window design: {last} ({attempt + 2}/{attempts})", file=sys.stderr, flush=True)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"window design failed: {last}")


def _call(prompt, model, effort):
    body = {"model": model, "instructions": DESIGN_INSTRUCTIONS, "input": [kb_api.u(prompt)],
            "tools": [], "tool_choice": "auto", "parallel_tool_calls": False,
            "reasoning": {"effort": effort}, "store": False, "stream": True}
    text, completed = [], False
    with kb_api.http("/responses", body, timeout=3600, stream=True) as response:
        for event in kb_api.events(response):
            kind = event.get("type")
            if kind == "response.output_text.delta":
                text.append(event.get("delta", ""))
            elif kind == "response.completed":
                completed = True
            elif kind in ("response.failed", "error"):
                raise ValueError(f"upstream returned {kind}")
    if not completed:
        raise ValueError("stream ended without response.completed")
    return "".join(text)
