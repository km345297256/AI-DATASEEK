"""Run from backend: python scripts/check_persistence_contracts.py --check.

--candidate writes a NEW generated snapshot; it never edits accepted history.
"""
import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.domain.utils.persistence_contracts import current_catalog, check_history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--candidate", type=Path, help="New generated JSON path (must not exist)")
    args = parser.parse_args()
    current = current_catalog()
    if args.candidate:
        with args.candidate.open("x", encoding="utf-8") as output:
            json.dump(current, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.write("\n")
        return 0
    try:
        changes = check_history(BACKEND.parent, current)
    except (ValueError, OSError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": not changes, "changes": changes}, ensure_ascii=False))
    return int(bool(changes))


if __name__ == "__main__":
    raise SystemExit(main())
