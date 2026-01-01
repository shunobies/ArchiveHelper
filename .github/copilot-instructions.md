
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

