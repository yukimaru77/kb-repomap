"""Remove conversation/instruction meta lines from a decrypted compaction text.

A compaction summary also records the state of the conversation that produced it
(restated instructions, output format, budgets, plans). Only the repository
knowledge is useful to a reader. The model only *chooses* line ranges; deletion is
mechanical, so knowledge is never rewritten. Lines that mix meta and knowledge stay.
"""
import json
import sys

import kb_api

MIN_TOKENS = 2_000
MAX_REMOVED_SHARE = 0.10
INSTRUCTIONS = "You clean knowledge-base text. Output JSON only."
PROMPT = """Below is a knowledge-base text produced by compacting a conversation about a code repository. Lines are numbered.
Some lines are not repository knowledge but describe the conversation itself: restated user/developer instructions, the requested output format or sections, token budgets, tool usage, the task status, plans, or suggestions for the next response.

Return the line ranges that contain ONLY such conversation/meta content.
Rules:
- Never include a line that states any repository fact (paths, identifiers, behavior, numbers, contracts, conflicts, legends/aliases used by the knowledge, etc.), even if the line also contains meta content.
- Section headings that only wrap meta content (e.g. "## CONV") may be included; headings of knowledge sections may not.
- When unsure, keep the line.

Output JSON: {{"delete": [[first_line, last_line], ...]}} (1-based, inclusive). Output {{"delete": []}} when there is nothing to delete.

{numbered}
"""


def _ranges(text, line_count):
    data = json.loads(text[text.index("{"):text.rindex("}") + 1])
    lines = set()
    for item in data.get("delete", []):
        if (isinstance(item, list) and len(item) == 2 and all(isinstance(v, int) for v in item)
                and 1 <= item[0] <= item[1] <= line_count):
            lines.update(range(item[0], item[1] + 1))
    return lines


def strip_meta(text, *, caller=None, model=kb_api.DEFAULT_MODEL, effort="medium"):
    """Return (clean_text, report). On any doubt the original text is returned unchanged."""
    total = kb_api.est_tokens(text)
    report = {"tokens": total, "removed_tokens": 0, "deleted_lines": [], "status": "kept"}
    if total < MIN_TOKENS:
        report["status"] = "short"
        return text, report
    lines = text.split("\n")
    numbered = "\n".join(f"{index}\t{line}" for index, line in enumerate(lines, 1))
    try:
        answer = (caller or _call)(PROMPT.format(numbered=numbered), model, effort)
        delete = _ranges(answer, len(lines))
    except (ValueError, KeyError, OSError) as error:
        report["status"] = f"error: {error}"[:200]
        return text, report
    if not delete:
        return text, report
    removed = kb_api.est_tokens("\n".join(lines[index - 1] for index in sorted(delete)))
    if removed > total * MAX_REMOVED_SHARE:
        report["status"] = f"rejected: would remove {removed} of {total} tokens"
        return text, report
    clean = "\n".join(line for index, line in enumerate(lines, 1) if index not in delete).strip("\n") + "\n"
    report.update(status="stripped", removed_tokens=removed, deleted_lines=sorted(delete))
    return clean, report


def _call(prompt, model, effort):
    body = {"model": model, "instructions": INSTRUCTIONS, "input": [kb_api.u(prompt)],
            "tools": [], "tool_choice": "auto", "parallel_tool_calls": False,
            "reasoning": {"effort": effort}, "store": False, "stream": True}
    text, completed = [], False
    with kb_api.http("/responses", body, timeout=1800, stream=True) as response:
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


if __name__ == "__main__":
    source = sys.stdin.read()
    clean, report = strip_meta(source)
    print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
    sys.stdout.write(clean)
