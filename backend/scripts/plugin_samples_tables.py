"""Offline, explicit conversions of the CC0 Palmer penguins observations.

Run inside the existing sandbox image, with reviewed input mounted read-only.
Only the caller's new output directory is written. No source SQL is executed;
SQL below is our fixed schema and values are always parameterized.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
from pathlib import Path
import sqlite3
import zipfile

PINNED_PENGUINS_SHA256 = "f204db2c753b0937caac3cb35258562c14f073e4bbc76be24b4c51ce22767a93"


def create(source: Path, destination: Path) -> dict:
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    if not source.is_file() or destination.exists():
        raise ValueError("Requires a reviewed input and a new output directory")
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != PINNED_PENGUINS_SHA256:
        raise ValueError("Penguins source differs from the reviewed CC0 snapshot")
    rows = list(csv.DictReader(raw.decode().splitlines()))
    assert len(rows) == 344 and list(rows[0]) == ["species", "island", "bill_length_mm", "bill_depth_mm", "flipper_length_mm", "body_mass_g", "sex", "year"]
    converted = []
    for row in rows:
        record = {}
        for key, value in row.items():
            if value == "NA": record[key] = None
            elif key in {"flipper_length_mm", "body_mass_g", "year"}: record[key] = int(value)
            elif key in {"bill_length_mm", "bill_depth_mm"}: record[key] = float(value)
            else: record[key] = value
        converted.append(record)
    assert sum(v is None for row in converted for v in row.values()) == 19
    destination.mkdir(parents=True)
    fields = list(rows[0])
    schema = "CREATE TABLE penguins(species TEXT, island TEXT, bill_length_mm REAL, bill_depth_mm REAL, flipper_length_mm INTEGER, body_mass_g INTEGER, sex TEXT, year INTEGER)"
    values = [tuple(row[k] for k in fields) for row in converted]
    sqlite = sqlite3.connect(destination / "penguins.sqlite")
    sqlite.execute(schema)
    sqlite.executemany("INSERT INTO penguins VALUES (?,?,?,?,?,?,?,?)", values)
    sqlite.commit()
    dump = "\n".join(sqlite.iterdump()) + "\n"
    assert sqlite.execute("SELECT COUNT(*) FROM penguins").fetchone()[0] == 344
    sqlite.close()
    (destination / "penguins.sql").write_text(dump)
    db = duckdb.connect(str(destination / "penguins.duckdb"))
    db.execute(schema)
    db.executemany("INSERT INTO penguins VALUES (?,?,?,?,?,?,?,?)", values)
    assert db.execute("SELECT COUNT(*) FROM penguins").fetchone()[0] == 344
    db.close()
    pq.write_table(pa.Table.from_pylist(converted), destination / "penguins.parquet", row_group_size=64, compression="NONE")
    assert pq.read_table(destination / "penguins.parquet").to_pylist() == converted
    (destination / "penguins.json").write_text(json.dumps({"source": "palmerpenguins", "license": "CC0", "missing_value_conversion": "Original NA tokens mapped to JSON null; raw CSV retained", "observations": converted}, ensure_ascii=False, indent=2) + "\n")
    title = "Palmer penguins: 344 observations (2007–2009)"
    note = "CC0 source: https://github.com/allisonhorst/palmerpenguins . Original CSV values and NA tokens preserved in this display. This format conversion is not a new experiment."
    markdown = "# " + title + "\n\n" + note + "\n\n| " + " | ".join(fields) + " |\n|" + "---|" * len(fields) + "\n"
    markdown += "\n".join("| " + " | ".join(row[k] for k in fields) + " |" for row in rows) + "\n"
    (destination / "penguins.md").write_text(markdown)
    table = "<table><thead><tr>" + "".join("<th>" + html.escape(k) + "</th>" for k in fields) + "</tr></thead><tbody>"
    table += "".join("<tr>" + "".join("<td>" + html.escape(row[k]) + "</td>" for k in fields) + "</tr>" for row in rows) + "</tbody></table>"
    (destination / "penguins.html").write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>' + title + '</title><style>body{font:16px sans-serif;margin:24px}td,th{padding:6px;text-align:left;border-bottom:1px solid #ddd}table{border-collapse:collapse}</style><h1>' + title + "</h1><p>" + html.escape(note) + "</p>" + table + "</html>")
    with zipfile.ZipFile(destination / "penguins-bundle.zip", "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in (("penguins.csv", raw), ("README.md", markdown.encode())):
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 18, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return {"source_sha256": hashlib.sha256(raw).hexdigest(), "rows": 344, "columns": 8, "missing_values": 19,
            "conversion": "Raw CSV retained. Numeric columns typed. NA -> SQL NULL, Arrow null, JSON null; original text in HTML/Markdown. No observations added or dropped.",
            "files": {p.name: {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(destination.iterdir())}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    print(json.dumps(create(args.source, args.destination), ensure_ascii=False))
