"""Full web decryption must also reach databases outside the backup whitelist."""
import sqlite3
import os

import pytest

from backup.decryptor import decrypt_for_backup
from engine import decrypt


def test_all_databases_reaches_session_and_unknown_subdirectories(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    for rel in ['message/message_0.db', 'session/session.db', 'other/deep/custom.db', 'other/no_key.db']:
        path = source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'encrypted')
    monkeypatch.setattr('backup.decryptor._resolve_key',
                        lambda keys, path, salts: None if path.endswith('no_key.db') else b'A' * 32)

    def decrypt_fixture(src, dst, key, **kwargs):
        with sqlite3.connect(dst) as conn:
            conn.execute('CREATE TABLE demo (value TEXT)')
        return True

    monkeypatch.setattr(decrypt, 'decrypt_database', decrypt_fixture)
    out = tmp_path / 'out'
    progress = []
    results, skipped, stats = decrypt_for_backup(
        str(source), str(out), {}, salt_keys={}, all_databases=True,
        on_progress=lambda message, value: progress.append(message))
    assert {p.relative_to(out).as_posix() for p in out.rglob('*.db')} == {
        'message/message_0.db', 'session/session.db', 'other/deep/custom.db',
    }
    assert len(results) == 3
    assert stats['decrypted'] == 3
    assert [s.replace('\\', '/') for s in skipped] == ['other/no_key.db']
    assert '跳过 1 ' in progress[-1]
    assert (source / 'session/session.db').read_bytes() == b'encrypted'


def test_full_decryption_cannot_overwrite_source(tmp_path):
    with pytest.raises(ValueError):
        decrypt_for_backup(str(tmp_path), str(tmp_path / 'output'), {}, salt_keys={}, all_databases=True)


def test_full_decryption_rejects_output_hardlinked_to_source(tmp_path):
    source = tmp_path / 'source'
    output = tmp_path / 'out'
    source.mkdir()
    output.mkdir()
    src = source / 'custom.db'
    src.write_bytes(b'encrypted source must survive')
    os.link(src, output / 'custom.db')
    with pytest.raises(ValueError):
        decrypt_for_backup(str(source), str(output), {}, salt_keys={}, all_databases=True)
    assert src.read_bytes() == b'encrypted source must survive'


def test_web_decryption_activates_all_databases_for_schema_browser(tmp_path, monkeypatch):
    from engine import config_file
    from web.app import create_app

    source = tmp_path / 'source'
    (source / 'other').mkdir(parents=True)
    (source / 'other/custom.db').write_bytes(b'encrypted fixture')
    output = tmp_path / 'decoded'
    monkeypatch.setattr(config_file, '_config_path', lambda: str(tmp_path / 'config.json'))
    config_file.set_db_keys({'other/custom.db': (b'A' * 32).hex()}, db_dir=str(source))
    monkeypatch.setattr('backup.decryptor._resolve_key', lambda *args: b'A' * 32)

    def decrypt_fixture(src, dst, key, **kwargs):
        with sqlite3.connect(dst) as conn:
            conn.execute('CREATE TABLE custom_table (create_time INTEGER)')
        return True

    monkeypatch.setattr(decrypt, 'decrypt_database', decrypt_fixture)
    app = create_app(str(tmp_path / 'old'), wxid='wxid_demo')
    client = app.test_client()
    response = client.post('/api/backup/decrypt', json={
        'db_dir': str(source), 'output_dir': str(output),
    }, buffered=True)
    assert response.status_code == 200
    assert (output / 'other/custom.db').is_file()
    assert app.config['DECRYPTED_DIR'] == str(output)
    assert app.config['DB_DIR'] == str(source)
    tables = client.get('/api/schema/tables?db=other/custom.db')
    assert tables.status_code == 200
    assert tables.get_json()['tables'][0]['name'] == 'custom_table'

