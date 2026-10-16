"""Schema browsing uses real SQLite metadata without returning message rows."""
import hashlib
import sqlite3

import pytest

from web.app import create_app


@pytest.fixture
def schema_env(tmp_path):
    root = tmp_path / 'decrypted'
    source = tmp_path / 'db_storage'
    (root / 'message').mkdir(parents=True)
    (source / 'session').mkdir(parents=True)
    (source / 'message').mkdir()
    db = root / 'message' / 'message_0.db'
    with sqlite3.connect(db) as conn:
        conn.executescript('''
            CREATE TABLE Name2Id (user_name TEXT);
            CREATE TABLE Msg_0123456789abcdef0123456789abcdef (
                local_id INTEGER PRIMARY KEY,
                local_type INTEGER NOT NULL DEFAULT 1,
                real_sender_id INTEGER REFERENCES Name2Id(rowid),
                message_content BLOB,
                mystery BLOB
            );
            CREATE INDEX msg_type ON Msg_0123456789abcdef0123456789abcdef(local_type);
            INSERT INTO Msg_0123456789abcdef0123456789abcdef
                (local_id, message_content) VALUES (1, 'PRIVATE_MESSAGE_TOKEN');
            CREATE TABLE "odd""name" ("x""y" TEXT DEFAULT 'hello',
                doubled TEXT GENERATED ALWAYS AS ("x""y" || "x""y") VIRTUAL);
            CREATE VIEW msg_view AS SELECT local_id FROM Msg_0123456789abcdef0123456789abcdef;
        ''')
    (source / 'message' / 'message_0.db').write_bytes(b'encrypted source')
    (source / 'session' / 'session.db').write_bytes(b'encrypted source')
    (root / 'broken.db').write_bytes(b'not a sqlite database')
    (root / 'ignore.db-wal').write_bytes(b'wal')
    app = create_app(str(root), wxid='wxid_demo', db_dir=str(source))
    app.config['TESTING'] = True
    return app, root, source


def test_inventory_includes_nested_and_not_decrypted_databases(schema_env):
    app, root, source = schema_env
    response = app.test_client().get('/api/schema/databases')
    assert response.status_code == 200
    data = response.get_json()
    databases = {d['path']: d for d in data['databases']}
    assert set(databases) == {'message/message_0.db', 'session/session.db', 'broken.db'}
    assert databases['message/message_0.db']['status'] == 'ready'
    assert databases['session/session.db']['status'] == 'not_decrypted'
    assert databases['broken.db']['status'] == 'unreadable'
    assert data['decrypted_dir'] == str(root)
    assert data['source_dir'] == str(source)


def test_tables_include_views_and_unknown_tables(schema_env):
    app, _, _ = schema_env
    response = app.test_client().get('/api/schema/tables', query_string={'db': 'message/message_0.db'})
    assert response.status_code == 200
    tables = {t['name']: t for t in response.get_json()['tables']}
    assert {'Name2Id', 'Msg_0123456789abcdef0123456789abcdef', 'odd"name', 'msg_view'} == set(tables)
    assert tables['msg_view']['type'] == 'view'
    assert '消息' in tables['Msg_0123456789abcdef0123456789abcdef']['comment']


def test_structure_reports_constraints_indexes_and_evidence(schema_env):
    app, root, _ = schema_env
    db = root / 'message' / 'message_0.db'
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    response = app.test_client().get('/api/schema/table', query_string={
        'db': 'message/message_0.db', 'table': 'Msg_0123456789abcdef0123456789abcdef',
    })
    assert response.status_code == 200
    data = response.get_json()
    cols = {c['name']: c for c in data['columns']}
    assert cols['local_id']['primary_key'] == 1
    assert cols['local_type']['type'] == 'INTEGER'
    assert cols['local_type']['not_null'] is True
    assert cols['local_type']['default'] == '1'
    assert cols['real_sender_id']['confidence'] == 'high'
    assert 'Name2Id' in cols['real_sender_id']['comment']
    assert 'sender_model.py' in cols['real_sender_id']['basis']
    assert cols['mystery']['confidence'] == 'unknown'
    assert '未知' in cols['mystery']['comment']
    assert data['indexes'][0]['name'] == 'msg_type'
    assert data['indexes'][0]['columns'][0]['name'] == 'local_type'
    assert data['foreign_keys'][0]['table'] == 'Name2Id'
    assert 'CREATE TABLE' in data['sql']
    assert 'PRIVATE_MESSAGE_TOKEN' not in response.get_data(as_text=True)
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_quoted_identifiers_and_generated_columns(schema_env):
    app, _, _ = schema_env
    response = app.test_client().get('/api/schema/table', query_string={
        'db': 'message/message_0.db', 'table': 'odd"name',
    })
    assert response.status_code == 200
    cols = {c['name']: c for c in response.get_json()['columns']}
    assert cols['x"y']['default'] == "'hello'"
    assert cols['doubled']['hidden'] == 2


