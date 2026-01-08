
# Copilot instructions (ArchiveHelper)

This repo is a Python CLI + Tkinter GUI tool for ripping discs with MakeMKV and encoding with HandBrake, usually via SSH to a remote rip host.

## Scope and priorities

- Keep changes minimal and targeted to the user request.
- Prefer reliability and safety over cleverness (especially around deletion and remote execution).
- Preserve backwards compatibility with prior “v2” naming where it matters (logs, markers), unless the user explicitly opts out.

## Project layout

- CLI entrypoint: `rip_and_encode.py`
- GUI entrypoint: `rip_and_encode_gui.py`

Do not reintroduce “v2” filenames as primary names. If compatibility is needed, treat it as fallback only.

## Safety rails (must not regress)

### Cleanup behavior

- Cleanup deletes per-title *work directories* under `$HOME` (the temporary staging area), not the final Jellyfin Movies/Series storage directories.
- Workdir deletion must remain guarded:
	- Only consider candidates exactly one level under `$HOME`.
	- Require a marker file (new: `.rip_and_encode_workdir`, legacy: `.rip_and_encode_v2_workdir`) OR a conservative legacy hint.
	- Always exclude configured local Movies/Series directories and anything under them.
- If cleanup fails to delete a candidate directory, return a non-zero exit code (GUI should surface the failure).

### Title/Path sanitization

- Directory names should be Linux-safe (no quoting required for typical shell usage). Keep the conservative allowlist approach.

## Logging conventions

- New log naming: `rip_and_encode_*.log`
- Legacy log naming: `rip_and_encode_v2_*.log` (still supported for discovery/rotation)
- Default remote log dir: `$HOME/.archive_helper_for_jellyfin/logs/`

If you change any log patterns, update:
- CLI log rotation + naming
- GUI remote log discovery logic
- Any docs/tooltips that reference log names

## Remote execution model (GUI)

- The GUI typically:
	- Validates connection fields
	- Uploads the CLI script to `~/.archive_helper_for_jellyfin/` (prefer `rip_and_encode.py`, optionally fall back to `rip_and_encode_v2.py` if present locally)
	- Uploads a generated CSV schedule to `/tmp/`
	- Starts the run inside a detached `screen` session
	- Tails the remote log and parses progress from it

Guidelines:
- Don’t add new remote dependencies unless requested.
- Keep both OpenSSH and Paramiko code paths working.
- Stop must always leave the GUI in a recoverable idle state.

## OO/maintainability guidance

- Prefer small refactors with mechanical, verifiable improvements.
- Keep per-run state consolidated (e.g., `RunContext`), and avoid reintroducing many `self.run_*` fields.
- Avoid large rewrites of parsing logic unless you add a clear, simple test path (the GUI supports replaying a local log).

## Validation workflow

- At minimum, run: `python3 -m py_compile rip_and_encode.py rip_and_encode_gui.py`
- If changes affect CLI flags or help text, sanity-check `python3 rip_and_encode.py --help`.

## Git hygiene

- After a successful, meaningful change, create a git commit with a clear message.
- Avoid drive-by formatting changes.

# Documentation & Code Comment Writing Instructions

## Audience
Write for a **middle school–level reader (grades 6–8)**.  
Assume the reader is smart, curious, and new to the topic—but not an expert.

The reader should be able to say after one read:
> “Oh. That makes sense.”

---

## Voice & Style (Daniel Suarez–Inspired)
Write with the clarity and confidence of **Daniel Suarez explaining how a real system works**.

- Calm, precise, and serious (never childish)
- Focused on **systems**, **cause and effect**, and **how things actually work**
- Slightly cinematic when helpful, but always practical
- No hype, no marketing language, no slang

Think:
> “Here’s what this does, how it works behind the scenes, and why it matters.”

---

## Readability Rules
- Use **short sentences** (prefer under 20 words)
- Keep **paragraphs short** (1 idea per paragraph)
- Use **simple, everyday language**
- Avoid jargon; if you must use it, **define it immediately**
- Explain acronyms on first use
- Prefer bullet points over dense paragraphs

If something feels dense, split it.

---

## Explanation Structure (Use This Pattern)
For each concept, function, class, or module:

1. **What it is** (plain definition)
2. **Why it exists** (the problem it solves)
3. **Simple example** (easy, familiar scenario)
4. **Real or technical example** (how it’s actually used)
5. **Plain-language summary**

---

## Examples Are Required
- Every important idea **must include at least one example**
- Complex ideas should include:
  - One **simple example** first
  - One **more realistic example** second
- Use familiar analogies:
  - Locks and keys
  - Roads and traffic
  - Messages and mailboxes
  - Machines, tools, games, or rules

Examples should clarify, not impress.

---

## Code Comments Guidelines
Code comments should explain **intent and behavior**, not restate the code.

Good comments answer:
- What is this code responsible for?
- Why does it exist?
- What problem does it prevent or solve?
- What would break if it were removed?

Prefer:
- Clear, complete sentences
- Comments above blocks of logic
- Explanations of *why*, not just *what*

Avoid:
- Obvious comments
- Jargon without explanation
- Humor or sarcasm

---

## Documentation Goals
Documentation should:
- Teach, not assume
- Move step by step
- Make invisible systems understandable
- Prioritize clarity over cleverness

When finished, ask:
> “Would a curious 12-year-old understand this without asking questions?”

If not, rewrite.

---