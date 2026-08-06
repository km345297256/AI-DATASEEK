from app.domain.models.dataset import (
    DataCenterDataset,
    DatasetFile,
    DatasetLocation,
    DatasetStorageType,
)
from app.interfaces.schemas.dataset import dataset_response


def test_dataset_response_never_serializes_host_paths_or_mount_names():
    source_path = "/srv/private/tenant-a"
    mount_name = "tenant-a"
    location = DatasetLocation(
        location_id="dsl_private",
        node_id="local-default",
        storage_type=DatasetStorageType.HOST_PATH,
        source_path=source_path,
        mount_name=mount_name,
        verified=True,
        verification_message="Directory registered",
    )
    dataset = DataCenterDataset(
        dataset_id="ds_private",
        data_center_id="center-a",
        data_center_name="Center A",
        name="Private dataset",
        files=[
            DatasetFile(path="sources/dsl_private/tenant-a"),
            DatasetFile(path="sources/dsl_private/tenant-a/nested/report.csv", size=42),
            DatasetFile(path="/srv/private/tenant-a/absolute-secret.csv", size=7),
        ],
        locations=[location],
    )

    response = dataset_response(
        dataset,
        include_locations=True,
        include_file_paths=True,
    )
    payload = response.model_dump(mode="json")
    serialized = response.model_dump_json()

    assert payload["locations"] == [
        {
            "location_id": "dsl_private",
            "node_id": "local-default",
            "storage_type": "host_path",
            "read_only": True,
            "verified": True,
            "verification_message": "Directory registered",
            "version": "1",
        }
    ]
    assert [(item["name"], item["path"]) for item in payload["files"]] == [
        ("report.csv", "nested/report.csv"),
    ]
    assert "source_path" not in serialized
    assert "mount_name" not in serialized
    assert source_path not in serialized
    assert mount_name not in serialized


def test_dataset_metadata_recursively_omits_host_paths_and_path_configuration():
    dataset = DataCenterDataset(
        dataset_id="ds_metadata_private",
        data_center_id="center-a",
        data_center_name="Center A",
        name="Metadata privacy",
        metadata={
            "source_path": "/srv/private/tenant-a/source.csv",
            "runtime": {
                "DatasetHostPathAllowlist": ["/srv/private", "/data/private"],
                "storage-directory": "/mnt/private/datasets",
                "safe_mode": "read-only",
            },
            "notes": [
                "ordinary analysis note",
                "loaded from /opt/private/input.csv during registration",
                {"canonicalSourceDirectory": r"C:\Users\private\dataset"},
                {r"\\fileserver\private\dataset": "UNC path used as a key"},
                "file:///var/lib/private/data.parquet",
            ],
        },
    )

    for include_locations in (False, True):
        response = dataset_response(dataset, include_locations=include_locations)
        payload = response.model_dump(mode="json")
        serialized = response.model_dump_json()

        assert payload["metadata"] == {
            "runtime": {"safe_mode": "read-only"},
            "notes": ["ordinary analysis note"],
        }
        for private_value in (
            "source_path",
            "DatasetHostPathAllowlist",
            "storage-directory",
            "/srv/private",
            "/data/private",
            "/mnt/private",
            "/opt/private",
            r"C:\Users\private",
            r"\\fileserver\private",
            "file:///var/lib/private",
        ):
            assert private_value not in serialized


def test_dataset_metadata_preserves_analysis_values_urls_and_relative_paths():
    metadata = {
        "dimensions": {"rows": 120, "columns": ["temperature", "humidity"]},
        "statistics": {"mean": 12.5, "missing": None},
        "spatial": {"crs": "EPSG:4326", "bbox": [90.0, 30.0, 110.0, 42.0]},
        "relative_path": "derived/summary.csv",
        "homepage": "https://data.example.org/catalog/dataset-a",
        "storage_format": "GeoTIFF",
        "root_mean_square": 0.125,
    }
    dataset = DataCenterDataset(
        dataset_id="ds_metadata_public",
        data_center_id="center-a",
        data_center_name="Center A",
        name="Public metadata",
        metadata=metadata,
    )

    payload = dataset_response(dataset).model_dump(mode="json")

    assert payload["metadata"] == metadata


def test_dataset_metadata_becomes_empty_when_every_value_is_private():
    dataset = DataCenterDataset(
        dataset_id="ds_metadata_only_private",
        data_center_id="center-a",
        data_center_name="Center A",
        name="Private-only metadata",
        metadata={
            "source_path": "/srv/private/tenant-a/source.csv",
            "runtime": {"host_root": "/srv/private"},
        },
    )

    payload = dataset_response(dataset).model_dump(mode="json")

    assert payload["metadata"] == {}
