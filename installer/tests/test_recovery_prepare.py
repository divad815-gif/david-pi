"""Replacement recovery protocol with real file copies and SQLite fixtures.

Host services/block inventory are isolated fakes; native VM acceptance remains
separate. No test formats disks or changes host mounts/services.
"""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import uuid

import pytest

from installer import host, recovery

IMAGE = 'ghcr.io/example/david-pi@sha256:' + '1' * 64


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    source_root = tmp_path / 'verified-code'
    source_root.mkdir()
    version = '10.0.0-beta.1'
    (source_root / 'VERSION').write_text(version)
    manifest = f'VERSION={version}\nARCHIVE=david-pi-{version}.tar.gz\nARCHIVE_SHA256={"a"*64}\nIMAGE={IMAGE}\nDATA_SCHEMA_VERSION=1\nROLLBACK_MIN_DATA_SCHEMA=1\n'
    host.atomic_json(source_root / 'verified-release.json', {'manifest': manifest, 'repository': 'example/david-pi', 'selected_version': version})
    api = SimpleNamespace(**{name: getattr(host, name) for name in dir(host) if not name.startswith('__')})
    api.ROOT = source_root
    api.safe_path = lambda path, **kwargs: Path(path)
    status = {'BackendState': 'Running', 'Self': {'ID': 'replacement-node', 'UserID': 42, 'DNSName': 'john-pi-2.example.ts.net.'}, 'User': {'42': {'LoginName': 'john@example.test'}}}
    calls = []
    def runner(args, **kwargs):
        calls.append(args)
        return json.dumps(status) if args == ['tailscale', 'status', '--json'] else ''
    controller = host.Controller(tmp_path / 'etc', runner=runner)
    backup = tmp_path / 'backup'
    source = backup / 'point'
    source.mkdir(parents=True)
    data_parent = tmp_path / 'primary'
    data_parent.mkdir()
    target = data_parent / 'david-pi-data'
    installation = {'schema_version': 1, 'instance_id': str(uuid.uuid4()), 'display_name': 'John home', 'hostname': 'john-pi', 'public_url': 'https://john-pi.example.ts.net', 'timezone': 'America/Denver', 'country': 'US', 'members': [{'login': 'john@example.test', 'name': 'John', 'role': 'admin'}], 'storage': {'mode': 'folder', 'data_root': '/srv/original', 'backup_root': '/mnt/backup/david-pi-backups', 'update_snapshot_root': '/mnt/updates/points'}, 'modules': {'notes': 'enabled', 'chat': 'enabled'}, 'integrations': {'web_push': False}}
    release = recovery.verified_release(api)
    (source / 'data').mkdir()
    (source / 'secrets').mkdir()
    (source / 'data/.david-pi-storage').write_text(installation['instance_id'] + '\n')
    with sqlite3.connect(source / 'data/notes.db') as database:
        database.execute('CREATE TABLE notes (body TEXT)')
        database.execute('INSERT INTO notes VALUES (?)', ('synthetic preserved note',))
    (source / 'secrets/chat-master.key').write_bytes(b'synthetic-key-never-public-credential')
    host.atomic_json(source / 'installation.json', installation)
    host.atomic_json(source / 'release.json', release)
    files = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
    ownership = {str(p.relative_to(source / 'data')): [10001, 10001, 0o700 if p.is_dir() else 0o600] for p in [source / 'data', *(source / 'data').rglob('*')]}
    host.atomic_json(source / 'snapshot.json', {'complete': True, 'independent': True, 'instance_id': installation['instance_id'], 'files': files, 'data_ownership': ownership})
    host.atomic_json(controller.state / 'recovery.json', {'mode': 'recovery', 'admin': 'john@example.test', 'origin': 'https://john-pi-2.example.ts.net', 'hostname': 'john-pi-2', 'node_id': 'replacement-node', 'release': release})
    controller.private_origin = lambda origin: origin
    controller.inspect_private_root = lambda origin: {}
    def inspect(path):
        path = Path(path)
        is_backup = path.is_relative_to(backup)
        return {'uuid': 'backup-uuid' if is_backup else 'data-uuid', 'source': '/dev/vdc' if is_backup else '/dev/vdb', 'target': str(backup if is_backup else data_parent), 'fstype': 'ext4', 'fsroot': '/'}
    controller.inspect_storage = inspect
    controller.storage_devices = lambda: {'/dev/vdb': 'primary-physical', '/dev/vdc': 'backup-physical'}
    controller.provision_storage = lambda cfg: None
    controller.write_runtime = lambda cfg, image: None
    monkeypatch.setattr(recovery.os, 'chown', lambda *args: None)
    monkeypatch.setattr(recovery.shutil, 'disk_usage', lambda path: SimpleNamespace(total=10*1024**3, free=8*1024**3))
    return SimpleNamespace(api=api, controller=controller, source=source, target=target, calls=calls, status=status, cfg=installation, release=release)


