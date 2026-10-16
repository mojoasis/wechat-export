"""Read-only SQLite schema discovery and evidence-labelled Chinese annotations."""
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path


# These meanings come from the project's readers, not an official WeChat schema.
_KNOWN = {
    'message': ('聊天消息表，表名后缀为会话标识的 MD5', 'message/__init__.py:_find_all_chat_dbs', {
        'local_id': '消息在当前表中的本地编号；跨分片可能重复',
        'local_type': '消息类型及高位标记；低 16 位为类型，如 1 文本、3 图片、34 语音、49 链接',
        'origin_source': '消息来源标记；不能单独据此判定是否本人发送',
        'create_time': '消息创建时间（Unix 秒级时间戳）',
        'status': '消息状态；具体枚举含义尚未确定',
        'message_content': '消息正文；按消息类型可能为文本、XML 或 Zstandard 压缩数据',
        'real_sender_id': '发送者编号；按当前分片 Name2Id.rowid → user_name 解析',
        'packed_info_data': '消息扩展二进制数据；项目从中解析媒体路径等信息',
    }),
    'name2id': ('当前消息分片的用户编号映射表', 'sender_model.py:load_name2id', {
        'user_name': '微信用户标识；隐式 rowid 与消息 real_sender_id 对应',
    }),
    'contact': ('联系人信息表', 'chat.py:_load_contacts / contact_extra.py:parse_extra_buffer', {
        'id': '联系人在通讯录中的本地编号',
        'username': '微信用户或群聊标识',
        'remark': '联系人备注名',
        'nick_name': '微信昵称',
        'alias': '用户设置的微信号',
        'small_head_url': '小头像地址',
        'big_head_url': '大头像地址',
        'extra_buffer': '联系人扩展 protobuf 数据，包含性别、签名、地区、手机号和标签等',
    }),
    'chat_room': ('群聊信息表', 'chat.py:_load_room_owners / name_resolver.py:_load_chatroom_names', {
        'username': '群聊标识（通常以 @chatroom 结尾）',
        'owner': '群主的微信用户标识',
        'ext_buffer': '群聊扩展二进制数据；项目从中解析群名',
    }),
    'sessiontable': ('会话列表及摘要表', 'chat.py:_load_sessions', {
        'username': '会话对应的微信用户或群聊标识',
        'summary': '会话消息摘要',
    }),
}
_GUESSES = {
    'id': '记录编号', 'rowid': '行编号', 'username': '微信用户标识',
    'user_name': '微信用户标识', 'wxid': '微信用户标识', 'chat_id': '会话标识',
    'name': '名称', 'nick_name': '昵称', 'nickname': '昵称', 'remark': '备注',
    'alias': '别名或微信号', 'display_name': '显示名称', 'owner': '所有者或群主',
    'create_time': '创建时间（时间单位待确认）', 'update_time': '更新时间（时间单位待确认）',
    'modify_time': '修改时间（时间单位待确认）', 'timestamp': '时间戳（单位待确认）',
    'local_id': '本地编号', 'server_id': '服务端编号', 'real_sender_id': '发送者编号（映射方式待确认）',
    'type': '类型标记', 'local_type': '本地类型标记', 'status': '状态标记',
    'flag': '标记位', 'flags': '标记位集合', 'content': '内容或序列化数据',
    'message_content': '消息内容', 'summary': '摘要', 'md5': 'MD5 摘要',
    'path': '路径', 'file_name': '文件名', 'file_size': '文件大小（单位待确认）',
    'size': '大小（单位待确认）', 'url': '资源地址', 'count': '数量',
    'message_count': '消息数量', 'is_group': '是否群聊',
    'extra_buffer': '扩展二进制数据', 'ext_buffer': '扩展二进制数据',
}


def _annotation(db, table, column=None):
    basename = Path(db).name.lower()
    kind = table.lower()
    if re.fullmatch(r'message_\d+\.db', basename):
        if re.fullmatch(r'Msg_[0-9a-f]{32}', table, re.I):
            kind = 'message'
        elif kind != 'name2id':
            kind = ''
    elif not ((basename == 'contact.db' and kind in ('contact', 'chat_room'))
              or (basename == 'session.db' and kind == 'sessiontable')):
        kind = ''
    known = _KNOWN.get(kind)
    if known and (column is None or column.lower() in known[2]):
        basis = known[1]
        if column and kind == 'message':
            basis = ('sender_model.py:load_name2id' if column.lower() == 'real_sender_id'
                     else 'message/__init__.py:query_messages / _row_to_message')
        return {'comment': known[0] if column is None else known[2][column.lower()],
                'confidence': 'high', 'basis': '项目解析依据：engine/services/' + basis}
    if column and column.lower() in _GUESSES:
        return {'comment': _GUESSES[column.lower()], 'confidence': 'inferred',
                'basis': '仅按字段名称推测，需结合微信版本及实际数据确认'}
    return {'comment': '未知含义，待确认', 'confidence': 'unknown',
            'basis': '项目暂无该字段的解析依据' if column else '项目暂无该表的解析依据'}


