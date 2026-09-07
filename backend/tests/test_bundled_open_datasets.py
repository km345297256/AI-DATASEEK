"""The shipped catalog must be usable offline, attributed and complete."""
from collections import Counter
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from app.domain.models.dataset import CuratedDatasetSeed
from app.application.services.data_center_dataset_service import DataCenterDatasetService

ROOT = Path(__file__).resolve().parents[1] / 'app/resources/datasets'
DOMAINS = {'general', 'tabular', 'geoscience', 'image_science', 'chemistry', 'sequence', 'space', 'documents', 'spectroscopy'}


def test_catalog_has_two_real_bundles_per_system_domain():
    counts = Counter()
    for path in ROOT.glob('*/manifest.json'):
        seed = CuratedDatasetSeed.model_validate_json(path.read_text())
        counts[seed.domain] += 1
        assert seed.dataset_id == path.parent.name
        assert seed.metadata['curated'] is True
        for field in ('publisher', 'license', 'sample_scope'):
            assert seed.metadata[field]
        for field in ('source_url', 'license_url'):
            parsed = urlsplit(seed.metadata[field])
            assert parsed.scheme == 'https' and parsed.netloc
            assert not parsed.username and not parsed.password
        validated = DataCenterDatasetService._verified_managed_files(path.parent, seed.files)
        assert len(validated) == len(seed.files)
        provenance = {entry['path']: entry for entry in seed.metadata['provenance']}
        for item in seed.files:
            assert item.sha256 and item.size and item.size > 0
            assert provenance[item.path]['sha256'] == item.sha256
            assert urlsplit(provenance[item.path]['download_url']).scheme == 'https'
            raw = (path.parent / item.path).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == item.sha256
            # Reject accidental downloads of an HTML error/login page.
            assert not raw.lstrip().lower().startswith((b'<!doctype html', b'<html'))
    assert set(counts) == DOMAINS
    assert all(count == 2 for count in counts.values())
