# Multi-Movie Disc Handling TODO (Planning Draft)

This file is intentionally kept as a **future-planning TODO** for handling discs that contain multiple movies (for example, up to 4 movies on one DVD).

## Why keep this

Some discs contain multiple feature-length titles, and the app needs a workflow that allows selecting and naming each output cleanly from a single inserted disc.

## TODO: Candidate approach

### 1) Detect multiple likely feature titles

After scan, identify candidates using simple heuristics:

- Duration threshold (for example, >45 or >60 minutes)
- Chapter-count threshold
- Size threshold (to exclude tiny extras)
- Duplicate filtering for near-identical playlist/title variants

### 2) Add multi-select UI in scan flow

Allow users to select multiple titles from one disc scan and map metadata per selected title:

- Media type
- Movie title
- Year (optional)

### 3) Queue per-title outputs from one disc session

Generate one output job per selected title while keeping one disc session context.

### 4) Per-title status and fail-safe behavior

Track success/failure per title (not only per disc), and continue processing other selected titles when one title fails.

### 5) Naming/output expectations

Store each movie as a separate final file with explicit title/year naming.

---

## Related implementation preference (from feedback)

For troublesome discs in the ripping pipeline:

- Fallback behavior should be **best-effort by default**.
- If direct MakeMKV reading fails, the workflow should automatically attempt all configured fallback stages.
- If all fallback stages fail, follow a clear fail-safe error path and continue the queue.

(Full fallback implementation details will be tracked in code tasks rather than this planning-only TODO file.)
