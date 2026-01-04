
# Features / TODO

## Dual-Mode Operation: Rip Remote (existing) + Rip Local (new)

Goal: Support two user workflows:

1) **Rip Remote (existing)**: GUI controls a remote host over SSH; ripping/encoding happens on the server; GUI tails remote logs and responds to prompts.
2) **Rip Local (new)**: ripping (and optionally encoding) happens on the local machine; when completed, outputs are uploaded to the server directories (Movies/Series/etc.) in the correct structure.

### Open questions (resolve before implementation)

- Local mode scope:
	- Is local mode **rip-only** (produce MKVs locally, then upload MKVs for server-side encode), or **rip+encode locally** (upload final MP4s)?
	- If “rip-only”, do we also want a follow-up remote command to start HandBrake on the server after upload?
- Disc prompting in local mode:
	- Should the GUI still show the same “Insert disc / Continue” prompts when driving local MakeMKV?
	- Should “Continue” control a local process stdin, similar to the legacy direct-stdin path?
- Storage expectations:
	- Where should local workdirs live (per-title temp under $HOME / %USERPROFILE%)?
	- Do we support local output to external drives?

### UX requirements

- Startup prompt:
	- On GUI initialization, show a modal choice: **Rip on Server (Remote)** vs **Rip on This Machine (Local)**.
	- Remember selection (persisted setting). Provide “Don’t ask again” or just auto-use last choice.
- Settings:
	- Add Settings menu option to switch modes: **Settings → Mode → Rip Remote / Rip Local**.
	- Switching modes should be blocked while a run is active.
	- When mode changes, update labels/tooltips to reduce confusion (e.g., “Connection settings” only required for remote mode, but upload target still needed for local mode).

### Architecture / code organization

- Introduce an explicit mode enum/flag in persisted settings (e.g., `mode = remote|local`).
- Keep current remote pipeline unchanged as the default path.
- Add a “runner” abstraction so the GUI can start a job and stream logs from either:
	- remote job (existing: screen + tail over SSH)
	- local job (new: subprocess + local log file)

Proposed modules (minimal changes, avoid large rewrites):

- `archive_helper_gui/modes.py`: mode constants, persistence helpers, UI labels.
- `archive_helper_gui/local_runner.py`: start local script, manage stdin (Enter), stream/tail local log.
- `archive_helper_gui/uploader.py`: upload completed outputs to server (SFTP via Paramiko when available; fallback to `scp` if present).

### Local mode pipeline (high-level)

1) Build schedule (manual or CSV) exactly as today.
2) Run a local script that can:
	 - rip (MakeMKV) locally
	 - (optional) encode (HandBrakeCLI) locally
	 - write logs in the same format the GUI parser already understands, so the UI works with minimal parser changes.
3) Upload outputs to server:
	 - Movies → Movies directory
	 - Series → Series directory
	 - (Future: Books/Music)
4) Post-upload validation:
	 - confirm remote file exists and sizes are non-zero
	 - optionally verify by checksum for small files (keep optional to avoid long runs)
5) Local cleanup:
	 - delete only local workdirs with marker files (mirror remote cleanup safety approach)

### Cross-platform considerations (Windows / Linux / macOS)

- Tool availability:
	- MakeMKV:
		- Windows: `makemkvcon64.exe` path detection
		- macOS/Linux: `makemkvcon` in PATH
	- HandBrakeCLI:
		- Windows: `HandBrakeCLI.exe`
		- macOS/Linux: `HandBrakeCLI` in PATH
- Eject / tray control:
	- Windows: likely no standard `eject`; treat as best-effort/no-op.
	- Linux/macOS: `eject` exists sometimes; keep best-effort with clear logging.
- Drive device selection:
	- Windows: drive letter / MakeMKV drive index detection
	- macOS/Linux: `/dev/sr0` or similar; avoid hard-coding; prefer MakeMKV scan results.
- Paths:
	- Use `pathlib` and avoid assuming `/tmp`.
	- Ensure uploads preserve filenames and handle spaces safely.

### Logging + parsing

- Keep log lines consistent with existing regex patterns so:
	- total progress drives ETA
	- prompts drive Continue
	- error detection remains reliable
- If local tooling emits different progress formats, normalize in local runner to match current patterns.

### Implementation checklist (sequenced)

1) Persistence
	- Add persisted setting for rip mode.
	- Add migration default to “remote”.
2) Startup modal
	- Show mode picker on startup when no preference yet.
	- Store preference and apply immediately.
3) Settings integration
	- Add Settings → Mode to switch.
	- Ensure disabled while running.
4) Local runner (no upload yet)
	- Run a local process and tail a local log.
	- Verify GUI parser works with replay/log tail.
	- Ensure Continue sends Enter to the local process when prompted.
5) Upload-only prototype
	- Implement “upload outputs to server” function and test with a small file.
	- Prefer Paramiko SFTP when available; else OpenSSH scp.
6) Local pipeline end-to-end
	- After local job completes, upload outputs to the configured remote Movies/Series dirs.
	- Handle partial failures: retry upload, show errors, leave local files intact on failure.
7) Safety rails
	- Confirm we never delete server storage dirs.
	- Local cleanup guarded by marker file(s).
8) UX polish
	- Status text clearly indicates “Local run” vs “Remote run”.
	- Connection dialog wording adjusted in local mode (still required for upload target).

### Manual test checklist (with you)

General (all OS):

- Remote mode regression:
	- Start remote run, reattach, Continue prompt, Stop, Cleanup.
- Mode switching:
	- Switch mode idle; confirm UI changes; confirm blocked while running.

Windows:

- Detect local MakeMKV and start rip.
- Confirm log parsing updates progress and ETA.
- Confirm “Continue” interacts correctly with local prompts.
- Confirm upload places files into the correct server directories.

Linux:

- Confirm local MakeMKV + HandBrakeCLI paths.
- Confirm eject best-effort doesn’t error hard.
- Confirm upload via scp works when Paramiko absent.

macOS:

- Confirm app finds CLI tools in PATH (GUI-launched apps often have a different PATH).
- Confirm upload and path handling.