def rehash(f):
    value = host.read_json(f.source / 'snapshot.json')
    value['files'] = {str(p.relative_to(f.source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in f.source.rglob('*') if p.is_file() and p.name != 'snapshot.json'}
    host.atomic_json(f.source / 'snapshot.json', value)


def test_restore_uses_verified_image_preserves_identity_keys_and_content(fixture):
    f = fixture
    before = (f.source / 'snapshot.json').read_bytes()
    result = recovery.restore(f.controller, f.api, f.source, f.target)
    cfg = f.controller.config()
    assert result['restored']
    assert cfg['instance_id'] == f.cfg['instance_id']
    assert cfg['members'] == f.cfg['members']
    assert cfg['public_url'] == 'https://john-pi-2.example.ts.net'
    assert cfg['storage']['backup_root'] is None
    assert cfg['storage']['update_snapshot_root'] is None
    assert (f.controller.etc / 'secrets/chat-master.key').read_bytes() == (f.source / 'secrets/chat-master.key').read_bytes()
    with sqlite3.connect(f.target / 'notes.db') as database:
        assert database.execute('SELECT body FROM notes').fetchone()[0] == 'synthetic preserved note'
    assert ['docker', 'pull', IMAGE] in f.calls
    assert not (f.controller.state / 'setup.json').exists()
    assert not (f.controller.state / 'restore-progress.json').exists()
    assert (f.source / 'snapshot.json').read_bytes() == before
    with pytest.raises(host.HostError, match='repair'):
        recovery.restore(f.controller, f.api, f.source, f.target)


@pytest.mark.parametrize('fault', ['inventory', 'checksum', 'ownership', 'image', 'identity', 'existing_content', 'existing_keys', 'setup_claim', 'node_account', 'same_drive', 'unknown_backup_device', 'low_space'])
def test_refusal_occurs_before_copying_or_pulling(fixture, fault, monkeypatch):
    f = fixture
    if fault == 'inventory':
        (f.source / 'data/unknown').write_text('unexpected')
    elif fault == 'checksum':
        (f.source / 'data/notes.db').write_bytes(b'corrupted')
    elif fault == 'ownership':
        m = host.read_json(f.source / 'snapshot.json'); m['data_ownership']['notes.db'][0] = 999
        host.atomic_json(f.source / 'snapshot.json', m)
    elif fault == 'image':
        host.atomic_json(f.source / 'release.json', {**f.release, 'image': 'ghcr.io/example/david-pi@sha256:'+'2'*64}); rehash(f)
    elif fault == 'identity':
        (f.source / 'data/.david-pi-storage').write_text(str(uuid.uuid4())); rehash(f)
    elif fault == 'existing_content':
        f.target.mkdir(); (f.target / 'keep').write_text('untouched')
    elif fault == 'existing_keys':
        (f.controller.etc / 'secrets').mkdir()
    elif fault == 'setup_claim':
        host.atomic_json(f.controller.state / 'setup.json', {'claimed': False})
    elif fault == 'node_account':
        f.status['User']['42']['LoginName'] = 'other@example.test'
    elif fault == 'same_drive':
        f.controller.storage_devices = lambda: {'/dev/vdb': 'same', '/dev/vdc': 'same'}
    elif fault == 'unknown_backup_device':
        f.controller.storage_devices = lambda: {'/dev/vdb': 'primary'}
    elif fault == 'low_space':
        monkeypatch.setattr(recovery.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1024**3-1))
    with pytest.raises(host.HostError):
        recovery.restore(f.controller, f.api, f.source, f.target)
    assert not any(call[:2] == ['docker', 'pull'] for call in f.calls)
    assert not f.controller.config_path.exists()
    assert not (f.target / 'notes.db').exists()


def test_missing_verified_metadata_refuses_before_any_service(fixture):
    f = fixture
    (f.api.ROOT / 'verified-release.json').unlink()
    with pytest.raises(host.HostError, match='verified installer'):
        recovery.preflight(f.controller, f.api)
    assert not f.calls


@pytest.mark.parametrize('selection', ['latest', '10.0.0-beta.2', '10.0.0-beta.01'])
def test_recovery_manifest_requires_explicit_exact_testing_selection(fixture, selection):
    path = fixture.api.ROOT / 'verified-release.json'
    value = host.read_json(path); value['selected_version'] = selection; host.atomic_json(path, value)
    with pytest.raises(host.HostError):
        recovery.verified_release(fixture.api)


def test_prepare_guides_https_without_creating_installation_or_claim(fixture, monkeypatch, capsys):
    f = fixture
    (f.controller.state / 'recovery.json').unlink()
    monkeypatch.setattr('builtins.input', lambda prompt: '')
    f.api.fresh_setup_private_root = lambda *args: ('https://john-pi-2.example.ts.net', f.status, {})
    result = recovery.prepare(f.controller, f.api, 'john@example.test', 'john-pi')
    assert result['hostname'] == 'john-pi-2'
    assert 'instance_id' not in result and 'token_hash' not in result
    assert not f.controller.config_path.exists()
    assert not (f.controller.state / 'setup.json').exists()
    assert ['systemctl', 'restart', 'david-pi-helper.service'] in f.calls
    assert not any('david-pi-portal.service' in call for call in f.calls)
    assert 'sudo david-pi restore' in capsys.readouterr().out


def test_resume_after_data_and_keys_commit_preserves_backup(fixture):
    f = fixture
    def interruption(cfg):
        raise OSError('synthetic interruption before config commit')
    f.controller.provision_storage = interruption
    with pytest.raises(host.HostError, match='resume this exact recovery'):
        recovery.restore(f.controller, f.api, f.source, f.target)
    assert f.target.is_dir() and (f.controller.etc / 'secrets/chat-master.key').is_file()
    assert not f.controller.config_path.exists()
    before = (f.target / 'notes.db').read_bytes()
    f.controller.provision_storage = lambda cfg: None
    assert recovery.restore(f.controller, f.api, f.source, f.target)['restored']
    assert (f.target / 'notes.db').read_bytes() == before


def test_resume_refuses_changed_existing_content(fixture):
    f = fixture
    f.controller.provision_storage = lambda cfg: (_ for _ in ()).throw(OSError('interrupted'))
    with pytest.raises(host.HostError):
        recovery.restore(f.controller, f.api, f.source, f.target)
    (f.target / 'notes.db').write_bytes(b'newer activity must survive')
    with pytest.raises(host.HostError, match='checksum changed'):
        recovery.restore(f.controller, f.api, f.source, f.target)
    assert (f.target / 'notes.db').read_bytes() == b'newer activity must survive'


def test_after_config_commit_restore_refuses_and_directs_to_repair(fixture):
    f = fixture
    f.controller.write_runtime = lambda cfg, image: (_ for _ in ()).throw(OSError('runtime interrupted'))
    with pytest.raises(host.HostError, match='configuration is saved'):
        recovery.restore(f.controller, f.api, f.source, f.target)
    with pytest.raises(host.HostError, match='run sudo david-pi repair'):
        recovery.restore(f.controller, f.api, f.source, f.target)


def test_recovery_web_is_terminal_only_and_has_no_claim(fixture):
    f = fixture
    handler = object.__new__(host.SetupHandler)
    handler.server = SimpleNamespace(controller=f.controller)
    handler.headers = {'Host': 'john-pi-2.example.ts.net', 'Tailscale-User-Login': 'john@example.test'}
    handler.path = '/'
    responses = []
    handler.send = lambda *args: responses.append(args)
    handler.do_GET()
    assert responses[0][0] == 503 and b'sudo david-pi restore' in responses[0][1]
    handler.setup_post()
    assert responses[-1][0] == 400
    assert 'new-home claim is unavailable' in responses[-1][1]['error']
    f.controller.save_config(f.cfg)
    handler.do_GET()
    assert b'sudo david-pi repair' in responses[-1][1]
    assert b'<code>sudo david-pi restore</code>' not in responses[-1][1]


@pytest.mark.parametrize('backup,option', [(True, 'ro'), (False, 'rw')])
def test_unmounted_selection_mounts_only_rechecked_uuid(fixture, monkeypatch, tmp_path, backup, option):
    f = fixture
    item = {'uuid': '12345678-abcd-abcd-abcd-123456789012', 'device': '/dev/vdz', 'device_id': 'selected-physical', 'label': 'Recovery fixture', 'size': 12*1024**3, 'parent': None}
    monkeypatch.setattr(recovery, 'inventory', lambda controller, api: [copy.deepcopy(item)])
    real_path = Path
    mount_base = tmp_path / 'isolated-mounts'
    mount_base.mkdir()
    monkeypatch.setattr(recovery, 'Path', lambda value: mount_base if value == '/mnt' else real_path(value))
    monkeypatch.setattr('builtins.input', lambda prompt: '1')
    f.controller.inspect_storage = lambda path: {'uuid': item['uuid']}
    parent, selected = recovery.select_drive(f.controller, f.api, backup=backup)
    assert parent.parent == mount_base
    assert selected == item
    assert f.calls == [['mount', '-t', 'ext4', '-o', option, 'UUID=' + item['uuid'], str(parent)]]


def test_changed_drive_is_rejected_before_mount(fixture, monkeypatch):
    f = fixture
    item = {'uuid': '12345678-abcd-abcd-abcd-123456789012', 'device': '/dev/vdz', 'device_id': 'selected-physical', 'label': 'Recovery fixture', 'size': 12*1024**3, 'parent': None}
    inventories = iter([[item], []])
    monkeypatch.setattr(recovery, 'inventory', lambda controller, api: next(inventories))
    monkeypatch.setattr('builtins.input', lambda prompt: '1')
    with pytest.raises(host.HostError, match='changed or disappeared'):
        recovery.select_drive(f.controller, f.api, backup=True)
    assert not f.calls


def test_inventory_omits_unsupported_and_duplicate_filesystems(fixture):
    f = fixture
    records = [{'name': '/dev/vdb', 'type': 'disk', 'fstype': 'ext4', 'uuid': '12345678-abcd-abcd-abcd-123456789012', 'mountpoints': [], 'size': 10*1024**3},
               {'name': '/dev/vdc', 'type': 'disk', 'fstype': 'ntfs', 'uuid': '87654321-abcd-abcd-abcd-123456789012', 'mountpoints': [], 'size': 10*1024**3}]
    f.controller.runner = lambda args, **kwargs: json.dumps({'blockdevices': records})
    assert [item['device'] for item in recovery.inventory(f.controller, f.api)] == ['/dev/vdb']
    records[1].update(fstype='ext4', uuid=records[0]['uuid'])
    assert not recovery.inventory(f.controller, f.api)


def test_explicit_local_update_snapshot_can_restore_to_nonoverlapping_same_filesystem(fixture):
    f = fixture
    manifest = host.read_json(f.source / 'snapshot.json')
    manifest['independent'] = False
    host.atomic_json(f.source / 'snapshot.json', manifest)
    f.controller.inspect_storage = lambda path: {'source': '/dev/vdb', 'uuid': 'same-filesystem', 'target': str(f.source.parent.parent), 'fstype': 'ext4', 'fsroot': '/'}
    assert recovery.restore(f.controller, f.api, f.source, f.target)['restored']
    assert (f.source / 'data/notes.db').read_bytes() == (f.target / 'notes.db').read_bytes()


def test_corrupt_snapshot_metadata_has_actionable_error(fixture):
    f = fixture
    (f.source / 'snapshot.json').write_text('invalid json')
    with pytest.raises(host.HostError, match='metadata cannot be read'):
        recovery.restore(f.controller, f.api, f.source, f.target)
    assert not f.target.exists()


def test_image_preparation_space_is_rechecked_before_copy(fixture, monkeypatch):
    f = fixture
    capacity = iter([SimpleNamespace(free=8*1024**3), SimpleNamespace(free=1024**3-1)])
    monkeypatch.setattr(recovery.shutil, 'disk_usage', lambda path: next(capacity))
    with pytest.raises(host.HostError, match='space changed during image preparation'):
        recovery.restore(f.controller, f.api, f.source, f.target)
    assert ['docker', 'pull', IMAGE] in f.calls
    assert not f.target.exists()
    assert not (f.controller.state / 'restore-progress.json').exists()


def interrupt_data_copy(f, monkeypatch):
    original = recovery.shutil.copytree
    interrupted = False
    def copying(source, destination, **kwargs):
        nonlocal interrupted
        if Path(source) == f.source / 'data' and not interrupted:
            interrupted = True
            # Real partial bytes in the journal-bound staging directory.
            content = (f.source / 'data/library.bin').read_bytes()
            (Path(destination) / 'library.bin').write_bytes(content[:len(content)//2])
            raise OSError('synthetic mid-copy interruption')
        return original(source, destination, **kwargs)
    monkeypatch.setattr(recovery.shutil, 'copytree', copying)


def add_library_file(f):
    (f.source / 'data/library.bin').write_bytes(b'fixture-library-' * 131072)
    rehash(f)
    manifest = host.read_json(f.source / 'snapshot.json')
    manifest['data_ownership']['library.bin'] = [10001, 10001, 0o600]
    host.atomic_json(f.source / 'snapshot.json', manifest)


def test_mid_copy_retry_reclaims_only_owned_stage_and_needs_one_copy(fixture, monkeypatch):
    f = fixture
    add_library_file(f)
    full_size = sum(p.stat().st_size for p in (f.source / 'data').rglob('*') if p.is_file())
    capacity = full_size + 1024**3 + 4096
    def available(path):
        allocated = sum(p.stat().st_size for p in f.target.parent.rglob('*') if p.is_file())
        return SimpleNamespace(free=capacity-allocated)
    monkeypatch.setattr(recovery.shutil, 'disk_usage', available)
    interrupt_data_copy(f, monkeypatch)
    with pytest.raises(host.HostError, match='resume this exact recovery'):
        recovery.restore(f.controller, f.api, f.source, f.target)
    journal = host.read_json(f.controller.state / 'restore-progress.json')
    stage = f.target.parent / ('.david-pi-restore-' + journal['stage_id'])
    assert stage.is_dir() and (stage / 'library.bin').stat().st_size > 0
    assert available(f.target.parent).free < full_size + 1024**3
    assert recovery.restore(f.controller, f.api, f.source, f.target)['restored']
    assert not list(f.target.parent.glob('.david-pi-restore-*'))
    assert (f.target / 'library.bin').read_bytes() == (f.source / 'data/library.bin').read_bytes()
    assert available(f.target.parent).free >= 1024**3


@pytest.mark.parametrize('change', ['unknown_file', 'changed_inode', 'symlink'])
def test_resume_preserves_unknown_or_replaced_staging(fixture, monkeypatch, change):
    f = fixture
    add_library_file(f)
    interrupt_data_copy(f, monkeypatch)
    with pytest.raises(host.HostError):
        recovery.restore(f.controller, f.api, f.source, f.target)
    journal = host.read_json(f.controller.state / 'restore-progress.json')
    stage = f.target.parent / ('.david-pi-restore-' + journal['stage_id'])
    if change == 'unknown_file':
        (stage / 'unrelated.txt').write_text('must survive')
    elif change == 'changed_inode':
        stage.rename(stage.with_name(stage.name + '-preserved'))
        stage.mkdir()
        (stage / 'library.bin').write_text('different folder')
    else:
        (stage / 'unrelated-link').symlink_to(f.source)
    before = sorted(str(p) for p in stage.rglob('*'))
    with pytest.raises(host.HostError, match='preserved'):
        recovery.restore(f.controller, f.api, f.source, f.target)
    assert stage.exists()
    assert sorted(str(p) for p in stage.rglob('*')) == before
    assert not f.target.exists()


def test_preparation_retries_https_failure_without_new_identity(fixture, monkeypatch):
    f = fixture
    (f.controller.state / 'recovery.json').unlink()
    monkeypatch.setattr('builtins.input', lambda prompt: '')
    attempts = 0
    def assign(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise host.HostError('HTTPS certificates are not enabled')
        return 'https://john-pi-2.example.ts.net', f.status, {}
    f.api.fresh_setup_private_root = assign
    with pytest.raises(host.HostError, match='certificates'):
        recovery.prepare(f.controller, f.api, 'john@example.test', 'john-pi')
    assert not (f.controller.state / 'setup.json').exists()
    assert not f.controller.config_path.exists()
    record = recovery.prepare(f.controller, f.api, 'john@example.test', 'john-pi')
    assert record['hostname'] == 'john-pi-2' and 'instance_id' not in record
    assert f.calls.count(['systemctl', 'restart', 'david-pi-helper.service']) == 2
