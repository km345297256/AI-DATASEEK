"""Exact synthetic-file ownership for local acceptance scripts, never production.

An HTTP response is not deletion authority. Bind each ID to this run's declared
filename, size, owner, provider and stored-file document before permitting any
cleanup. Unknown/changed bindings fail closed, leaving a visible fixture error.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

ID = re.compile(r"[A-Za-z0-9_:-]{1,256}\Z")
NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


def local_base(value):
    try:
        address = urlparse(value)
        valid = (type(value) is str and value == value.strip() and not any(ord(c) < 32 for c in value)
                 and "?" not in value and "#" not in value
                 and address.scheme == "http" and address.username is None and address.password is None
                 and not address.query and not address.fragment and address.path in {"", "/"}
                 and ((address.hostname == "frontend" and address.port in {None, 80})
                      or (address.hostname in {"localhost", "127.0.0.1", "::1"} and address.port == 7001)))
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise RuntimeError("Only the existing local frontend or loopback port 7001 may be tested")
    return value.rstrip("/")


class FixtureLedger:
    def __init__(self, database, source, run, user_id=None):
        assert type(source) is str and NAME.fullmatch(source)
        assert type(run) is str and NAME.fullmatch(run)
        assert user_id is None or (type(user_id) is str and user_id)
        self.database, self.source, self.run, self.user_id = database, source, run, user_id
        self.expected, self.records = {}, {}

    def declare(self, name, size):
        assert type(name) is str and NAME.fullmatch(name) and name not in {".", ".."}
        assert type(size) is int and 0 <= size <= 64 * 1024 * 1024
        filename = self.run + "-" + name
        assert filename not in self.expected and len(self.expected) < 128
        self.expected[filename] = size
        return filename

    def _inventory_query(self):
        return {"filename": {"$in": list(self.expected)}, "metadata.source": self.source,
                "metadata.regression_run": self.run}

    def _binding(self, file_id, filename):
        assert type(file_id) is str and ID.fullmatch(file_id) and filename in self.expected
        return {"file_id": file_id, "filename": filename,
                "metadata.source": self.source, "metadata.regression_run": self.run}

    async def accept(self, value):
        assert type(value) is dict
        file_id, filename = value.get("file_id"), value.get("filename")
        exact = self._binding(file_id, filename)
        assert type(value.get("size")) is int and value["size"] == self.expected[filename]
        assert await self.database.stored_files.count_documents({"file_id": file_id}) == 1
        rows = await self.database.stored_files.find(exact).limit(2).to_list()
        assert len(rows) == 1, "Synthetic file binding missing or ambiguous; refusing cleanup authority"
        record = rows[0]
        assert record.get("_id") is not None and record.get("provider") == "minio"
        assert type(record.get("size")) is int and record["size"] == self.expected[filename]
        assert type(record.get("user_id")) is str and record["user_id"]
        assert self.user_id is None or record["user_id"] == self.user_id
        snapshot = {key: record[key] for key in ("_id", "file_id", "filename", "user_id", "size", "provider")}
        if file_id in self.records:
            assert self.records[file_id] == snapshot, "Synthetic binding changed; refusing overwrite"
        else:
            assert all(item["filename"] != filename for item in self.records.values()), "Duplicate synthetic filename"
            self.records[file_id] = snapshot
        return file_id

    async def recover(self):
        """Recover only declared uploads, including a lost successful response."""
        rows = await self.database.stored_files.find(self._inventory_query()).limit(len(self.expected) + 1).to_list()
        assert len(rows) <= len(self.expected), "Unexpected synthetic inventory size"
        assert all(type(row.get("file_id")) is str and type(row.get("filename")) is str for row in rows)
        assert len({row["file_id"] for row in rows}) == len(rows), "Duplicate synthetic file ID"
        assert len({row["filename"] for row in rows}) == len(rows), "Duplicate synthetic filename"
        for row in rows:
            await self.accept({key: row.get(key) for key in ("file_id", "filename", "size")})

    async def assert_owned(self, file_id):
        assert type(file_id) is str and file_id in self.records, "No synthetic deletion authority"
        record = self.records[file_id]
        exact = {**self._binding(file_id, record["filename"]), **record}
        total = await self.database.stored_files.count_documents({"file_id": file_id})
        if not total:
            return False
        assert total == 1 and await self.database.stored_files.count_documents(exact) == 1, "Synthetic binding changed; refusing deletion"
        return True

    async def assert_absent(self, file_id):
        assert file_id in self.records
        assert not await self.database.stored_files.count_documents({"file_id": file_id}), "Deleted synthetic file is still stored"

    async def assert_clean(self):
        assert not await self.database.stored_files.count_documents(self._inventory_query()), "This run still has synthetic files"