def _db_files(root):
    if not root or not Path(root).is_dir():
        return {}
    base = Path(root).resolve()
    found = {}
    for directory, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if not Path(directory, d).is_symlink())
        for name in sorted(files):
            path = Path(directory, name)
            if path.suffix.lower() == '.db' and path.resolve().is_relative_to(base):
                found[path.relative_to(base).as_posix()] = path
    return found


def list_databases(decrypted_dir, source_dir=None):
    local = _db_files(decrypted_dir)
    source = _db_files(source_dir)
    databases = []
    for rel in sorted(local.keys() | source.keys(), key=str.casefold):
        status = 'not_decrypted'
        error = ''
        if rel in local:
            try:
                with local[rel].open('rb') as stream:
                    ready = stream.read(16) == b'SQLite format 3\x00'
                status = 'ready' if ready else 'unreadable'
                if not ready:
                    error = '不是可读的 SQLite 文件：可能尚未解密、文件为空或已损坏'
            except OSError:
                status, error = 'unreadable', '数据库文件无法访问'
        databases.append({'path': rel, 'status': status, 'error': error,
                          'origin': 'tool' if rel.startswith('data/') else 'wechat'})
    return {'decrypted_dir': decrypted_dir or '', 'source_dir': source_dir or '',
            'databases': databases}


def _database_path(root, relative):
    if not relative or '\x00' in relative:
        raise ValueError('请选择数据库')
    rel = Path(relative)
    if rel.is_absolute() or rel.drive or '..' in rel.parts or rel.suffix.lower() != '.db':
        raise ValueError('数据库路径无效')
    if not root:
        raise FileNotFoundError('尚未配置解密目录，请先备份或解密数据库')
    base = Path(root).resolve()
    path = (base / rel).resolve()
    if not path.is_relative_to(base):
        raise ValueError('数据库路径超出当前解密目录')
    if not path.is_file():
        raise FileNotFoundError('数据库尚未解密或已不存在，请先解密后刷新')
    return path


def _connect(path):
    wal = Path(str(path) + '-wal')
    # Stable snapshots need no sidecars; an existing WAL must remain visible.
    options = '?mode=ro' if wal.exists() and wal.stat().st_size else '?mode=ro&immutable=1'
    conn = sqlite3.connect(path.as_uri() + options, uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def list_tables(root, database):
    with closing(_connect(_database_path(root, database))) as conn:
        rows = conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY name").fetchall()
        return {'database': database, 'tables': [
            {**dict(row), **_annotation(database, row['name'])} for row in rows
        ]}


def table_structure(root, database, table):
    if not table or '\x00' in table:
        raise ValueError('请选择数据表')
    with closing(_connect(_database_path(root, database))) as conn:
        row = conn.execute("SELECT name, type, sql FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)).fetchone()
        if row is None:
            raise FileNotFoundError('数据表不存在，请刷新列表')
        columns = [{
            'cid': col['cid'], 'name': col['name'], 'type': col['type'],
            'not_null': bool(col['notnull']), 'default': col['dflt_value'],
            'primary_key': col['pk'], 'hidden': col['hidden'],
            **_annotation(database, table, col['name']),
        } for col in conn.execute(f'PRAGMA table_xinfo({_quote(table)})')]
        indexes = []
        for idx in conn.execute(f'PRAGMA index_list({_quote(table)})').fetchall():
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (idx['name'],)).fetchone()
            indexes.append({
                'name': idx['name'], 'unique': bool(idx['unique']), 'partial': bool(idx['partial']),
                'origin': idx['origin'], 'sql': sql['sql'] if sql else None,
                'columns': [dict(c) for c in conn.execute(f'PRAGMA index_xinfo({_quote(idx["name"])})')],
            })
        return {**dict(row), 'database': database, **_annotation(database, table),
                'columns': columns, 'indexes': indexes,
                'foreign_keys': [dict(fk) for fk in conn.execute(f'PRAGMA foreign_key_list({_quote(table)})')]}
