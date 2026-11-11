---
name: tokencut
description: Use before reading large files (300+ lines), several files at once, whole directories, long logs or test output, or before writing predictable code (unit tests, configs, stubs, boilerplate, fixtures). Routes that bulk work to a cheap worker model via `bulk-read` and `code-write` so it never fills the main context. Also use when the tokencut hook blocks a Read or Bash call.
---

# TokenCut: send bulk work to a cheap model

Most of an agent's tokens go to *reading*, not reasoning. Anything you Read stays in context and gets re-sent on every later turn. So when the job is "find out what's in this big thing" or "write obvious code", hand it to the worker model and keep only its short answer.

Commands (on PATH while the plugin is enabled; full path: `${CLAUDE_PLUGIN_ROOT}/bin/`):

| Need | Do |
|---|---|
| Understand big file(s) or a directory | `bulk-read -q "<specific question>" path/a path/b dir/` |
| Make sense of long command output | `<cmd> 2>&1 \| bulk-read --stdin -q "Which tests failed and why?"` |
| Write tests / configs / stubs / boilerplate | `code-write --spec "<detailed spec>" --reference <example file> --reference <code under test> --target <new file>` |
| See savings | `tokencut-stats` |

## bulk-read

- Ask **specific** questions that will drive your next action: "Which functions call `retryPolicy()` and with what args?" beats "Explain this file".
- **Batch**: one call with 10 related files is far cheaper than 10 calls.
- Answers cite `path:line`. Use them to Read just the region you need (offset/limit) before an Edit.
- Harder cross-file question? Add `--model sonnet`. Still much cheaper than reading it all into this context.
- Don't use it for small files (< ~150 lines) or when you need exact text to edit. Read those directly.

## code-write

- The output goes straight to disk and is **not shown to you**. That is the point.
- Put everything the worker needs into `--spec`: test cases, names, edge cases, fixtures. Pass the closest existing test as `--reference` so it copies the house style, plus the code under test.
- Then **verify by running** the tests, linter, or type-checker. Read only the failing regions and fix them with Edit.
- Use it only for predictable code. Write tricky logic, security-sensitive code, or anything that needs design judgment yourself.
- Won't overwrite existing files unless you pass `--force`.

## When the hook blocks you

The guard blocks one full read of a large file and suggests these tools. Pick the cheapest option that works:
1. `bulk-read` for a question about it, 2. Grep -n then Read with offset/limit to edit a spot, 3. repeat the exact call only if you truly need the whole verbatim file.

## Other habits that save tokens

- Grep/Glob before Read. Read with offset/limit when you know the region.
- Pipe noisy commands through `tail -n 50`, `grep`, or `bulk-read --stdin` instead of dumping full output.
- Use a subagent (e.g. Explore) for wide multi-step searches, so only its summary returns here.