@pytest.mark.parametrize('db', ['../outside.db', '/outside.db', 'C:\\outside.db', 'message/../../outside.db', 'ignore.db-wal'])
def test_rejects_paths_outside_database_root(schema_env, db):
    app, _, _ = schema_env
    response = app.test_client().get('/api/schema/tables', query_string={'db': db})
    assert response.status_code == 400


def test_missing_corrupt_and_unselected_databases_do_not_create_files(schema_env):
    app, root, _ = schema_env
    client = app.test_client()
    assert client.get('/api/schema/tables').status_code == 400
    assert client.get('/api/schema/tables?db=missing.db').status_code == 404
    assert client.get('/api/schema/tables?db=broken.db').status_code == 422
    assert client.get('/api/schema/table?db=message/message_0.db&table=missing').status_code == 404
    assert not (root / 'missing.db').exists()


def test_field_name_guesses_are_not_claimed_as_verified(schema_env):
    app, root, _ = schema_env
    with sqlite3.connect(root / 'unknown.db') as conn:
        conn.execute('CREATE TABLE unknown (create_time INTEGER, real_sender_id INTEGER)')
    data = app.test_client().get('/api/schema/table?db=unknown.db&table=unknown').get_json()
    cols = {c['name']: c for c in data['columns']}
    assert cols['create_time']['confidence'] == 'inferred'
    assert cols['real_sender_id']['confidence'] != 'high'


@pytest.mark.parametrize('database,table,fields,basis', [
    ('contact/contact.db', 'contact_label',
     {'label_id_': 'extra_buffer', 'label_name_': '标签'}, 'contact_extra.py'),
    ('message/message_resource.db', 'ChatName2Id',
     {'user_name': 'MessageResourceInfo.chat_id'}, 'message/media_resolve.py'),
    ('message/message_resource.db', 'MessageResourceInfo',
     {'chat_id': 'ChatName2Id.rowid', 'message_local_id': '本地编号',
      'message_local_type': '消息类型', 'packed_info': 'protobuf'}, 'message/media_resolve.py'),
    ('message/media_0.db', 'Name2Id',
     {'user_name': 'VoiceInfo.chat_name_id'}, 'media.py'),
    ('message/media_12.db', 'VoiceInfo',
     {'chat_name_id': 'Name2Id.rowid', 'create_time': '秒',
      'local_id': '本地编号', 'voice_data': 'SILK'}, 'media.py'),
    ('hardlink/hardlink.db', 'image_hardlink_info_v4',
     {'md5': '文件名', 'file_name': '文件名',
      'dir1': 'dir2id.rowid', 'dir2': 'dir2id.rowid'}, 'media.py'),
    ('hardlink/hardlink.db', 'video_hardlink_info_v4',
     {'dir1': 'dir2id.rowid'}, 'media.py'),
    ('hardlink/hardlink.db', 'file_hardlink_info_v4',
     {'dir2': 'dir2id.rowid'}, 'media.py'),
    ('hardlink/hardlink.db', 'dir2id', {'username': '目录'}, 'message/media_resolve.py'),
    ('message/message_fts.db', 'name2id',
     {'username': 'c4'}, 'search_index.py'),
    ('message/message_fts.db', 'message_fts_v4_12_content',
     {'c0': '文本', 'c1': '本地编号', 'c3': '消息类型',
      'c4': 'name2id.rowid', 'c6': '秒'}, 'search_index.py'),
    ('sns/sns.db', 'SnsTimeLine',
     {'tid': '朋友圈', 'user_name': '发布者', 'content': 'XML'}, 'sns.py'),
])
def test_annotations_use_each_database_own_relations(tmp_path, database, table, fields, basis):
    from engine.services.schema import list_tables, table_structure

    db = tmp_path / database
    db.parent.mkdir(parents=True)
    columns = ', '.join(f'"{name}" BLOB' for name in [*fields, 'unparsed_field'])
    with sqlite3.connect(db) as conn:
        conn.execute(f'CREATE TABLE "{table}" ({columns})')
    data = table_structure(str(tmp_path), database, table)
    assert list_tables(str(tmp_path), database)['tables'][0]['confidence'] == 'high'
    assert data['confidence'] == 'high'
    for col in data['columns']:
        if col['name'] == 'unparsed_field':
            assert col['confidence'] == 'unknown'
        else:
            assert col['confidence'] == 'high'
            assert fields[col['name']] in col['comment']
            assert basis in col['basis']


