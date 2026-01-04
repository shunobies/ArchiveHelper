
# Features / TODO

## Dual-Mode Operation: Rip Remote (existing) + Rip Local (new)

## Current status (as of 2026-01-04)

- Completed:
	- Mode selector (3 modes) in the GUI with persistence + Settings entry.
	- **Local rip-only, encode on server**: rip locally with MakeMKV, upload MKVs to server workdir, then start remote encode with `--no-disc-prompts`.
	- Server support: `rip_and_encode.py` has `--no-disc-prompts` to skip between-disc `input()` pauses in CSV mode.
- Not completed yet:
	- Configurable local destination path (currently uses a fixed local staging location).
	- Local disk-space checks during local ripping.
	- Local cleanup (delete local workdirs safely) and any UI for it.
	- **Rip + encode locally, upload results**.

Where we left off: local rip-only is working end-to-end, but the local staging directory is not user-configurable yet and there are no local disk-space guardrails/cleanup tooling.

Goal: Support two user workflows:

1) **Rip Remote (existing)**: GUI controls a remote host over SSH; ripping/encoding happens on the server; GUI tails remote logs and responds to prompts.
2) **Rip Local (new)**: ripping (and optionally encoding) happens on the local machine; when completed, outputs are uploaded to the server directories (Movies/Series/etc.) in the correct structure.

### Open questions (resolve before implementation)

- Local mode scope:
	- Rip-only (MKVs locally, encode on server) is implemented.
	- Rip + encode locally (upload MP4s) is still pending.
- Storage expectations (NEW):
	- Add a **configurable local destination path** in Settings so the app has a managed local workspace.
	- Add **local disk-space checks** during a local run to prevent filling the drive.

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

Note: local rip-only was implemented directly in `rip_and_encode_gui.py` (minimal change / avoid large rewrite). We can still refactor into modules later if desired.

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
	 - **NEW**: local workspace should be user-configurable and managed (safe cleanup + guardrails)

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

Done:

1) Persistence
	- Persisted setting for rip mode (default remote).
2) Startup modal
	- Mode picker shown on startup when no preference yet.
3) Settings integration
	- Settings entry to switch modes; blocked while running.
4) Local runner + upload + handoff
	- Local MakeMKV ripping via GUI, Continue prompts.
	- Upload MKVs to server workdir.
	- Start server-side encode pass with `--no-disc-prompts`.

Next:

5) Settings: configurable local destination (NEW)
	- Add a “Local destination” path input when a local rip mode is selected.
	- Use it for local staging/workdirs.
	- Store/persist it.
6) Local disk-space checks (NEW)
	- Before starting each disc rip, ensure local destination drive has >= X GB free.
	- Periodically check while ripping and warn/pause if below threshold.
7) Local cleanup (NEW)
	- Mirror remote cleanup safety rails: marker file + only delete within the configured local destination.
	- Add a UI action to cleanup local staging.
8) Local rip+encode mode
	- Encode locally (HandBrakeCLI), upload MP4 results.
	- Decide whether to upload into Movies/Series dirs directly or stage then remote move.

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

