#!/usr/bin/env python3
"""Open `codex resume ID` in a detached tmux pane and check the transcript shows TEXT.

With --compare ORIGINAL the original thread is opened the same way and its
transcript (the scrollback above the composer) must be where the converted
one begins; turns run after the conversion may follow it. The TUI is quit
before anything is sent, so no model turn runs. The thread cwd must already be
trusted: this never answers Codex's folder-trust prompt.
"""
import argparse
import re
import subprocess
import sys
import time


def tmux(*args, check=True):
    return subprocess.run(["tmux", *args], check=check, stdout=subprocess.PIPE, text=True).stdout


def transcript(thread_id, expected, cwd, timeout):
    session = f"kb-tui-{thread_id[:8]}-{int(time.time())}"
    tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "200", "-c", cwd,
         f"codex resume {thread_id}; sleep 30")
    try:
        deadline = time.monotonic() + timeout
        pane = ""
        while time.monotonic() < deadline:
            pane = tmux("capture-pane", "-p", "-S", "-", "-t", session)
            if "Trust this folder" in pane:
                raise SystemExit(f"{cwd} is not trusted; refusing to answer the trust prompt")
            if expected in pane and "Ask Codex" in pane:
                break
            time.sleep(1)
        else:
            raise SystemExit(f"{thread_id}: {expected!r} not visible\n{pane}")
        # Everything above the composer is the restored transcript.
        lines = pane.splitlines()
        end = max(index for index, line in enumerate(lines) if "Ask Codex" in line)
        return [line.rstrip() for line in lines[:end] if line.strip()]
    finally:
        tmux("kill-session", "-t", session, check=False)


def normalize(lines):
    # Drop the startup banner and timing footers that differ between launches.
    start = next((index for index, line in enumerate(lines) if line.lstrip().startswith("›")), 0)
    return [re.sub(r"Worked for .*", "Worked for", line) for line in lines[start:]]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("thread")
    parser.add_argument("text", help="an earlier user message that must be visible")
    parser.add_argument("--compare", metavar="ORIGINAL")
    parser.add_argument("--cwd", default=".")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    converted = transcript(args.thread, args.text, args.cwd, args.timeout)
    print("\n".join(converted))
    if args.compare:
        original = transcript(args.compare, args.text, args.cwd, args.timeout)
        expected = normalize(original)
        if normalize(converted)[:len(expected)] != expected:
            print("--- original transcript differs:\n" + "\n".join(original), file=sys.stderr)
            return 1
        print(f"OK: transcript of {args.thread} begins with that of {args.compare} ({len(expected)} lines)")
    else:
        print(f"OK: {args.text!r} visible in {args.thread}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
