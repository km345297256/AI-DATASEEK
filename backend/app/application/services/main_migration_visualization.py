"""Explicit trusted Cordis bindings for the migrated main workbenches.

Only inert payload validators live in the API. Scientific libraries are imported
solely by the one-shot sandbox worker, never by this host-side dispatch.
"""
from __future__ import annotations

KINDS = {
    "matrix-workbench": {"tree", "image", "series"},
    "astronomy-workbench": {"tree", "image", "table", "series"},
    "alignment-browser": {"tree", "table"},
    "sequence-browser": {"tree", "table"},
    "genome-tracks": {"tree", "map"},
    "blast-hits": {"tree", "table"},
}
INPUT_LIMITS = {reader: mib * 1024**2 for reader, mib in {
    "matrix-workbench":128, "astronomy-workbench":32, "alignment-browser":64,
    "sequence-browser":16, "genome-tracks":16, "blast-hits":16}.items()}
OUTPUT_LIMITS = {reader: mib * 1024**2 for reader, mib in {
    "matrix-workbench":8, "astronomy-workbench":4, "alignment-browser":4,
    "sequence-browser":2, "genome-tracks":2, "blast-hits":2}.items()}


def _validators(reader):
    if reader == "matrix-workbench":
        from app.application.services.main_matrix_visualization import validate_main_matrix_options, validate_main_matrix_payload
        return validate_main_matrix_options, validate_main_matrix_payload
    if reader == "astronomy-workbench":
        from app.application.services.astronomy_workbench_visualization import validate_options, validate_payload
        return validate_options, validate_payload
    if reader == "alignment-browser":
        from app.application.services.alignment_browser_visualization import validate_options, validate_payload
        return validate_options, validate_payload
    if reader in {"sequence-browser", "genome-tracks", "blast-hits"}:
        from app.application.services.sequence_browser_visualization import validate_options, validate_payload
        return lambda kind, options: validate_options(reader, kind, options), lambda value, **kwargs: validate_payload(value, reader=reader, **kwargs)
    raise ValueError("Unregistered migration reader")


def validate_options(reader, kind, options):
    if kind not in KINDS[reader]:
        raise ValueError("Unsupported visualization kind")
    return _validators(reader)[0](kind, options)


def validate_payload(value, reader, kind, *, options=None, format=None, source_bytes=None, limit=None):
    import json
    maximum = min(OUTPUT_LIMITS[reader], limit or OUTPUT_LIMITS[reader])
    kwargs = {"kind":kind, "options":options}
    if reader == "matrix-workbench":
        kwargs.update(fmt=format, size=source_bytes, limit=maximum)
    else:
        kwargs.update(format=format, source_bytes=source_bytes)
        if reader != "astronomy-workbench": kwargs["limit"] = maximum
    result = _validators(reader)[1](value, **kwargs)
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > maximum:
        raise ValueError("Migrated visualization output budget exceeded")
    return result
