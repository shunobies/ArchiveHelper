from __future__ import annotations

from pathlib import Path


def write_csv_rows(path: Path, rows: list[str]) -> None:
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


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
