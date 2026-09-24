"""Recovery copies must fit the selected disk and survive live-file changes."""
import json
import hashlib
import io
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from installer import host, update_storage
from installer.host import HostError, atomic_json
from installer.tests.test_host import config, controller  # shared host fixtures
from modules.installation import InstallationError, validate_installation


@pytest.fixture
def snapshot_host(controller, monkeypatch):
    cfg = controller.config()
    data = Path(cfg['storage']['data_root'])
    controller.create_data_directories(cfg)
    controller.provision_secrets(cfg)
    controller.compose_path.write_text('{}')
    (controller.etc / 'runtime.env').write_text('')
    atomic_json(controller.state / 'storage.json', {'uuid': 'library-drive', 'data_root': str(data)})
    monkeypatch.setattr(controller, 'storage_guard', lambda: None)
    monkeypatch.setattr(host, 'safe_path', lambda path, **kwargs: Path(path))
    monkeypatch.setattr(controller, 'inspect_storage', lambda path: {'uuid': 'library-drive'})
    (data / 'book.mp3').write_bytes(b'unchanged library content' * 128)
    (data / 'mutable.txt').write_text('before')
    with sqlite3.connect(data / 'notes.db') as db:
        db.execute('CREATE TABLE notes(body TEXT)')
        db.execute("INSERT INTO notes VALUES('before')")
    return controller


def test_default_recovery_storage_is_alongside_library_not_on_system_disk(snapshot_host):
    c = snapshot_host
    root = c.update_snapshot_storage(create=True)
    assert root.parent == Path(c.config()['storage']['data_root']).parent
    assert c.etc not in root.parents
    assert root.stat().st_mode & 0o777 == 0o700
    assert json.loads((root / '.david-pi-update-recovery').read_text())['uuid'] == 'library-drive'


def test_wrong_recovery_filesystem_refused_even_if_marker_is_missing(snapshot_host, monkeypatch):
    c = snapshot_host
    root = c.update_snapshot_storage(create=True)
    (root / '.david-pi-update-recovery').unlink()
    root.rmdir()
    monkeypatch.setattr(c, 'inspect_storage', lambda path: {'uuid': 'system-disk-fallback'})
    with pytest.raises(HostError, match='missing or has changed'):
        c.update_snapshot_storage(create=True)
    assert not root.exists()


def test_recovery_path_must_not_overlap_data_or_independent_backup(snapshot_host, tmp_path):
    c = snapshot_host
    cfg = c.config()
    for path in (cfg['storage']['data_root'], cfg['storage']['data_root'] + '/recovery', str(tmp_path)):
        cfg['storage']['update_snapshot_root'] = path
        with pytest.raises(InstallationError, match='separate'):
            validate_installation(cfg)
    cfg['storage'].update(backup_root=str(tmp_path / 'backups'), update_snapshot_root=str(tmp_path / 'backups' / 'updates'))
    with pytest.raises(InstallationError, match='separate'):
        validate_installation(cfg)


def test_snapshot_reuse_never_links_live_files_and_keeps_consistent_databases(snapshot_host):
    c = snapshot_host
    data = Path(c.config()['storage']['data_root'])
    root = c.update_snapshot_storage(create=True)
    first, second = root / ('a' * 32), root / ('b' * 32)
    c.snapshot(first, {'id': first.name})
    assert (first / 'data/book.mp3').stat().st_ino != (data / 'book.mp3').stat().st_ino
    (data / 'mutable.txt').write_text('after')
    with sqlite3.connect(data / 'notes.db') as db:
        db.execute("UPDATE notes SET body='after'")
    c.snapshot(second, {'id': second.name})
    assert (second / 'data/book.mp3').stat().st_ino == (first / 'data/book.mp3').stat().st_ino
    assert (second / 'data/mutable.txt').stat().st_ino != (first / 'data/mutable.txt').stat().st_ino
    assert (first / 'data/mutable.txt').read_text() == 'before'
    assert (second / 'data/mutable.txt').read_text() == 'after'
    for path, expected in ((first, 'before'), (second, 'after')):
        with sqlite3.connect(path / 'data/notes.db') as db:
            assert db.execute('SELECT body FROM notes').fetchone()[0] == expected
    assert json.loads((second / 'snapshot.json').read_text())['reused_bytes'] >= (data / 'book.mp3').stat().st_size
    (data / 'book.mp3').write_bytes(b'live content changed after both snapshots')
    assert (first / 'data/book.mp3').read_bytes() == (second / 'data/book.mp3').read_bytes()


