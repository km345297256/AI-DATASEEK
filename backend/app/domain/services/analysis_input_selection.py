"""Selection of user-provided session inputs, never inferred from output files."""
from app.domain.models.event import MessageEvent
from app.domain.models.file import FileInfo
from app.domain.models.analysis_input import assign_upload_namespace


def input_snapshot(files: list[FileInfo]) -> list[dict]:
    return [{"file_id": info.file_id, "filename": info.filename, "size": info.size,
             "content_type": info.content_type,
             "metadata": {key: (info.metadata or {}).get(key) for key in
                          ("analysis_input_namespace", "analysis_input_filename")}}
            for info in files]


def snapshot_files(metadata: dict | None) -> list[FileInfo]:
    return [FileInfo.model_validate(item) for item in (metadata or {}).get("analysis_input_files", [])]


def session_input_state(events) -> tuple[list[FileInfo], list[str]]:
    """Only explicit user attachments are eligible; assistant products never are."""
    known: dict[str, FileInfo] = {}
    selected: list[str] = []
    for event in events:
        if not isinstance(event, MessageEvent) or event.role != "user":
            continue
        incoming = [item for item in event.attachments or [] if item.file_id]
        normalized = assign_upload_namespace(incoming)
        authoritative = {item.file_id: item for item in snapshot_files(event.metadata)}
        for original, assigned in zip(incoming, normalized):
            # New admission metadata is server-owned. Older input events are only
            # read as user provenance; no old task or event is rewritten.
            # Upload metadata is user-controlled. Only the server-owned event
            # input snapshot can establish a persisted namespace, not metadata
            # copied from an uploaded object or an old attachment event.
            info = authoritative.get(original.file_id, assigned)
            known[info.file_id] = info.model_copy(deep=True)
        metadata = event.metadata or {}
        if "analysis_input_file_ids" in metadata:
            selected = list(metadata["analysis_input_file_ids"])
        elif incoming:
            selected = list(dict.fromkeys([*selected, *(item.file_id for item in incoming)]))
    return list(known.values()), [item for item in selected if item in known]


def select_input_files(events, incoming: list[FileInfo], requested: list[str] | None) -> tuple[list[FileInfo], list[FileInfo]]:
    known_files, inherited = session_input_state(events)
    known = {item.file_id: item for item in known_files}
    fresh = assign_upload_namespace([item for item in incoming if item.file_id not in known])
    known.update({item.file_id: item for item in fresh})
    submitted = [known[item.file_id] for item in incoming]
    chosen = list(dict.fromkeys([*(inherited if requested is None else requested), *(item.file_id for item in submitted)]))
    if any(item not in known for item in chosen):
        raise ValueError("所选文件不是本会话中由用户提交的分析资料。")
    return [known[item].model_copy(deep=True) for item in chosen], submitted
