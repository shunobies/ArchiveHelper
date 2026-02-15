from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


def trim_ws(s: str) -> str:
    return s.strip()


def is_bool_yn(s: str) -> bool:
    return s.strip().lower() in {"y", "n", "yes", "no", "true", "false"}


def normalize_bool_yn(s: str) -> str:
    v = s.strip().lower()
    if v in {"y", "yes", "true"}:
        return "y"
    if v in {"n", "no", "false"}:
        return "n"
    return ""


@dataclass
class ScheduleRow:
    kind: str  # "movie" or "series"
    name: str
    year: str
    third: str  # MultiDisc y/n OR season integer string
    disc: int
    line: int


def load_csv_schedule(file: Path) -> list[ScheduleRow]:
    rows: list[ScheduleRow] = []

    for n, raw in enumerate(file.read_text(errors="ignore").splitlines(), start=1):
        line = raw.rstrip("\r")
        if n == 1:
            # Accept UTF-8 BOM-prefixed files exported by spreadsheet editors.
            line = line.lstrip("\ufeff")
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            continue

        # Skip common header rows.
        if re.match(r"^\s*(movie|series)?\s*name\s*,\s*year\s*,", line, flags=re.I):
            continue

        parts = [trim_ws(p) for p in line.split(",")]
        if len(parts) != 4 or any(p == "" for p in parts[:4]):
            raise RuntimeError(
                f"CSV parse error at line {n}: expected exactly 4 comma-separated columns\n  Line: {line}"
            )

        name, year, third, disc_s = parts

        if not re.fullmatch(r"\d{4}", year):
            raise RuntimeError(f"CSV validation error at line {n}: year must be 4 digits\n  Line: {line}")

        if not disc_s.isdigit() or int(disc_s) < 1:
            raise RuntimeError(
                f"CSV validation error at line {n}: disc must be an integer >= 1\n  Line: {line}"
            )
        disc = int(disc_s)

        if is_bool_yn(third):
            kind = "movie"
            third_n = normalize_bool_yn(third)
            if not third_n:
                raise RuntimeError(
                    f"CSV validation error at line {n}: MultiDisc must be y/n\n  Line: {line}"
                )
            third = third_n
        else:
            kind = "series"
            if not third.isdigit() or int(third) < 1:
                raise RuntimeError(
                    f"CSV validation error at line {n}: season must be an integer >= 1\n  Line: {line}"
                )

        rows.append(ScheduleRow(kind=kind, name=name, year=year, third=third, disc=disc, line=n))

    if not rows:
        raise RuntimeError(f"CSV schedule is empty: {file}")

    return rows


def csv_disc_prompt_for_row(r: ScheduleRow) -> str:
    if r.kind == "movie":
        if r.third == "y":
            return f"Insert: Movie '{r.name} ({r.year})' Disc {r.disc} (MultiDisc=y). Press Enter when ready."
        return f"Insert: Movie '{r.name} ({r.year})' Disc {r.disc}. Press Enter when ready."
    return f"Insert: Series '{r.name} ({r.year})' Season {r.third} Disc {r.disc}. Press Enter when ready."


def csv_next_up_note(next_row: ScheduleRow) -> None:
    print(f"Next up: {csv_disc_prompt_for_row(next_row)}")
