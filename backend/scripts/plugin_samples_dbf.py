"""Lossless scalar dBASE III copy of the reviewed CC0 penguins CSV.

Field names are shortened to <=10 ASCII characters, with their mapping below.
Original numeric decimal strings are preserved, and NA numeric fields are blank
(DBF null). Text NA tokens remain text. No records are synthesized or repeated.
"""
import argparse
import csv
import hashlib
from pathlib import Path
import struct

FIELDS = [("species", "species", "C", 16, 0), ("island", "island", "C", 16, 0),
          ("bill_length_mm", "bill_len", "N", 6, 1), ("bill_depth_mm", "bill_dep", "N", 6, 1),
          ("flipper_length_mm", "flipper_mm", "N", 5, 0), ("body_mass_g", "mass_g", "N", 6, 0),
          ("sex", "sex", "C", 8, 0), ("year", "year", "N", 4, 0)]


def convert(source, target):
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != "f204db2c753b0937caac3cb35258562c14f073e4bbc76be24b4c51ce22767a93":
        raise ValueError("Unreviewed source")
    rows = list(csv.DictReader(raw.decode().splitlines()))
    assert len(rows) == 344
    width = 1 + sum(f[3] for f in FIELDS)
    header = bytearray(32)
    header[:4] = bytes([3, 126, 9, 18])
    struct.pack_into("<IHH", header, 4, len(rows), 33+32*len(FIELDS), width)
    output = bytearray(header)
    for _, label, kind, size, decimals in FIELDS:
        field = bytearray(32); field[:len(label)] = label.encode(); field[11] = ord(kind)
        field[16:18] = bytes([size, decimals]); output.extend(field)
    output.extend(b"\r")
    for row in rows:
        output.extend(b" ")
        for original, _, kind, size, _ in FIELDS:
            value = row[original]
            if kind == "N" and value == "NA": value = ""
            encoded = value.encode("ascii"); assert len(encoded) <= size
            output.extend(encoded.rjust(size, b" ") if kind == "N" else encoded.ljust(size, b" "))
    output.extend(b"\x1a")
    with target.open("xb") as stream: stream.write(output)
    print({"rows": len(rows), "columns": len(FIELDS), "bytes": len(output), "sha256": hashlib.sha256(output).hexdigest()})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path); parser.add_argument("destination", type=Path)
    args = parser.parse_args(); convert(args.source, args.destination)
