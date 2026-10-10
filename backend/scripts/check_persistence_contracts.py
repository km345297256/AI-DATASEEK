"""Run from backend: python scripts/check_persistence_contracts.py --check.

--candidate writes a NEW generated snapshot; it never edits accepted history.
--review --before-ref REF --after-ref REF compares trusted local source commits,
not their acknowledgements; no checkout, fetch, store access or migration occurs.
"""
import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from app.domain.utils.persistence_contracts import current_catalog, check_history, review_catalogs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--check", action="store_true")
    modes.add_argument("--candidate", type=Path, help="New generated JSON path (must not exist)")
    modes.add_argument("--review", action="store_true", help="Compare actual schemas from two trusted local Git commits")
    parser.add_argument("--before-ref")
    parser.add_argument("--after-ref")
    args = parser.parse_args()
    if args.review:
        if not args.before_ref or not args.after_ref:
            parser.error("--review requires --before-ref and --after-ref")
        from persistence_review import catalog_at_ref
        try:
            before_commit, before = catalog_at_ref(BACKEND.parent, args.before_ref)
            after_commit, after = catalog_at_ref(BACKEND.parent, args.after_ref)
            report = review_catalogs(before, after)
        except (ValueError, OSError, KeyError, TypeError) as error:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "before_commit": before_commit,
                          "after_commit": after_commit, **report}, ensure_ascii=False))
        return 0
    if args.before_ref or args.after_ref:
        parser.error("Git revisions require --review")
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
