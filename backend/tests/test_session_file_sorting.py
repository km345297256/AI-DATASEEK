from datetime import datetime

from app.domain.models.file import FileInfo
from app.interfaces.api.session_routes import _sort_session_files


def test_sort_session_files_by_filename_ascending():
    files = [
        FileInfo(file_id="1", filename="zeta.txt"),
        FileInfo(file_id="2", filename="Alpha.txt"),
    ]

    sorted_files = _sort_session_files(files, "filename", "asc")

    assert [file.filename for file in sorted_files] == ["Alpha.txt", "zeta.txt"]


def test_sort_session_files_by_size_descending():
    files = [
        FileInfo(file_id="1", filename="small.txt", size=10),
        FileInfo(file_id="2", filename="large.txt", size=2048),
        FileInfo(file_id="3", filename="unknown.txt", size=None),
    ]

    sorted_files = _sort_session_files(files, "size", "desc")

    assert [file.filename for file in sorted_files] == ["large.txt", "small.txt", "unknown.txt"]


def test_sort_session_files_by_upload_date_descending():
    files = [
        FileInfo(file_id="1", filename="old.txt", upload_date=datetime(2024, 1, 1)),
        FileInfo(file_id="2", filename="new.txt", upload_date=datetime(2025, 1, 1)),
    ]

    sorted_files = _sort_session_files(files, "upload_date", "desc")

    assert [file.filename for file in sorted_files] == ["new.txt", "old.txt"]