def test_incremental_capacity_counts_new_content_not_entire_library(snapshot_host, monkeypatch):
    c = snapshot_host
    data = Path(c.config()['storage']['data_root'])
    (data / 'book.mp3').write_bytes(b'unchanged' * 1024 * 1024)
    root = c.update_snapshot_storage(create=True)
    c.snapshot(root / ('a' * 32), {'id': 'a' * 32})
    # Enough for changed metadata/SQLite and reserve, not a second 9 MiB file.
    monkeypatch.setattr(host.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1024**3 + 1024**2))
    second = root / ('b' * 32)
    c.snapshot(second, {'id': second.name})
    assert json.loads((second / 'snapshot.json').read_text())['reused_bytes'] >= 9 * 1024**2
    (data / 'book.mp3').write_bytes(b'new bytes' * 1024 * 1024)
    with pytest.raises(HostError, match='existing snapshots were preserved'):
        c.snapshot(root / ('c' * 32), {'id': 'c' * 32})
    assert not (root / ('c' * 32)).exists()
    assert (second / 'snapshot.json').is_file()


def test_corrupt_prior_file_is_recopied_and_live_hardlink_is_refused(snapshot_host):
    c = snapshot_host
    data = Path(c.config()['storage']['data_root'])
    root = c.update_snapshot_storage(create=True)
    first = root / ('a' * 32)
    c.snapshot(first, {'id': first.name})
    old = first / 'data/book.mp3'
    old.write_bytes(b'corrupted')
    reusable, _, _ = update_storage.copy_plan(data, (first, json.loads((first / 'snapshot.json').read_text())))
    assert str(data / 'book.mp3') not in reusable
    old.unlink()
    os.link(data / 'book.mp3', old)
    with pytest.raises(update_storage.SnapshotError, match='never live-file links'):
        update_storage.copy_plan(data, (first, json.loads((first / 'snapshot.json').read_text())))


def test_capacity_reserves_committed_wal_growth_before_copying(snapshot_host, monkeypatch):
    c = snapshot_host
    data = Path(c.config()['storage']['data_root'])
    root = c.update_snapshot_storage(create=True)
    with sqlite3.connect(data / 'notes.db') as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('INSERT INTO notes(body) VALUES(?)', ('x' * (2 * 1024**2),))
        db.commit()
        wal_size = (data / 'notes.db-wal').stat().st_size
        assert wal_size > 2 * 1024**2
        copied = sum(path.stat().st_size for path in data.rglob('*') if path.is_file())
        # The old forecast fits, but not the expanded SQLite backup while the
        # copied WAL still exists. Refuse before creating the snapshot tree.
        free = 1024**3 + copied + (data / 'notes.db').stat().st_size + 512 * 1024
        monkeypatch.setattr(host.shutil, 'disk_usage', lambda path: SimpleNamespace(free=free))
        with pytest.raises(HostError, match='existing snapshots were preserved'):
            c.snapshot(root / ('a' * 32), {'id': 'a' * 32})
        assert not (root / ('a' * 32)).exists()


