from __future__ import annotations

import re


MAKE_MKV_PROGRESS_RE = re.compile(r"MakeMKV progress:\s*([0-9]+\.[0-9]+)%")
MAKEMKV_CURRENT_PROGRESS_RE = re.compile(r"Current progress\s*-\s*([0-9]{1,3})%")
MAKEMKV_TOTAL_PROGRESS_RE = re.compile(r"Total progress\s*-\s*([0-9]{1,3})%")
MAKEMKV_OPERATION_RE = re.compile(r"^Current operation:\s*(.+)$")
MAKEMKV_ACTION_RE = re.compile(r"^Current action:\s*(.+)$")

HB_PROGRESS_RE = re.compile(r"Encoding:.*?\s*([0-9]{1,3}(?:\.[0-9]+)?)\s*%")
HB_TASK_RE = re.compile(r"^\[[0-9]{2}:[0-9]{2}:[0-9]{2}\]\s*Starting Task:\s*(.+)$")

HB_START_RE = re.compile(r"^HandBrake start:\s*(\d+)\s*/\s*(\d+):\s*(.+)$")
HB_DONE_RE = re.compile(r"^HandBrake done:\s*(\d+)\s*/\s*(\d+):\s*(.+)$")
SUBTITLE_START_RE = re.compile(r"^Subtitle extraction start:\s*(.+?)\s*\((\d+)\s+streams\)$")
SUBTITLE_PROGRESS_RE = re.compile(r"^Subtitle extraction progress:\s*(\d+)\s*/\s*(\d+):\s*(.+)$")
SUBTITLE_DONE_RE = re.compile(r"^Subtitle extraction done:\s*(.+?)\s*\((.+)\)$")

PROMPT_INSERT_RE = re.compile(r"Insert: ")
PROMPT_NEXT_DISC_RE = re.compile(r"When the next disc is inserted, press Enter to start ripping\.\.\.")
PROMPT_LOW_DISK_RE = re.compile(r"^Low disk space:")
FINALIZING_RE = re.compile(r"^Finalizing: ")
CSV_LOADED_RE = re.compile(r"^CSV schedule loaded:\s*(\d+)\s*discs")
ERROR_RE = re.compile(r"^ERROR:")
MAKEMKV_ACCESS_ERROR_RE = re.compile(r"Failed to get full access to drive")
FALLBACK_STATUS_RE = re.compile(r"^Fallback:\s*(.+)$")
