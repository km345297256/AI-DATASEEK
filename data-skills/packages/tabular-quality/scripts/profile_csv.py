"""Bounded, read-only UTF-8 comma-separated table quality profile."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 10000
MAX_COLUMNS = 256


def profile(data: bytes) -> dict:
    if len(data) > MAX_BYTES:
        raise ValueError('CSV exceeds 4 MiB budget')
    reader = csv.reader(io.StringIO(data.decode('utf-8-sig'), newline=''), strict=True)
    header = next(reader, None)
    if not header or len(header) > MAX_COLUMNS or any(not name.strip() for name in header):
        raise ValueError('CSV requires nonempty column names, at most 256 columns')
    if len(set(header)) != len(header):
        raise ValueError('Duplicate column names')
    missing = [0] * len(header)
    seen = set()
    rows = duplicates = 0
    for row in reader:
        rows += 1
        if rows > MAX_ROWS:
            raise ValueError('CSV exceeds 10000 row budget')
        if len(row) != len(header):
            raise ValueError('CSV row width differs from header')
        key = tuple(row)
        duplicates += key in seen
        seen.add(key)
        for index, value in enumerate(row):
            missing[index] += not value.strip()
    return {
        'schema_version': 1, 'input_sha256': hashlib.sha256(data).hexdigest(),
        'row_count': rows, 'column_count': len(header), 'duplicate_rows': duplicates,
        'columns': [{'name': name, 'missing_count': missing[index]} for index, name in enumerate(header)],
        'method': {'missing': 'empty_or_whitespace', 'duplicates': 'exact_parsed_row_after_first',
                   'dialect': 'comma', 'encoding': 'utf-8-sig'},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    args = parser.parse_args()
    try:
        with args.input.open('rb') as stream:
            result = profile(stream.read(MAX_BYTES + 1))
    except (OSError, ValueError, UnicodeError, csv.Error):
        print(json.dumps({'valid': False, 'error': 'CSV unreadable, malformed, or outside declared limits'}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