@pytest.mark.parametrize('database,table,column', [
    ('unknown.db', 'contact_label', 'label_id_'),
    ('contact.db', 'MessageResourceInfo', 'chat_id'),
    ('message_0.db', 'ChatName2Id', 'user_name'),
    ('message_resource.db', 'Name2Id', 'user_name'),
    ('media_backup.db', 'VoiceInfo', 'voice_data'),
    ('media_1.db', 'Name2Id', 'username'),
    ('message_fts.db', 'name2id', 'user_name'),
    ('message_fts.db', 'message_fts_v4_X_content', 'c0'),
    ('message_fts.db', 'message_fts_v4_1_content', 'c2'),
    ('message_fts.db', 'message_fts_v4_1_content', 'c5'),
    ('message_0.db', 'message_fts_v4_1_content', 'c0'),
    ('hardlink.db', 'voice_hardlink_info_v4', 'dir1'),
    ('hardlink.db', 'image_hardlink_info_v4', 'file_size'),
    ('sns_archive.db', 'SnsTimeLine', 'content'),
    ('sns.db', 'SnsTimeLine', 'create_time'),
])
def test_annotations_do_not_promote_wrong_context_or_unread_fields(tmp_path, database, table, column):
    from engine.services.schema import table_structure

    with sqlite3.connect(tmp_path / database) as conn:
        conn.execute(f'CREATE TABLE "{table}" ("{column}" BLOB)')
    col = table_structure(str(tmp_path), database, table)['columns'][0]
    assert col['confidence'] != 'high'
    assert not col['basis'].startswith('项目解析依据')


def test_empty_root_and_page_entry(tmp_path):
    app = create_app(str(tmp_path / 'absent'), wxid='wxid_demo')
    client = app.test_client()
    response = client.get('/api/schema/databases')
    assert response.status_code == 200
    assert response.get_json()['databases'] == []
    assert client.get('/schema').status_code == 200
    assert 'href="/schema"' in client.get('/').get_data(as_text=True)


def test_checkpointed_wal_snapshot_does_not_create_sidecar_files(tmp_path):
    from engine.services.schema import list_tables
    db = tmp_path / 'snapshot.db'
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE snapshot_table (value TEXT)')
    conn.close()
    before = set(tmp_path.iterdir())
    assert list_tables(str(tmp_path), 'snapshot.db')['tables'][0]['name'] == 'snapshot_table'
    assert set(tmp_path.iterdir()) == before


def test_schema_committed_only_in_wal_is_visible(tmp_path):
    from engine.services.schema import list_tables
    db = tmp_path / 'live.db'
    conn = sqlite3.connect(db)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE latest_schema (value TEXT)')
        conn.commit()
        assert (tmp_path / 'live.db-wal').stat().st_size > 0
        assert list_tables(str(tmp_path), 'live.db')['tables'][0]['name'] == 'latest_schema'
    finally:
        conn.close()

