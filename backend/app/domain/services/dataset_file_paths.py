"""One browser-safe dataset path projection shared by listing and preview."""
from pathlib import PurePosixPath

from app.domain.models.dataset import DatasetStorageType


def dataset_location_mount_name(location) -> str:
    # Legacy records omitted this synthetic alias. Derive it only to recognize
    # and strip an internal prefix; never expose it as storage information.
    return location.mount_name or (
        PurePosixPath(location.source_path.rstrip("/").replace("\\", "/")).name
        or "source"
    )


def dataset_host_file_relative_path(path: PurePosixPath, location) -> PurePosixPath | None:
    """Resolve only a matching source identity, with old/new inventory forms."""
    root = PurePosixPath("sources") / location.location_id
    if root not in path.parents:
        return None
    relative = path.relative_to(root)
    if relative.parts and relative.parts[0] == dataset_location_mount_name(location):
        relative = PurePosixPath(*relative.parts[1:])
    return None if str(relative) in {"", "."} else relative


def public_dataset_file_path(path: str, locations) -> PurePosixPath | None:
    """Project a registered inventory path exactly as the existing dataset API.

    This is a display projection, not an authorization/path-validation function.
    A preview caller must additionally enforce strict input and unique inventory
    membership before opening files. Multiple locations can project to the same
    display path; callers must not silently choose the first one.
    """
    public_path = PurePosixPath(path.replace("\\", "/"))
    if (public_path.is_absolute() or ".." in public_path.parts
            or str(public_path) in {"", "."}
            or (public_path.parts and len(public_path.parts[0]) == 2 and public_path.parts[0][1] == ":")
            or any(ord(char) < 32 or ord(char) == 127 for char in str(public_path))):
        return None
    for location in locations:
        if location.storage_type != DatasetStorageType.HOST_PATH:
            continue
        root = PurePosixPath("sources") / location.location_id
        if public_path == root:
            return None
        if root in public_path.parents:
            return dataset_host_file_relative_path(public_path, location)
    return public_path
