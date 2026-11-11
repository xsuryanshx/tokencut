"""Shared helpers for the tokencut plugin: config, cheap-model worker calls, logging.

Stdlib only so it runs on any machine with python3.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HOME = os.environ.get("TOKENCUT_HOME") or os.path.expanduser("~/.claude/tokencut")
LOG_PATH = os.path.join(HOME, "log.jsonl")

DEFAULT_MIN_LINES = 350
CHARS_PER_TOKEN = 4  # rough estimate, good enough for reporting

# Max characters sent to the worker in one call (roughly 75% of its context window).
MAX_CHARS = {"haiku": 520_000, "gemini": 3_000_000}


def min_lines():
    try:
        return int(os.environ.get("TOKENCUT_MIN_LINES", DEFAULT_MIN_LINES))
    except ValueError:
        return DEFAULT_MIN_LINES


def backend():
    b = os.environ.get("TOKENCUT_BACKEND", "haiku").strip().lower()
    return b if b in MAX_CHARS else "haiku"


def disabled():
    return os.environ.get("TOKENCUT_DISABLE") == "1" or os.environ.get("TOKENCUT_WORKER") == "1"


def log(event):
    try:
        os.makedirs(HOME, exist_ok=True)
        event = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **event}
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


def is_binary(path):
    try:
        with open(path, "rb") as f:
            return b"\0" in f.read(8192)
    except OSError:
        return True


def count_lines(path):
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
    return n


class WorkerError(RuntimeError):
    pass


def run_worker(system, user, model=None, timeout=600):
    """Send `user` content to the cheap worker model and return (text, meta)."""
    b = backend()
    if len(user) > MAX_CHARS[b]:
        raise WorkerError(
            f"input is {len(user):,} chars, over the {b} limit of {MAX_CHARS[b]:,}. "
            "Split it into smaller batches or narrow the paths."
        )
    # One-shot calls never reuse a cache, so skip the 1.25x cache-write premium.
    env = {**os.environ, "TOKENCUT_WORKER": "1", "DISABLE_PROMPT_CACHING": "1"}
    # Run outside the project so the worker doesn't auto-load the project's CLAUDE.md.
    cwd = tempfile.gettempdir()

    if b == "gemini":
        exe = shutil.which("gemini")
        if not exe:
            raise WorkerError("TOKENCUT_BACKEND=gemini but the `gemini` CLI is not on PATH")
        cmd = [exe, "-m", model or os.environ.get("TOKENCUT_GEMINI_MODEL", "gemini-2.5-flash"),
               "-p", system + "\n\nWork only from the content provided above. Do not use tools."]
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True,
                              timeout=timeout, env=env, cwd=cwd)
        if proc.returncode != 0:
            raise WorkerError(f"gemini exited {proc.returncode}: {proc.stderr.strip()[-500:]}")
        return proc.stdout.strip(), {"backend": "gemini"}

    exe = os.environ.get("TOKENCUT_CLAUDE_BIN") or shutil.which("claude")
    if not exe:
        raise WorkerError("`claude` CLI not found on PATH (set TOKENCUT_CLAUDE_BIN)")
    cmd = [exe, "-p",
           "--model", model or os.environ.get("TOKENCUT_HAIKU_MODEL", "haiku"),
           "--tools", "",
           "--system-prompt", system,
           "--strict-mcp-config",
           "--no-session-persistence",
           "--output-format", "json"]
    proc = subprocess.run(cmd, input=user, capture_output=True, text=True,
                          timeout=timeout, env=env, cwd=cwd)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise WorkerError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[-500:]}")
    if data.get("is_error"):
        raise WorkerError(f"worker error: {data.get('result')}")
    usage = data.get("usage") or {}
    return (data.get("result") or "").strip(), {
        "backend": "haiku",
        "worker_cost_usd": data.get("total_cost_usd"),
        "worker_input_tokens": usage.get("input_tokens"),
        "worker_output_tokens": usage.get("output_tokens"),
    }


def die(msg, code=1):
    print(f"tokencut: {msg}", file=sys.stderr)
    sys.exit(code)
