from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


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


@dataclass
class ScheduleV2Row:
    disc_id: str
    disc_number: int
    source_title_index: int
    movie_title: str
    year: str
    tmdb_id: Optional[int]
    output_role: str
    line: int


@dataclass
class ParsedSchedule:
    version: int
    rows_v1: list[ScheduleRow]
    rows_v2: list[ScheduleV2Row]


ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')


def normalize_title(raw: str, *, item_label: str) -> str:
    title = " ".join((raw or "").strip().split())
    if not title:
        raise RuntimeError(f"Schedule validation error at {item_label}: movie_title/title is required")
    if ILLEGAL_FILENAME_CHARS_RE.search(title):
        raise RuntimeError(
            f"Schedule validation error at {item_label}: title contains illegal filename characters: {title!r}"
        )
    return title


def normalize_year(raw: str, *, item_label: str) -> str:
    year = str(raw or "").strip()
    if not re.fullmatch(r"\d{4}", year):
        raise RuntimeError(f"Schedule validation error at {item_label}: year must be 4 digits")
    return year


def _normalize_output_role(raw: str, *, item_label: str) -> str:
    role = str(raw or "").strip().lower()
    if role not in {"main", "extra"}:
        raise RuntimeError(f"Schedule validation error at {item_label}: output_role must be 'main' or 'extra'")
    return role


def _parse_disc_number(raw: Any, *, item_label: str) -> int:
    v = "" if raw is None else str(raw).strip()
    if not v.isdigit() or int(v) < 1:
        raise RuntimeError(f"Schedule validation error at {item_label}: disc_number must be an integer >= 1")
    return int(v)


def _parse_source_title_index(raw: Any, *, item_label: str) -> int:
    v = "" if raw is None else str(raw).strip()
    if not v.isdigit() or int(v) < 0:
        raise RuntimeError(f"Schedule validation error at {item_label}: source_title_index must be an integer >= 0")
    return int(v)


def _parse_tmdb_id(raw: Any, *, item_label: str) -> Optional[int]:
    v = "" if raw is None else str(raw).strip()
    if not v:
        return None
    if not v.isdigit() or int(v) < 1:
        raise RuntimeError(f"Schedule validation error at {item_label}: tmdb_id must be an integer >= 1 when provided")
    return int(v)


def _validate_v2_rows(rows: list[ScheduleV2Row]) -> None:
    if not rows:
        raise RuntimeError("Schedule validation error: v2 schedule has no title rows")

    seen: set[tuple[str, int]] = set()
    discs_with_titles: set[str] = set()
    for r in rows:
        key = (r.disc_id, r.source_title_index)
        if key in seen:
            raise RuntimeError(
                "Schedule validation error at "
                f"line {r.line}: duplicate (disc_id, source_title_index)=({r.disc_id!r}, {r.source_title_index})"
            )
        seen.add(key)
        discs_with_titles.add(r.disc_id)

    if not discs_with_titles:
        raise RuntimeError("Schedule validation error: each disc must include at least one selected title")


def _load_schedule_v2_json(file: Path, text: str) -> list[ScheduleV2Row]:
    try:
        payload = json.loads(text)
    except Exception as e:
        raise RuntimeError(f"Schedule parse error in {file}: invalid JSON ({e})") from e

    if isinstance(payload, dict):
        version = int(payload.get("version", 2))
        if version != 2:
            raise RuntimeError(f"Schedule parse error in {file}: JSON version must be 2")
        items = payload.get("items")
    elif isinstance(payload, list):
        version = 2
        items = payload
    else:
        raise RuntimeError(f"Schedule parse error in {file}: JSON must be an array or object")

    if not isinstance(items, list):
        raise RuntimeError(f"Schedule parse error in {file}: JSON payload must include an items array")

    rows: list[ScheduleV2Row] = []
    for idx, item in enumerate(items, start=1):
        item_label = f"item {idx}"
        if not isinstance(item, dict):
            raise RuntimeError(f"Schedule parse error at {item_label}: each item must be an object")

        disc_number_raw = item.get("disc_number", item.get("disc_id", ""))
        disc_number = _parse_disc_number(disc_number_raw, item_label=item_label)
        disc_id = str(item.get("disc_id", f"disc-{disc_number}"))
        disc_id = " ".join(disc_id.strip().split()) or f"disc-{disc_number}"
        source_title_index = _parse_source_title_index(item.get("source_title_index", ""), item_label=item_label)
        movie_title = normalize_title(str(item.get("movie_title", "")), item_label=item_label)
        year = normalize_year(item.get("year", ""), item_label=item_label)
        tmdb_id = _parse_tmdb_id(item.get("tmdb_id", ""), item_label=item_label)
        output_role = _normalize_output_role(item.get("output_role", ""), item_label=item_label)

        rows.append(
            ScheduleV2Row(
                disc_id=disc_id,
                disc_number=disc_number,
                source_title_index=source_title_index,
                movie_title=movie_title,
                year=year,
                tmdb_id=tmdb_id,
                output_role=output_role,
                line=idx,
            )
        )

    _validate_v2_rows(rows)
    return rows