def test_success_cleanup_preserves_failed_unknown_current_and_live_content(snapshot_host):
    c = snapshot_host
    root = c.update_snapshot_storage(create=True)
    ids = ['a' * 32, 'b' * 32, 'c' * 32, 'd' * 32]
    for identifier, state in zip(ids, ('complete', 'failed', 'interrupted', 'running')):
        c.snapshot(root / identifier, {'id': identifier})
        atomic_json(c.jobs / (identifier + '.json'), {'operation': 'update', 'state': state})
    removed = update_storage.prune_successful(root, c.config()['instance_id'], ids[-1], c.jobs)
    assert removed == [ids[0]]
    assert all((root / identifier / 'data/book.mp3').is_file() for identifier in ids[1:])
    assert (Path(c.config()['storage']['data_root']) / 'book.mp3').is_file()
    assert c.remove_update_snapshot(ids[1])['content_preserved'] is True
    assert not (root / ids[1]).exists()


def test_snapshot_rejects_source_symlink_before_copy(snapshot_host, tmp_path):
    c = snapshot_host
    data = Path(c.config()['storage']['data_root'])
    outside = tmp_path / 'outside'; outside.write_text('must not enter snapshot')
    (data / 'link').symlink_to(outside)
    root = c.update_snapshot_storage(create=True)
    with pytest.raises(HostError, match='links or special files'):
        c.snapshot(root / ('a' * 32), {'id': 'a' * 32})
    assert not (root / ('a' * 32)).exists()


def test_only_recorded_failed_incomplete_snapshots_can_be_removed(snapshot_host):
    c = snapshot_host
    root = c.update_snapshot_storage(create=True)
    for key, state in (('a', 'failed'), ('b', 'running'), ('c', 'unknown')):
        path = root / (key * 32); path.mkdir(mode=0o700)
        (path / 'partial-content').write_text('an incomplete recovery copy')
        if state != 'unknown':
            atomic_json(c.jobs / (path.name + '.json'), {'operation': 'update', 'state': state})
    status = c.update_snapshot_status()
    assert status['snapshots'] == [{'id': 'a' * 32, 'complete': False, 'created_at': None, 'job_state': 'failed'}]
    c.remove_update_snapshot('a' * 32)
    assert not (root / ('a' * 32)).exists()
    for key in ('b', 'c'):
        with pytest.raises(HostError, match='not found'):
            c.remove_update_snapshot(key * 32)
        assert (root / (key * 32) / 'partial-content').is_file()


@pytest.mark.parametrize('installed,available,expected', [('10.0.0-beta.1','9.22.2',False),('10.0.0-beta.1','10.0.0',True),('10.0.0','10.0.0',False),('10.0.0','10.0.1',True)])
def test_beta_update_check_only_offers_newer_stable(controller, monkeypatch, installed, available, expected):
    release = controller.release(); release['version'] = installed
    atomic_json(controller.etc / 'release.json', release)
    text = f'VERSION={available}\nARCHIVE=david-pi-{available}.tar.gz\nARCHIVE_SHA256={"a"*64}\nIMAGE={release["image"]}\nDATA_SCHEMA_VERSION=1\nROLLBACK_MIN_DATA_SCHEMA=1\n'
    monkeypatch.setattr(host, 'read_https', lambda *args, **kwargs: text.encode())
    result = controller.update_check()
    assert result['update_available'] is expected
    if '-beta.' in installed and not expected:
        assert 'testing release' in result['message']


def test_beta_manifest_requires_explicit_opt_in(controller):
    version = '10.0.0-beta.1'
    text = f'VERSION={version}\nARCHIVE=david-pi-{version}.tar.gz\nARCHIVE_SHA256={"a"*64}\nIMAGE={controller.release()["image"]}\nDATA_SCHEMA_VERSION=1\nROLLBACK_MIN_DATA_SCHEMA=1\n'
    with pytest.raises(HostError, match='exact testing release'):
        host.parse_manifest(text, 'example/david-pi')
    assert host.parse_manifest(text, 'example/david-pi', allow_prerelease=True)['VERSION'] == version


