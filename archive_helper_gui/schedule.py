from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from archive_helper_core.schedule_csv import normalize_title, normalize_year


def write_csv_rows(path: Path, rows: list[str]) -> None:
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


@dataclass
class ScheduleV2Selection:
    disc_id: str
    disc_number: int
    source_title_index: int
    movie_title: str
    year: str
    tmdb_id: int | None = None
    output_role: str = "main"


def write_schedule_v2(path: Path, selections: list[ScheduleV2Selection]) -> None:
    if not selections:
        raise ValueError("v2 schedule requires at least one selected title row.")

    items: list[dict[str, object]] = []
    for idx, item in enumerate(selections, start=1):
        label = f"item {idx}"
        if item.disc_number < 1:
            raise ValueError(f"Schedule validation error at {label}: disc_number must be >= 1.")
        if item.source_title_index < 0:
            raise ValueError(f"Schedule validation error at {label}: source_title_index must be >= 0.")
        disc_id = " ".join((item.disc_id or "").strip().split())
        if not disc_id:
            raise ValueError(f"Schedule validation error at {label}: disc_id is required.")
        if item.output_role not in {"main", "extra"}:
            raise ValueError(f"Schedule validation error at {label}: output_role must be 'main' or 'extra'.")

        normalized_title = normalize_title(item.movie_title, item_label=label)
        normalized_year = normalize_year(item.year, item_label=label)

        row = asdict(item)
        row["disc_id"] = disc_id
        row["movie_title"] = normalized_title
        row["year"] = normalized_year
        items.append(row)

    payload = {"version": 2, "items": items}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def csv_rows_from_manual(
    kind: str,
    name: str,
    year: str,
    season: str,
    start_disc: int,
    total_discs: int,
) -> list[str]:
    if "," in name:
        raise ValueError("Title must not contain commas (CSV constraint).")

    rows: list[str] = []
    if kind == "movie":
        md = "y" if total_discs > 1 else "n"
        for d in range(start_disc, total_discs + 1):
            rows.append(f"{name}, {year}, {md}, {d}")
    else:
        if not season.strip().isdigit() or int(season.strip()) < 1:
            raise ValueError("Season must be an integer >= 1.")
        s = str(int(season.strip())).zfill(2)
        for d in range(start_disc, total_discs + 1):
            rows.append(f"{name}, {year}, {s}, {d}")

    return rows
