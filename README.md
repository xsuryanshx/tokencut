# tokencut

**An open-source version of Spotify's "shunt" setup, which cut their Claude Code token usage by about 90%. It works with any Claude Code install.**

In September 2026 Spotify Engineering published [*Portal by Spotify cut my Claude Code token usage by 90%*](https://engineering.atspotify.com/2026/9/portal-by-spotify-cut-my-claude-code-token-usage-by-90). Their key observation: most of what a coding agent does isn't reasoning. It's reading large files and writing predictable code, so they routed that work to a cheaper model.

Their implementation depends on **Portal**, Spotify's internal developer platform, so nobody outside Spotify can run it. tokencut copies the same design and swaps Portal for tools you already have:

- **Haiku**, run headless through `claude -p`. This is the default: same login, no extra API keys.
- **Gemini 2.5 Flash** through the `gemini` CLI, which is the model Spotify used. Optional.

---

## Contents

- [The problem](#the-problem)
- [What Spotify built](#what-spotify-built)
- [How tokencut mimics it](#how-tokencut-mimics-it)
- [How each piece works](#how-each-piece-works)
- [Install](#install)
- [Usage](#usage)
- [Configuration](#configuration)
- [Measured results](#measured-results)
- [Differences from Spotify's version](#differences-from-spotifys-version)
- [Limitations](#limitations)
- [Other ways to cut Claude Code tokens](#other-ways-to-cut-claude-code-tokens)
- [Repo layout and tests](#repo-layout-and-tests)
- [Sources](#sources)

---

## The problem

When Claude Code `Read`s a 2,000-line file, the whole file goes into the main conversation. Claude Code re-sends that conversation on every later turn, so the file is paid for again and again. Prompt caching makes the repeats cheaper, but they still count toward your usage. With a frontier model like Opus or Sonnet, most of your spend can go to holding files the model only needed one fact from.

Two kinds of work cause most of this:

1. **Bulk reading:** "where is retry logic configured?" means opening several large files to find about 20 relevant lines.
2. **Predictable writing:** unit tests, configs, stubs and fixtures. The main model writes them, and the generated code then stays in context.

## What Spotify built

From the article, Spotify's setup has **three layers** plus **two worker "modes"** hosted on Portal:

| Spotify layer | What it does |
|---|---|
| **Hooks** (Claude Code `PreToolUse`) | `check-file-size` blocks `Read`s of files over 350 lines (configurable via `SHUNT_MIN_LINES`). `check-bash-read` catches `cat`/shell reads of large files. Both point Claude to the bulk reader. |
| **Scripts** | `bulk-read` wraps files in XML tags and sends them with a question to the *bulk-reader* mode. `code-write` sends a spec plus reference files to the *code-writer* mode, strips markdown fences, and writes the result **straight to disk, so Claude never sees it**. |
| **Skills** | Markdown docs that tell Claude when and how to call the scripts, as a fallback if the hooks miss something. |

| Spotify mode (Portal "AiKA Mode") | Model | Behavior |
|---|---|---|
| **bulk-reader** | Gemini 2.5 Flash, temperature 0.2 | Precise code analysis. Answers in structured bullets only. |
| **code-writer** | Gemini 2.5 Flash | Matches existing patterns for tests and boilerplate. Outputs only code: no explanations, no markdown fences. |

They packaged this as a Claude Code plugin called **shunt** in the `spotify/portal-ai-plugins` marketplace. It needs a Portal instance and Portal auth, so it doesn't work outside Spotify. They report a **mean ~90% token saving on the delegated bulk-read work**, measured on a Java monorepo.

## How tokencut mimics it

Each component of tokencut matches one piece of Spotify's setup:

| Spotify (shunt + Portal) | tokencut | Notes |
|---|---|---|
| `check-file-size` hook | `hooks/guard.py` (`Read` branch) | Same 350-line default threshold |
| `check-bash-read` hook | `hooks/guard.py` (`Bash` branch) | Catches `cat`, `bat`, `nl`, `less`, `more`, `tac`, and `head`/`tail -n N` with N over the threshold, including inside `cd x && cat y`. Pipes (`cat f \| grep`) and redirects are allowed because they already filter |
| `SHUNT_MIN_LINES` | `TOKENCUT_MIN_LINES` | |
| `bulk-read` script | `bin/bulk-read` | Same XML-wrapped files plus a question. Also accepts `--paths`, like Spotify's CLI |
| bulk-reader mode (Gemini Flash, structured bullets) | system prompt in `bin/bulk-read`, run on **Haiku** (default) or **Gemini Flash** | Adds line numbers to the input so answers cite `path:line` |
| `code-write` script | `bin/code-write` | Same: spec + references → strip fences → write to disk, code not shown to Claude |
| code-writer mode (code only, no fences) | system prompt in `bin/code-write` | |
| Portal (hosted, ephemeral, no API keys) | `claude -p --tools "" --system-prompt …` in a temp dir | A disposable, tool-less, single-shot worker on your existing Claude login |
| Skill docs | `skills/tokencut/SKILL.md` | Loaded only when relevant |
| Plugin marketplace | `.claude-plugin/marketplace.json` | Install with one command |
| — | `bin/tokencut-stats` | Extra: shows calls, blocks and estimated tokens avoided |

### Data flow

```
Claude (Opus/Sonnet) ──Read big.java──▶ PreToolUse hook (guard.py)
                                            │  ≥ 350 lines, first time this session?
                                            ▼
                                       deny + "run bulk-read -q '...' big.java"
Claude ──Bash: bulk-read -q "Where is retry configured?" big.java a.java──▶ bin/bulk-read
                                            │  wrap files in <file path=…> with line numbers
                                            ▼
                               worker: claude -p --model haiku --tools ""   (or gemini -m gemini-2.5-flash)
                                            │  reads ~35k tokens in its own throwaway context
                                            ▼
Claude ◀── ~300 tokens: bullets with path:line refs ──┘
```

The main model then reads only the exact lines it needs, with `Read` offset/limit, before it edits.

## How each piece works

### 1. The hook: `hooks/guard.py`

Registered in `hooks/hooks.json` as a `PreToolUse` hook on `Read|Bash`. On each call it:

1. Resolves the target path and skips binaries, images, PDFs, notebooks, and anything under `~/.claude` or the plugin itself.
2. Counts lines. Below `TOKENCUT_MIN_LINES`, or a `Read` with `limit ≤ threshold`, passes straight through.
3. If the file is large and **hasn't been blocked yet this session**, it returns `permissionDecision: "deny"` with a reason giving Claude three options:
   - `bulk-read -q "<question>" <file>` (the exact command, ready to run)
   - Grep, then `Read` with offset/limit
   - repeat the same call if it really needs the whole file verbatim
4. Records the block in `~/.claude/tokencut/sessions/<session>.json`. A retry of the same file goes through and is logged as an `override`.

**Fails open:** if the hook errors for any reason, the tool call goes ahead, so the guard can never break a session.

### 2. `bin/bulk-read`

- Accepts files, directories (walked recursively, skipping `.git`, `node_modules`, `venv`, `dist`, `build`, etc. and binary files), or `--stdin` for logs and command output.
- Prefixes every line with its line number and wraps each file in `<file path="...">`.
- Sends everything with the question to the worker under a strict system prompt: answer only the question, structured bullets, cite `path:line` for every claim, short quotes only.
- Prints only the worker's answer. Logs its size and cost to `~/.claude/tokencut/log.jsonl`.
- Refuses inputs that would overflow the worker's context, and tells Claude to split the batch.

### 3. `bin/code-write`

- Takes `--spec`, one or more `--reference` files (an example to imitate plus the code under test), and `--target`.
- The worker returns only the file contents. tokencut strips stray markdown fences and writes the file.
- Prints a single line, `wrote X (N lines)`, so the generated code never enters the main context. Claude then verifies by running the tests or linter and reads only what fails.
- Won't overwrite an existing file without `--force`.

### 4. The worker call: `lib/tokencut.py`

The Haiku backend runs:

```
claude -p --model haiku --tools "" --system-prompt <mode prompt> \
       --strict-mcp-config --no-session-persistence --output-format json
```

- Runs from a temp directory, so the project's `CLAUDE.md` isn't pulled in.
- Sets `TOKENCUT_WORKER=1`, so the worker's own hooks don't block it.
- Sets `DISABLE_PROMPT_CACHING=1`. One-shot calls never reuse a cache, so a cache write would only add a 1.25× premium. In testing this halved worker cost, from $0.077 to $0.041 per 35k-token read.

The Gemini backend runs `gemini -m gemini-2.5-flash -p <prompt>` with the content on stdin.

### 5. The skill: `skills/tokencut/SKILL.md`

Loaded on demand. It tells Claude when to use `bulk-read` and `code-write`, how to write good questions and specs, and that code-write output must be verified by running it. It's the fallback layer for anything the hook doesn't catch, for example "summarize these 12 medium-sized files".

## Install

Requirements: `python3` (stdlib only) and the `claude` CLI. The `gemini` CLI is optional.

```bash
claude plugin marketplace add xsuryanshx/tokencut
claude plugin install tokencut@tokencut-marketplace
```

Restart Claude Code, then run `/hooks` and check for the `PreToolUse` entry for `Read|Bash`.

**Roll out to a team.** Commit this to a project's `.claude/settings.json`. Teammates are then prompted to install the plugin when they trust the folder:

```json
{
  "extraKnownMarketplaces": {
    "tokencut-marketplace": { "source": { "source": "github", "repo": "xsuryanshx/tokencut" } }
  },
  "enabledPlugins": { "tokencut@tokencut-marketplace": true }
}
```

**Try it without installing:**

```bash
git clone https://github.com/xsuryanshx/tokencut
claude --plugin-dir tokencut/plugins/tokencut
```

## Usage

Normally you don't call these yourself: the hook and skill teach Claude to. Run by hand they look like this:

```bash
# Ask a question across files and directories
bulk-read -q "Where is retry logic configured and what are the defaults?" src/http/ src/config/Retry.java

# Make sense of noisy output
npm test 2>&1 | bulk-read --stdin -q "Which tests failed and what is the root cause of each?"

# Harder cross-file question: use a stronger worker (still far cheaper than the main context)
bulk-read --model sonnet -q "Trace how a request flows from the controller to the DB" src/

# Write boilerplate straight to disk
code-write --spec "JUnit 5 tests for LruCache eviction: evicts least-recently-used at capacity, get() refreshes recency, capacity 0 throws" \
           --reference src/test/java/CacheTest.java --reference src/main/java/LruCache.java \
           --target src/test/java/LruCacheEvictionTest.java

# See what it's saved
tokencut-stats
```

## Configuration

Set these env vars in your shell or in `settings.json` → `"env"`:

| Var | Default | Meaning |
|---|---|---|
| `TOKENCUT_MIN_LINES` | `350` | Files at least this long get guarded |
| `TOKENCUT_BACKEND` | `haiku` | `haiku` or `gemini` |
| `TOKENCUT_HAIKU_MODEL` | `haiku` | Any `claude --model` value |
| `TOKENCUT_GEMINI_MODEL` | `gemini-2.5-flash` | Any `gemini -m` value |
| `TOKENCUT_CLAUDE_BIN` | `claude` on PATH | Path to the claude binary |
| `TOKENCUT_DISABLE` | unset | `1` turns the hook off |
| `TOKENCUT_HOME` | `~/.claude/tokencut` | Where the log and session state live |

## Measured results

All on one machine, Claude Code 2.1.280:

| Test | Result |
|---|---|
| `bulk-read` on CPython's `argparse.py` (2,662 lines, ~35k tokens): "where are mutually exclusive groups enforced and what error is raised?" | About **300 tokens** returned to the main context instead of about 35k, a **~99% cut** for that read. Line citations (`argparse.py:2006`, `:2249`) checked exact. Haiku cost ≈ **$0.04** |
| Real Sonnet session with the plugin loaded, told to `Read` that file and summarize it | Hook blocked the `Read` → Claude ran `bulk-read` on its own → correct 4-bullet overview |
| `code-write`: pytest suite for a small `slugify()` | 8 tests written to disk; 7 passed, 1 had a wrong expectation. Claude catches that by running the tests, which is why the skill insists on it |
| Same `bulk-read` question on Gemini 2.5 Flash | Faster (~6s vs ~12s) and doesn't use Claude quota, but it missed the main conflict check. That's why Haiku is the default |
| `tests/test_guard.sh` | 19/19 pass |

How much you save overall depends on how read-heavy your work is. Spotify's ~90% was the mean over **delegated bulk reads** in one Java monorepo, not total spend.

## Differences from Spotify's version

- **No Portal.** The worker is a local `claude -p` or `gemini` process instead of a hosted mode. There's no infrastructure to run, but the worker's tokens count against your own Claude plan or API usage (at Haiku rates).
- **Haiku is the default instead of Gemini Flash.** Code stays with the same provider, nothing new needs approval, and answers were more complete in testing. Gemini is one env var away.
- **No temperature control** on the Haiku backend (`claude -p` doesn't expose it). The system prompt is strict instead.
- **Block once, then allow.** The guard blocks each large file only once per session, so Claude is never stuck when it truly needs the whole file. This is a safety valve we added; the article doesn't describe one.
- **Line-numbered input**, so every worker answer can cite `path:line` and the main model can do a targeted `Read` before editing.
- **Extras:** `--stdin` for logs and test output, directory walking, an input-size guard, `tokencut-stats`, and an offline test suite.

## Limitations

- It only cuts **reading and boilerplate** costs. It doesn't help with long conversations, a bloated `CLAUDE.md`, or too many MCP servers. See the next section for those.
- The worker can be wrong. `bulk-read` answers are summaries, so use the `path:line` refs to check before editing. `code-write` output must be run.
- `claude -p` startup adds about 2k tokens and a few seconds per call, so it isn't worth it for small files. That's why the threshold is 350 lines.
- The Bash guard is a best-effort parser. Unusual shell constructs can slip through, but the skill covers those.
- With `TOKENCUT_BACKEND=gemini`, file contents are sent to Google. Check your org's policy.

## Other ways to cut Claude Code tokens

From Anthropic's [cost guide](https://code.claude.com/docs/en/costs) and community write-ups. These stack with tokencut:

- **`/usage`, then press `f`.** Shows what's eating your quota (CLAUDE.md size, MCP servers, effort level) and applies the fixes it can.
- **`/clear` between unrelated tasks.** Stale context is re-sent every turn. `/clear` is free; `/compact` is itself a large request.
- **Match the model to the job.** Use Sonnet by default and save Opus for hard reasoning. Give simple subagents `model: haiku`. Lower `/effort` for routine work.
- **Keep `CLAUDE.md` under ~200 lines.** Move workflow-specific instructions into skills, which load only when used.
- **Trim MCP.** Disable unused servers in `/mcp`, and prefer CLIs (`gh`, `aws`, `gcloud`) over MCP servers when both exist.
- **Code-intelligence (LSP) plugins** for typed languages. One go-to-definition call replaces grep plus several file reads.
- **Filter tool output with hooks**, e.g. rewrite test commands to `| tail -n 100` (example in the cost guide).
- **Delegate noisy work to subagents**, so only their summary returns to the main context.
- **Write specific prompts.** "Add validation to `login()` in `auth.ts`" beats "improve the codebase".
- **Avoid cache misses.** The first message after a long idle re-processes the whole context. Watch for background `/loop`s and scheduled tasks.

## Repo layout and tests

```
.claude-plugin/marketplace.json      # marketplace manifest (lets `claude plugin marketplace add` work)
plugins/tokencut/
├── .claude-plugin/plugin.json       # plugin manifest
├── hooks/hooks.json                 # registers the PreToolUse hook on Read|Bash
├── hooks/guard.py                   # = Spotify's check-file-size + check-bash-read
├── bin/bulk-read                    # = Spotify's bulk-read  (+ bulk-reader mode prompt)
├── bin/code-write                   # = Spotify's code-write (+ code-writer mode prompt)
├── bin/tokencut-stats               # savings report
├── lib/tokencut.py                  # config, worker backends (Haiku / Gemini), logging
└── skills/tokencut/SKILL.md         # = Spotify's skill layer
tests/test_guard.sh                  # offline hook tests (no model calls)
```

```bash
bash tests/test_guard.sh                    # 19 offline tests
claude plugin validate .                    # marketplace manifest
claude plugin validate plugins/tokencut     # plugin manifest
```

## Sources

- Spotify Engineering: [Portal by Spotify cut my Claude Code token usage by 90%](https://engineering.atspotify.com/2026/9/portal-by-spotify-cut-my-claude-code-token-usage-by-90)
- [Techzine: Spotify reduces Claude Code token usage by 90 percent](https://www.techzine.eu/news/devops/144093/spotify-reduces-claude-code-token-usage-by-90-percent/)
- Anthropic: [Manage costs effectively](https://code.claude.com/docs/en/costs) · [Hooks reference](https://code.claude.com/docs/en/hooks) · [Plugins reference](https://code.claude.com/docs/en/plugins-reference)
- [Firecrawl: 12 ways to cut token consumption in Claude Code](https://www.firecrawl.dev/blog/claude-code-token-efficiency)

Not affiliated with Spotify or Anthropic. MIT licensed.