def test_beta_with_legacy_stable_metadata_gets_recovery_guidance(controller, monkeypatch):
    release = controller.release(); release['version'] = '10.0.0-beta.1'
    atomic_json(controller.etc / 'release.json', release)
    monkeypatch.setattr(host, 'read_https', lambda *args, **kwargs: b'VERSION=9.22.2\n')
    result = controller.update_check()
    assert result['update_available'] is False
    assert 'metadata is not compatible' in result['message']
    release['version'] = '10.0.0'; atomic_json(controller.etc / 'release.json', release)
    with pytest.raises(HostError):
        controller.update_check()


def test_web_administrator_cannot_enroll_server_into_testing_channel(controller, monkeypatch):
    jobs = []
    monkeypatch.setattr(controller, 'enqueue', lambda *args: jobs.append(args))
    with pytest.raises(HostError, match='selected locally'):
        controller.dispatch('update', {'testing_version': '10.0.0-beta.2'}, 'john@example.test')
    assert not jobs
    controller.dispatch('update', {'testing_version': '10.0.0-beta.2'}, '', local=True)
    assert jobs == [('update', {'testing_version': '10.0.0-beta.2'})]


def test_explicit_testing_update_pins_manifest_and_refuses_downgrade(controller, monkeypatch):
    release = controller.release(); release['version'] = '10.0.0-beta.2'
    atomic_json(controller.etc / 'release.json', release)
    urls = []
    def fetch(url, **kwargs):
        urls.append(url)
        version = '10.0.0-beta.1'
        return f'VERSION={version}\nARCHIVE=david-pi-{version}.tar.gz\nARCHIVE_SHA256={"a"*64}\nIMAGE={release["image"]}\nDATA_SCHEMA_VERSION=1\nROLLBACK_MIN_DATA_SCHEMA=1\n'.encode()
    monkeypatch.setattr(host, 'read_https', fetch)
    assert controller.update_check('10.0.0-beta.1')['update_available'] is False
    assert '/releases/download/v10.0.0-beta.1/' in urls[0]
    with pytest.raises(HostError, match='does not match'):
        controller.update_check('10.0.0-beta.3')


def test_journal_full_after_snapshot_restarts_untouched_previous_release(snapshot_host, monkeypatch):
    c = snapshot_host
    old = c.release()
    archive_bytes = b'verified test archive placeholder'
    manifest = {'VERSION': '10.0.1', 'ARCHIVE': 'david-pi-10.0.1.tar.gz',
                'ARCHIVE_SHA256': hashlib.sha256(archive_bytes).hexdigest(),
                'IMAGE': old['image'], 'DATA_SCHEMA_VERSION': '2', 'ROLLBACK_MIN_DATA_SCHEMA': '1'}
    monkeypatch.setattr(c, 'update_check', lambda: {'update_available': True, 'manifest': manifest})
    monkeypatch.setattr(c, 'inspect_private_root', lambda origin: {})
    monkeypatch.setattr(c, 'set_private_root', lambda *args: None)
    class Download(io.BytesIO):
        url = 'https://github.com/example/david-pi/release'
    monkeypatch.setattr(host, 'https_open', lambda *args, **kwargs: Download(archive_bytes))
    def unpack(archive, destination):
        root = destination / 'david-pi-10.0.1'; root.mkdir(parents=True)
        (root / 'VERSION').write_text('10.0.1')
    monkeypatch.setattr(host, 'validate_archive', unpack)
    monkeypatch.setattr(c, 'snapshot', lambda path, job: c.docker('stop'))
    def phase(job, text):
        if text == 'applying release with writes paused':
            raise OSError('system disk full while recording phase')
    monkeypatch.setattr(c, 'phase', phase)
    monkeypatch.setattr(c, 'write_runtime', lambda *args: pytest.fail('Unchanged runtime must not need a rewrite to restart'))
    with pytest.raises(HostError, match='snapshot retained'):
        c.update({}, {'id': 'e' * 32})
    assert c.release() == old
    assert any('up' in command and '--wait' in command for command in c.calls)
    assert (Path(c.config()['storage']['data_root']) / 'mutable.txt').read_text() == 'before'
