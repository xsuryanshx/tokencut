#!/usr/bin/env python3
"""PreToolUse guard: stop whole-file dumps of large files into the main context.

Blocks (once per file per session) a full Read, or a cat/less/head -n 9999 style Bash
command, on text files over TOKENCUT_MIN_LINES lines, and tells Claude to use bulk-read
or a targeted Read instead. Retrying the same read is allowed, so Claude can still get
the verbatim file when it truly needs it. Fails open on any error.
"""
import json
import os
import re
import shlex
import sys

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_ROOT, "lib"))
import tokencut as tc  # noqa: E402

BULK_READ = os.path.join(PLUGIN_ROOT, "bin", "bulk-read")
SKIP_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg", ".pdf", ".ipynb"}
DUMP_CMDS = {"cat", "bat", "batcat", "nl", "less", "more", "tac"}
HEAD_TAIL = {"head", "tail"}
EXEMPT_DIRS = [os.path.realpath(os.path.expanduser("~/.claude")), PLUGIN_ROOT]


def big_file(path, cwd):
    """Return line count if `path` is a large text file worth guarding, else None."""
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    path = os.path.realpath(path)
    if not os.path.isfile(path) or os.path.splitext(path)[1].lower() in SKIP_EXT:
        return None
    if any(path.startswith(d + os.sep) for d in EXEMPT_DIRS):
        return None
    if tc.is_binary(path):
        return None
    n = tc.count_lines(path)
    return (path, n) if n >= tc.min_lines() else None


def check_read(ti, cwd):
    limit = ti.get("limit")
    if isinstance(limit, int) and limit <= tc.min_lines():
        return []
    hit = big_file(ti.get("file_path", ""), cwd)
    return [hit] if hit else []


def check_bash(ti, cwd):
    cmd = ti.get("command", "")
    hits = []
    # Pipes into grep/head/wc etc. are already filtering; redirects are copies, not reads.
    for seg in re.split(r"&&|\|\||;|\n", cmd):
        if "|" in seg or ">" in seg or "<<" in seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        while toks and re.match(r"^\w+=", toks[0]):  # leading VAR=value
            toks.pop(0)
        if not toks:
            continue
        prog = os.path.basename(toks[0])
        args = toks[1:]
        if prog in HEAD_TAIL:
            n = None
            for i, a in enumerate(args):
                if a == "-n" and i + 1 < len(args):
                    n = args[i + 1]
                elif re.match(r"^-n?\d+$", a):
                    n = a.lstrip("-n")
            if n is None or not n.lstrip("+").isdigit() or int(n.lstrip("+")) <= tc.min_lines():
                continue
        elif prog not in DUMP_CMDS:
            continue
        skip_next = False
        for a in args:
            if skip_next:
                skip_next = False
                continue
            if a == "-n" and prog in HEAD_TAIL:
                skip_next = True
                continue
            if a.startswith("-"):
                continue
            hit = big_file(a, cwd)
            if hit:
                hits.append(hit)
    return hits


def show(path, cwd):
    rel = os.path.relpath(path, cwd)
    return path if rel.startswith("..") else rel


def state_path(session_id):
    safe = re.sub(r"[^\w-]", "_", session_id or "nosession")
    return os.path.join(tc.HOME, "sessions", safe + ".json")


def main():
    if tc.disabled():
        return
    data = json.load(sys.stdin)
    tool, ti = data.get("tool_name"), data.get("tool_input") or {}
    cwd = data.get("cwd") or os.getcwd()
    hits = check_read(ti, cwd) if tool == "Read" else check_bash(ti, cwd) if tool == "Bash" else []
    if not hits:
        return

    sp = state_path(data.get("session_id"))
    try:
        with open(sp) as f:
            denied = set(json.load(f))
    except (OSError, ValueError):
        denied = set()
    new = [(p, n) for p, n in hits if p not in denied]
    if not new:
        tc.log({"tool": "hook", "action": "override", "via": tool, "paths": [p for p, _ in hits]})
        return

    os.makedirs(os.path.dirname(sp), exist_ok=True)
    with open(sp, "w") as f:
        json.dump(sorted(denied | {p for p, _ in new}), f)
    tc.log({"tool": "hook", "action": "deny", "via": tool, "paths": [p for p, _ in new],
            "lines": sum(n for _, n in new)})

    desc = ", ".join(f"{show(p, cwd)} ({n:,} lines)" for p, n in new)
    rels = " ".join(shlex.quote(show(p, cwd)) for p, _ in new)
    lim = tc.min_lines()
    reason = (
        f"tokencut: blocked a full read of {desc}. Dumping large files into the main context is "
        f"expensive and they stay there for every later turn. Pick one:\n"
        f"1. Have a question about it? Run:  {BULK_READ} -q \"<specific question>\" {rels}\n"
        f"   A cheap model reads it and returns only the answer, with path:line refs. Batch several files in one call.\n"
        f"2. Need one spot (e.g. to Edit)? Grep -n for it, then Read with offset/limit (limit <= {lim}).\n"
        f"3. Really need the verbatim whole file (e.g. a full rewrite)? Repeat the exact same call; it is allowed on retry."
    )
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)  # never break the user's session because of the guard