def _load_schedule_v2_csv(lines: list[str]) -> list[ScheduleV2Row]:
    rows: list[ScheduleV2Row] = []
    seen_header = False

    for n, raw in enumerate(lines, start=1):
        line = raw.rstrip("\r")
        if n == 1:
            line = line.lstrip("\ufeff")
        if not line.strip() or line.lstrip().startswith("#"):
            continue

        cols = next(csv.reader([line], skipinitialspace=True))
        if not seen_header:
            head = [c.strip().lower() for c in cols]
            expected = ["disc_id", "disc_number", "source_title_index", "movie_title", "year", "tmdb_id", "output_role"]
            if head == expected:
                seen_header = True
                continue

        if len(cols) != 7:
            raise RuntimeError(
                f"CSV parse error at line {n}: v2 format expects 7 columns\n  Line: {line}"
            )

        disc_id_raw, disc_number_raw, source_title_index_raw, movie_title_raw, year_raw, tmdb_id_raw, output_role_raw = [
            trim_ws(c) for c in cols
        ]
        item_label = f"line {n}"
        disc_id = " ".join(disc_id_raw.split())
        if not disc_id:
            raise RuntimeError(f"Schedule validation error at {item_label}: disc_id is required")
        disc_number = _parse_disc_number(disc_number_raw, item_label=item_label)
        source_title_index = _parse_source_title_index(source_title_index_raw, item_label=item_label)
        movie_title = normalize_title(movie_title_raw, item_label=item_label)
        year = normalize_year(year_raw, item_label=item_label)
        tmdb_id = _parse_tmdb_id(tmdb_id_raw, item_label=item_label)
        output_role = _normalize_output_role(output_role_raw, item_label=item_label)

        rows.append(
            ScheduleV2Row(
                disc_id=disc_id,
                disc_number=disc_number,
                source_title_index=source_title_index,
                movie_title=movie_title,
                year=year,
                tmdb_id=tmdb_id,
                output_role=output_role,
                line=n,
            )
        )

    _validate_v2_rows(rows)
    return rows


def load_schedule(file: Path) -> ParsedSchedule:
    text = file.read_text(errors="ignore")
    lines = text.splitlines()
    first = ""
    for raw in lines:
        line = raw.lstrip("\ufeff").strip()
        if line and not line.startswith("#"):
            first = line
            break

    if not first:
        raise RuntimeError(f"CSV schedule is empty: {file}")

    if first.startswith("{") or first.startswith("["):
        rows_v2 = _load_schedule_v2_json(file, text)
        return ParsedSchedule(version=2, rows_v1=[], rows_v2=rows_v2)

    lowered = first.lower()
    if "source_title_index" in lowered or "output_role" in lowered or "disc_id" in lowered:
        rows_v2 = _load_schedule_v2_csv(lines)
        return ParsedSchedule(version=2, rows_v1=[], rows_v2=rows_v2)

    rows_v1 = _load_csv_schedule_v1_from_lines(lines, file)
    return ParsedSchedule(version=1, rows_v1=rows_v1, rows_v2=[])


def _load_csv_schedule_v1_from_lines(lines: list[str], file: Path) -> list[ScheduleRow]:
    rows: list[ScheduleRow] = []

    for n, raw in enumerate(lines, start=1):
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
        name = normalize_title(name, item_label=f"line {n}")
        year = normalize_year(year, item_label=f"line {n}")

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


def load_csv_schedule(file: Path) -> list[ScheduleRow]:
    parsed = load_schedule(file)
    if parsed.version != 1:
        raise RuntimeError(
            f"Schedule format v{parsed.version} is not supported by legacy CSV rip flow; "
            "use a v1 4-column schedule for remote --csv runs."
        )
    return parsed.rows_v1


def csv_disc_prompt_for_row(r: ScheduleRow) -> str:
    if r.kind == "movie":
        if r.third == "y":
            return f"Insert: Movie '{r.name} ({r.year})' Disc {r.disc} (MultiDisc=y). Press Enter when ready."
        return f"Insert: Movie '{r.name} ({r.year})' Disc {r.disc}. Press Enter when ready."
    return f"Insert: Series '{r.name} ({r.year})' Season {r.third} Disc {r.disc}. Press Enter when ready."


def csv_next_up_note(next_row: ScheduleRow) -> None:
    print(f"Next up: {csv_disc_prompt_for_row(next_row)}")
