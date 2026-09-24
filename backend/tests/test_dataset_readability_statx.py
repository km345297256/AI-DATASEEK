"""Synchronized metadata must fix cache staleness without weakening identity."""
import ctypes
import errno
import os
import struct
import sys
from types import SimpleNamespace

import pytest

from app.infrastructure.external.sandbox.dataset_readability import DatasetReadabilityError, _dataset_view_operation


def install_statx(monkeypatch, *, transform=None, error=None, mask=0x7ff):
    original_stat, original_fstat = os.stat, os.fstat
    calls = []

    def statx(fd, name, flags, requested, buffer):
        calls.append((name, flags, requested))
        if error is not None:
            ctypes.set_errno(error)
            return -1
        value = original_fstat(fd) if not name else original_stat(name, dir_fd=fd, follow_symlinks=False)
        values = {key: getattr(value, key) for key in (
            'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns',
            'st_mode', 'st_uid', 'st_gid', 'st_nlink')}
        if transform:
            transform(values, name, flags)
        raw = bytearray(256)
        for fmt, offset, val in [
            ('I', 0, mask), ('I', 16, values['st_nlink']), ('I', 20, values['st_uid']),
            ('I', 24, values['st_gid']), ('H', 28, values['st_mode']),
            ('Q', 32, values['st_ino']), ('Q', 40, values['st_size']),
            ('q', 96, values['st_ctime_ns'] // 1_000_000_000),
            ('I', 104, values['st_ctime_ns'] % 1_000_000_000),
            ('q', 112, values['st_mtime_ns'] // 1_000_000_000),
            ('I', 120, values['st_mtime_ns'] % 1_000_000_000),
            ('I', 136, os.major(values['st_dev'])), ('I', 140, os.minor(values['st_dev'])),
        ]:
            struct.pack_into('=' + fmt, raw, offset, val)
        ctypes.memmove(buffer, bytes(raw), 256)
        return 0

    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a, **kw: SimpleNamespace(statx=statx))
    return calls


def test_every_comparison_fetches_attributes_and_preserves_fd_nofollow_boundaries(tmp_path, monkeypatch):
    (tmp_path / 'sample.csv').write_text('value\n1\n')
    calls = install_statx(monkeypatch)
    assert _dataset_view_operation('probe', str(tmp_path)) == {'ok': True, 'file_count': 1}
    assert len(calls) >= 6
    assert all(flags & 0x2000 and requested == 0x7ff for _, flags, requested in calls)
    assert all(flags & (0x100 if name else 0x1000) for name, flags, _ in calls)
    assert any(name == b'sample.csv' for name, _, _ in calls)


@pytest.mark.parametrize('field,delta', [
    ('st_ino', 1), ('st_size', 1), ('st_mtime_ns', 1), ('st_ctime_ns', 1),
    ('st_mode', 0o020), ('st_uid', 1), ('st_gid', 1), ('st_nlink', 1),
])
@pytest.mark.parametrize('target', ['file', 'directory'])
def test_real_identity_change_still_rejected_after_synchronized_read(tmp_path, monkeypatch, field, delta, target):
    path = tmp_path / 'sample.csv'
    path.write_text('value\n1\n')
    target_inode = (path if target == 'file' else tmp_path).stat().st_ino
    changed = False
    original_read = os.read

    def read(fd, size):
        nonlocal changed
        result = original_read(fd, size)
        changed = True
        return result

    def transform(values, name, flags):
        if changed and values['st_ino'] == target_inode:
            values[field] += delta

    install_statx(monkeypatch, transform=transform)
    monkeypatch.setattr(os, 'read', read)
    result = _dataset_view_operation('probe', str(tmp_path))
    assert result['ok'] is False and result['code'] == 'dataset_changed'
    assert result['diagnostic']['object_kind'] == target
    assert result['diagnostic']['changed_fields'] == [{'st_ino': 'inode', 'st_size': 'size',
        'st_mtime_ns': 'mtime_ns', 'st_ctime_ns': 'ctime_ns', 'st_mode': 'mode',
        'st_uid': 'uid', 'st_gid': 'gid', 'st_nlink': 'link_count'}[field]]
    assert str(tmp_path) not in str(result)


def test_missing_owner_metadata_cannot_be_admitted_as_zero(tmp_path, monkeypatch):
    (tmp_path / 'sample').write_text('x')
    install_statx(monkeypatch, mask=0x7ff & ~0x8)
    assert _dataset_view_operation('probe', str(tmp_path)) == {'ok': False, 'code': 'dataset_preparation_failed'}


def test_statx_access_denied_stays_unreadable(tmp_path, monkeypatch):
    install_statx(monkeypatch, error=errno.EACCES)
    assert _dataset_view_operation('probe', str(tmp_path)) == {'ok': False, 'code': 'dataset_unreadable'}


def test_old_kernel_fallback_keeps_link_rejection(tmp_path, monkeypatch):
    (tmp_path / 'unsafe').symlink_to('/etc/passwd')
    calls = install_statx(monkeypatch, error=errno.ENOSYS)
    assert _dataset_view_operation('probe', str(tmp_path)) == {'ok': False, 'code': 'dataset_unsafe'}
    assert len(calls) == 1


def test_synchronized_entry_check_does_not_follow_link(tmp_path, monkeypatch):
    (tmp_path / 'unsafe').symlink_to('/etc/passwd')
    calls = install_statx(monkeypatch)
    assert _dataset_view_operation('probe', str(tmp_path)) == {'ok': False, 'code': 'dataset_unsafe'}
    assert any(name == b'unsafe' and flags & 0x100 for name, flags, _ in calls)


def test_diagnostics_keep_only_anonymous_whitelisted_facts():
    safe = {'stage': 'directory_scan', 'object_kind': 'directory',
            'object_id': 'a' * 16, 'changed_fields': ['uid', 'gid']}
    error = DatasetReadabilityError('dataset_changed', {**safe, 'host_path': '/private/source'})
    assert error.diagnostic == safe
    assert '/private/source' not in str(error)


@pytest.mark.parametrize('bad', [None, 'private path', {'stage': []},
    {'stage': 'file_read', 'object_kind': [], 'object_id': 'a' * 16, 'changed_fields': ['uid']},
    {'stage': 'file_read', 'object_kind': 'file', 'object_id': '/private/source', 'changed_fields': ['uid']}])
def test_malformed_diagnostics_never_cross_helper_boundary(bad):
    assert DatasetReadabilityError('dataset_changed', bad).diagnostic is None
