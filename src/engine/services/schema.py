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
        'local_type': '消息类型编码；项目按低 16 位识别主类型，如 1 文本、3 图片、34 语音、49 应用消息（链接、文件等）；高位编码含义需结合微信版本确认',
        'origin_source': '消息来源标记；不能单独据此判定是否本人发送',
        'create_time': '消息创建时间（Unix 秒级时间戳）',
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
        'extra_buffer': '联系人扩展 protobuf 数据；项目解析性别、签名、地区、手机号和标签编号，标签名称通过 contact_label.label_id_ 关联',
    }),
    'contact_label': ('联系人标签编号与名称映射表', 'contact_extra.py:load_labels / resolve_labels', {
        'label_id_': '联系人标签编号；与 contact.extra_buffer 中解析出的标签编号关联',
        'label_name_': '联系人标签名称',
    }),
    'chat_room': ('群聊信息表', 'chat.py:_load_room_owners / name_resolver.py:_load_chatroom_names', {
        'username': '群聊标识（通常以 @chatroom 结尾）',
        'owner': '群主的微信用户标识',
        'ext_buffer': '群聊扩展 protobuf 数据；项目从成员标识生成群名回退，或尝试提取可读名称，不能将其整体当作群名',
    }),
    'sessiontable': ('会话列表及摘要表', 'chat.py:_load_sessions', {
        'username': '会话对应的微信用户或群聊标识',
        'summary': '会话消息摘要',
    }),
    'chatname2id': ('媒体资源库的会话编号映射表', 'message/media_resolve.py:_lookup_resource_file_name', {
        'user_name': '会话对应的微信用户或群聊标识；本表 rowid 与 MessageResourceInfo.chat_id 对应',
    }),
    'messageresourceinfo': ('消息与媒体资源的关联表', 'message/media_resolve.py:_lookup_resource_file_name', {
        'chat_id': '资源库内的会话编号；关联当前库 ChatName2Id.rowid，不是消息分片的 Name2Id',
        'message_local_id': '消息本地编号；需与会话编号和消息类型组合查询',
        'message_local_type': '资源对应的消息类型；项目与会话编号、消息本地编号一同匹配',
        'packed_info': '媒体资源扩展 protobuf 数据；项目从中读取文件名哈希，再匹配 hardlink.db 的 file_name',
    }),
    'media_name2id': ('当前语音分片的会话编号映射表', 'media.py:_query_voice_blob', {
        'user_name': '会话对应的微信用户或群聊标识；本表 rowid 与 VoiceInfo.chat_name_id 对应',
    }),
    'voiceinfo': ('语音消息数据表', 'media.py:_query_voice_blob / _extract_voice_from_db', {
        'chat_name_id': '当前语音分片内的会话编号；关联本库 Name2Id.rowid',
        'create_time': '语音对应消息的创建时间（Unix 秒级时间戳）；项目与 local_id 一同查询',
        'local_id': '语音对应消息的本地编号；需结合创建时间和会话编号定位',
        'voice_data': '语音二进制数据；项目提取后保存为 SILK 文件供播放或转写',
    }),
    'hardlink_info': ('媒体文件哈希、文件名与目录编号映射表', 'media.py:_hardlink_rows_by_keys / message/media_resolve.py:_resolve_media_from_proto', {
        'md5': '媒体资源的哈希键；可能为 CDN MD5，与文件名中的文件 MD5 不一定相同',
        'file_name': '媒体文件名；项目也用文件名哈希前缀定位资源',
        'dir1': '媒体路径中的目录编号；关联当前库 dir2id.rowid，实际路径层级按媒体类型处理',
        'dir2': '媒体路径中的另一目录编号；关联当前库 dir2id.rowid，部分媒体类型不使用',
    }),
    'dir2id': ('媒体路径的目录编号映射表', 'message/media_resolve.py:_resolve_media_from_proto', {
        'username': '目录名称或路径片段；本表 rowid 与媒体映射表的 dir1、dir2 对应，不一定是微信用户标识',
    }),
    'fts_name2id': ('微信全文检索库的会话编号映射表', 'search_index.py:_read_fts_sessions', {
        'username': '检索记录对应的微信用户或群聊标识；本表 rowid 与 content 表 c4 对应',
    }),
    'fts_content': ('微信全文检索的文本内容表；项目读取该普通表以构建搜索语料', 'search_index.py:build_fts_ex / search.py:_execute_fallback', {
        'c0': '微信提取的可检索文本；与消息表中可能压缩的 message_content 不同',
        'c1': '检索记录对应消息的本地编号；需结合会话等上下文定位原消息',
        'c3': '检索记录中的消息类型；项目用于筛选消息主类型',
        'c4': '检索库内的会话编号；关联当前 message_fts.db 的 name2id.rowid',
        'c6': '检索记录对应消息的创建时间（Unix 秒级时间戳）',
    }),
    'snstimeline': ('朋友圈时间线表', 'sns.py:_row_content / parse_timeline_xml', {
        'tid': '朋友圈记录标识；项目作为时间线条目标识读取',
        'user_name': '朋友圈发布者的微信用户标识',
        'content': '朋友圈 TimelineObject XML；二进制内容可能为 UTF-8 文本或 Zstandard 压缩数据',
    }),
}

# These labels cover observed contact.db columns; only _KNOWN has reader evidence.
_CONTACT_FIELDS = {
    'id': '联系人本地编号；编号范围及与其他表的关系待确认',
    'username': '联系人用户名或会话标识；具体标识规则待确认',
    'local_type': '联系人本地类型标记；具体枚举待确认，不套用消息类型编码',
    'alias': '联系人别名或微信号；具体用途待确认',
    'encrypt_username': '编码后的联系人标识；编码或加密算法及用途待确认',
    'flag': '联系人标记位；各位含义待确认',
    'delete_flag': '联系人删除状态标记；具体枚举及软删除行为待确认',
    'verify_flag': '联系人认证或验证标记；具体枚举待确认',
    'remark': '联系人备注名',
    'remark_quan_pin': '联系人备注名的全拼检索值',
    'remark_pin_yin_initial': '联系人备注名的拼音首字母检索值',
    'nick_name': '联系人昵称',
    'pin_yin_initial': '联系人昵称的拼音首字母检索值',
    'quan_pin': '联系人昵称的全拼检索值',
    'big_head_url': '联系人大头像地址',
    'small_head_url': '联系人小头像地址',
    'head_img_md5': '头像摘要标记；摘要对象及是否直接对应文件 MD5 待确认',
    'chat_room_notify': '群聊通知设置标记；具体枚举待确认',
    'is_in_chat_room': '群聊归属或成员状态标记；取值与适用对象待确认',
    'description': '联系人描述或附加说明；具体业务用途待确认',
    'extra_buffer': '联系人扩展二进制数据；本表的序列化格式与内部字段待确认',
    'chat_room_type': '群聊类型标记；具体枚举待确认',
}
_CONTACT_GUESSES = {
    'contact': ('联系人信息表', _CONTACT_FIELDS),
    'stranger': ('陌生人或临时联系人信息表；与正式联系人的迁移规则待确认', _CONTACT_FIELDS),
    'biz_info': ('公众号或业务账号扩展信息表；业务类型划分待确认', {
        'id': '业务账号信息本地编号；与 contact.id 的关系待确认',
        'username': '业务账号的用户名或标识',
        'type': '业务账号类型标记；具体枚举待确认',
        'accept_type': '接收或接受类型标记；适用业务及枚举待确认',
        'child_type': '业务账号子类型标记；具体枚举待确认',
        'home_url': '业务账号主页地址',
        'version': '业务信息版本标记；编码规则待确认',
        'external_info': '业务账号外部扩展信息；格式待确认',
        'brand_info': '品牌或公众号信息；格式待确认',
        'brand_icon_url': '品牌或公众号图标地址',
        'brand_list': '品牌或公众号列表信息；格式待确认',
        'brand_flag': '品牌或公众号标记位；各位含义待确认',
        'belong': '业务账号归属信息；取值及关联目标待确认',
        'ext_buffer': '业务账号扩展数据；序列化格式待确认',
        'sync_version': '业务信息同步版本标记；更新规则待确认',
    }),
    'chat_room': ('群聊信息表', {
        'id': '群聊本地编号；与其他表中群编号的映射关系待确认',
    }),
    'chat_room_info_detail': ('群聊详细信息与群公告表', {
        'room_id_': '群聊本地编号；与 chat_room.id 的关联方式待确认',
        'username_': '群聊用户名或标识',
        'announcement_': '群公告文本或内容',
        'announcement_editor_': '群公告编辑者标识；具体标识格式待确认',
        'announcement_publish_time_': '群公告发布时间（时间单位待确认）',
        'chat_room_status_': '群聊状态标记；具体枚举待确认',
        'xml_announcement_': 'XML 形式的群公告信息；节点结构待确认',
        'ext_buffer_': '群聊详细信息扩展数据；序列化格式待确认',
    }),
    'chatroom_member': ('群聊与成员的编号关系表；关联目标待确认', {
        'room_id': '群聊编号；是否关联 chat_room.id 待确认',
        'member_id': '群成员编号；是否关联 contact.id 或其他编号表待确认',
    }),
    'contact_label': ('联系人标签编号与名称映射表', {
        'sort_order_': '联系人标签排序值；排序方向及取值规则待确认',
    }),
    'name2id': ('通讯录用户名与行编号映射表；不套用消息分片的发送者映射', {
        'username': '通讯录用户名或标识；行编号的使用方待确认',
    }),
    'encrypt_name2id': ('编码用户名与行编号映射表；编码及关联规则待确认', {
        'username': '编码用户名或标识；编码或加密算法及使用方待确认',
    }),
    'openim_acct_type': ('OpenIM 账号类型信息表；具体接入场景待确认', {
        'lang_id': '语言标识；具体编码规则待确认',
        'acc_type_id': 'OpenIM 账号类型标识；取值及关联关系待确认',
        'update_time': '账号类型信息更新时间（时间单位待确认）',
        'ext_buffer': '账号类型扩展数据；序列化格式待确认',
    }),
    'openim_appid': ('OpenIM 应用与账号类型信息表；关联规则待确认', {
        'lang_id': '语言标识；具体编码规则待确认',
        'app_id': 'OpenIM 应用标识',
        'acct_type_id': 'OpenIM 账号类型标识；与 openim_acct_type 的关联方式待确认',
        'update_time': '应用信息更新时间（时间单位待确认）',
        'ext_buffer': '应用信息扩展数据；序列化格式待确认',
    }),
    'openim_wording': ('OpenIM 多语言文案及拼音检索信息表', {
        'lang_id': '文案语言标识；具体编码规则待确认',
        'app_id': '文案所属 OpenIM 应用标识；关联方式待确认',
        'wording_id': '文案标识；取值含义待确认',
        'wording': '本地化文案文本',
        'pinyin': '文案拼音检索值；分词及格式待确认',
        'quan_pin': '文案全拼检索值',
        'update_time': '文案信息更新时间（时间单位待确认）',
        'ext_buffer': '文案扩展数据；序列化格式待确认',
    }),
    'oplog': ('通讯录操作日志或同步记录表；处理流程待确认', {
        'id': '操作记录本地编号',
        'buffer': '操作记录序列化数据；格式与操作类型待确认',
    }),
    'room_verify_application': ('群聊验证或入群申请记录表；具体流程待确认', {
        'msg_server_id': '申请关联的服务端消息编号；与消息表的关联方式待确认',
        'chatroom_name': '申请对应的群聊名称或标识；具体格式待确认',
        'hash_key': '申请记录的摘要或查找键；算法及生成规则待确认',
        'inviter_username': '邀请者的用户名或标识',
        'state': '申请处理状态；具体枚举待确认',
        'read_status': '申请阅读状态；具体枚举待确认',
        'data_json': '申请附加 JSON 信息；内部字段定义待确认',
        'update_time': '申请记录更新时间（时间单位待确认）',
    }),
    'sqlite_sequence': ('SQLite 自增编号计数表，不属于微信业务表', {
        'name': '使用 AUTOINCREMENT 的表名',
        'seq': '该表已记录的自增编号序列值',
    }),
    'stranger_ticket_info': ('陌生人凭据信息表；凭据用途待确认', {
        'id': '陌生人凭据记录编号；与 stranger.id 的关联方式待确认',
        'ticket': '陌生人相关凭据数据；格式、有效期及使用流程待确认',
    }),
    'ticket_info': ('联系人凭据信息表；凭据用途待确认', {
        'id': '联系人凭据记录编号；与 contact.id 的关联方式待确认',
        'ticket': '联系人相关凭据数据；格式、有效期及使用流程待确认',
    }),
}
_GUESSES = {
    'id': '记录编号', 'rowid': '行编号', 'username': '用户名或用户标识（具体格式待确认）',
    'user_name': '用户名或用户标识（具体格式待确认）', 'wxid': '微信用户标识', 'chat_id': '会话编号或标识（映射方式待确认）',
    'name': '名称', 'nick_name': '昵称', 'nickname': '昵称', 'remark': '备注',
    'alias': '别名（具体用途待确认）', 'display_name': '显示名称', 'owner': '所有者标识（具体对象待确认）',
    'create_time': '创建时间（时间单位待确认）', 'update_time': '更新时间（时间单位待确认）',
    'modify_time': '修改时间（时间单位待确认）', 'timestamp': '时间戳（单位待确认）',
    'local_id': '本地编号', 'server_id': '服务端编号', 'real_sender_id': '发送者编号（映射方式待确认）',
    'type': '类型标记', 'local_type': '本地类型标记', 'status': '状态标记（具体枚举待确认）',
    'flag': '标记位', 'flags': '标记位集合', 'content': '内容或序列化数据',
    'message_content': '消息内容', 'summary': '摘要', 'md5': 'MD5 摘要',
    'path': '路径', 'file_name': '文件名', 'file_size': '文件大小（单位待确认）',
    'size': '大小（单位待确认）', 'url': '资源地址', 'count': '数量',
    'message_count': '消息数量', 'is_group': '是否群聊',
    'extra_buffer': '扩展二进制数据', 'ext_buffer': '扩展二进制数据',
    'sort_seq': '排序序号；编码规则及是否包含时间信息待确认',
    'server_seq': '服务端顺序编号；与本地排序的对应规则待确认',
    'upload_status': '上传状态标记；具体枚举待确认',
    'download_status': '下载状态标记；具体枚举待确认',
    'source': '来源信息；存储格式及业务含义待确认',
    'origin_source': '原始来源标记；具体枚举待确认，不能单独判定发送方向',
    'compress_content': '可能为压缩内容或兼容字段；编码格式及当前版本是否使用待确认',
    'wcdb_ct_message_content': 'WCDB 内部消息内容编码或压缩辅助标记；具体枚举待确认',
    'wcdb_ct_source': 'WCDB 内部来源内容编码或压缩辅助标记；具体枚举待确认',
    'packed_info_data': '打包的扩展数据；序列化格式与内部字段待确认',
    'packed_info': '打包的扩展信息；序列化格式与内部字段待确认',
    'is_session': '是否用于会话的标记；具体取值及作用范围待确认',
    'session_id': '会话编号；所属编号表及映射方式待确认',
    'session_name': '会话名称或标识；具体格式待确认',
    'session_title': '会话显示标题',
    'message_id': '消息编号；本地或服务端编号规则待确认',
    'message_local_id': '消息本地编号；需结合所属会话与分片定位',
    'message_server_id': '消息服务端编号；与其他表的关联方式待确认',
    'sender_id': '发送者编号；所属编号表及映射方式待确认',
    'chat_name_id': '会话名称对应的本地编号；映射方式待确认',
    'message_create_time': '消息创建时间（时间单位待确认）',
    'db_time_stamp': '数据库时间戳或版本时间标记（时间单位待确认）',
    'start_local_id': '范围起点的本地编号；是否包含边界待确认',
    'end_local_id': '范围终点的本地编号；是否包含边界待确认',
    'data_index': '数据索引或位置编号；索引对象及单位待确认',
    'md5_hash': 'MD5 摘要相关的哈希或索引值；算法与对应对象待确认',
    '_rowid_': '显式行编号字段；是否对应 SQLite 隐式 rowid 需看建表 SQL',
    'seq': '顺序编号或序列值；具体用途待确认',
    'tid': '记录或时间线条目标识；编号规则待确认，不当作时间戳',
    'key': '配置或查找键；各键含义待确认',
    'value': '配置或查找键对应的值；格式待确认',
    'valueint64': '64 位整数值；具体含义取决于对应键',
    'valuedouble': '浮点数值；具体含义取决于对应键',
    'valuestdstr': '字符串值；具体含义取决于对应键',
    'valueblob': '二进制值；具体含义与格式取决于对应键',
    'tablename': '表名；对应表的使用规则待确认',
    'columns': '字段名称集合；存储格式及用途待确认',
    'version': '版本标记；编码及更新规则待确认',
    'search_key': '检索键或检索文本；生成规则待确认',
    'client_id': '客户端生成的标识；作用范围与生成规则待确认',
    'brand_user_name': '品牌或公众号的用户名标识',
    'group_id': '分组或群组编号；具体业务及关联方式待确认',
    'user_id': '用户编号；所属编号表及映射方式待确认',
    'bit_flag': '位标记集合；各位含义待确认',
    'head_img_url': '头像图像地址',
    'image_buffer': '图像二进制数据；编码格式待确认',
    'title': '标题', 'extra_info': '附加信息；存储格式待确认',
    'unread_count': '未读数量；统计范围与更新规则待确认',
    'unread_first_msg_srv_id': '首条未读消息的服务端编号；范围规则待确认',
    'unread_first_pat_msg_local_id': '首条未读拍一拍相关消息的本地编号；具体规则待确认',
    'unread_first_pat_msg_sort_seq': '首条未读拍一拍相关消息的排序序号；具体规则待确认',
    'is_hidden': '是否隐藏的标记；具体取值及隐藏条件待确认',
    'draft': '草稿内容或状态；存储格式待确认',
    'draft_data': '草稿数据；序列化格式待确认',
    'last_timestamp': '最近时间戳；对应事件与时间单位待确认',
    'sort_timestamp': '用于排序的时间戳（时间单位待确认）',
    'last_clear_unread_timestamp': '最近清除未读状态的时间戳（时间单位待确认）',
    'last_msg_locald_id': '最近消息的本地编号；按实际字段名保留，定位范围待确认',
    'last_msg_type': '最近消息的类型标记；具体枚举待确认',
    'last_msg_sub_type': '最近消息的子类型标记；具体枚举待确认',
    'last_msg_sender': '最近消息的发送者标识；映射方式待确认',
    'last_sender_display_name': '最近消息发送者的显示名称',
    'last_msg_ext_type': '最近消息的扩展类型标记；具体枚举待确认',
    'username_id': '用户名对应的本地编号；所属编号表待确认',
    'unread_stat': '未读统计数据；统计维度与存储格式待确认',
    'room_id': '群聊或房间编号；所属编号表及映射方式待确认',
    'member_id': '成员编号；所属编号表及映射方式待确认',
    'chat_name': '会话显示名称', 'owner_id': '所有者编号；关联方式待确认',
    'max_member_count': '最大成员数量；限制的适用范围待确认',
    'user_list': '用户列表；存储格式待确认',
    'profile_url': '资料页地址', 'add_member_url': '添加成员的入口地址',
    'time': '时间值（对应事件与时间单位待确认）',
    'last_update_time': '最近更新时间（时间单位待确认）',
    'msg_create_time': '消息创建时间（时间单位待确认）',
    'msg_local_id': '消息本地编号；需结合所属会话与分片定位',
    'svr_id': '服务端编号；编号对象及关联方式待确认',
    'delete_table_name': '删除记录对应的表名；删除行为待确认',
    'keyword': '检索关键词', 'query': '查询文本或查询条件',
    'score': '排序或匹配分值；计算规则待确认',
    'sort_order_': '排序值；方向及取值规则待确认',
    'package_id_': '资源包编号；与资源包表的关联方式待确认',
    'md5_': '资源 MD5 摘要；对应对象待确认',
    'type_': '类型标记；具体枚举待确认',
    'month_id': '月份或按月分组编号；编码规则待确认',
    'ui_type': '界面或展示类型标记；具体枚举待确认',
    'tips_uuid': '提示记录的唯一标识；生成规则待确认',
    'tips_content': '提示内容；存储格式待确认',
}
_TABLE_GUESSES = {
    'name2id': '名称与本地行编号映射表；编号使用方待确认',
    'sendername2id': '发送者名称与本地编号映射表；编号使用方待确认',
    'sqlite_sequence': 'SQLite 自增编号计数表，不属于微信业务表',
    'timestamp': '数据库时间戳或版本时间记录表；单位及用途待确认',
    'db_info': '数据库配置或元信息表；各配置键含义待确认',
    'table_info': '表状态或元信息记录表；具体用途待确认',
    'wcdb_builtin_compression_record': 'WCDB 内部压缩状态记录表；不属于聊天消息表',
    'deleteinfo': '删除记录或删除进度信息表；具体删除规则待确认',
    'deleteresinfo': '消息关联资源的删除记录表；删除流程待确认',
    'messagegrouptimeinfo': '消息分组及时间信息表；分组规则待确认',
    'messageresourcedetail': '消息媒体资源详细信息表；与资源主表的关联规则待确认',
    'ftsdeleteinfo': '全文检索删除记录或进度信息表',
    'ftsrange': '全文检索处理范围记录表；边界规则待确认',
    'sessiondeletetable': '会话删除记录表；删除规则待确认',
    'sessiondraft': '会话草稿表；草稿格式待确认',
    'sessionnocontactinfotable': '未匹配通讯录联系人的会话信息表',
    'sessionunreadlisttable_1': '会话未读记录列表；编号映射待确认',
    'sessionunreadstattable_1': '会话未读统计表；统计规则待确认',
    'sns_timeline': '朋友圈时间线表；内容格式需结合微信版本确认',
    'snsadtimeline': '朋友圈广告时间线表',
    'snsdraft': '朋友圈草稿记录表',
    'snserrormessage': '朋友圈错误消息记录表；错误枚举待确认',
    'snspublishtask': '朋友圈发布任务表；任务状态规则待确认',
    'snsnotevoice': '朋友圈笔记语音记录表；具体业务待确认',
    'snspendingdraftdeletiontable': '朋友圈待删除草稿记录表',
    'chat_group': '业务聊天的群组信息表；群组编号关系待确认',
    'user_info': '用户资料信息表；账号类型及编号关系待确认',
    'my_user_info': '当前账号的用户资料表；具体范围待确认',
    'chatbot_message_v1': '聊天机器人消息记录表；消息状态及方向规则待确认',
    'chatbot_session': '聊天机器人会话信息表',
    'chatbot_sort_info': '聊天机器人消息排序信息表；排序规则待确认',
    'search_dict_v1': '检索词典或检索候选记录表；词条来源待确认',
    'knonstoreemoticontable': '非商店来源的表情信息表；资源来源规则待确认',
    'kfavemoticonordertable': '收藏表情的排序信息表',
    'kcustomemoticonordertable': '自定义表情的排序信息表',
    'kexpressrecentuseeemoticontable': '最近使用表情的记录表',
    'kstoreemoticonpackagetable': '商店表情包的资料与安装状态表',
    'kstoreemoticonfilestable': '商店表情包中的文件信息表',
    'kstoreemoticoncaptionstable': '商店表情的多语言说明文字表',
    'fav_db_item': '收藏条目及同步状态表；收藏类型及状态枚举待确认',
    'fav_tag_db_item': '收藏标签定义表；标签编号规则待确认',
    'fav_bind_tag_db_item': '收藏条目与标签的绑定记录表；编号关联待确认',
    'config': '配置键值记录表；各配置项用途待确认',
    'buff': '缓冲或缓存信息表；数据用途与格式待确认',
    'resync_task': '重新同步任务表；任务选择与执行规则待确认',
    'sync_client_storage': '客户端同步状态的键值存储表',
    'revokemessage': '消息撤回记录表；撤回状态与消息关联方式待确认',
    'revokebatchmessage': '批量消息撤回记录表；批次规则待确认',
    'wcfinderlivestatus': '视频号直播状态记录表；状态枚举待确认',
    'wcfinderuserpage': '视频号用户页面信息表；内容格式待确认',
    'fmessagetable': '联系人验证或好友申请相关消息表；具体流程待确认',
    'ilink_voip': '互联语音或视频通话记录表；通话协议与状态待确认',
    'biz_pay_status': '业务消息付费或支付状态记录表；具体枚举待确认',
    'biz_subscribe_status': '业务账号或模板订阅状态表；订阅规则待确认',
    'transfertable': '转账关联消息及状态记录表；不据此判定资金结算结果',
    'redenvelopetable': '红包关联消息及状态记录表；不据此判定资金结算结果',
    'grouppaytable': '群收款或群付款账单关联消息表；具体业务规则待确认',
    'websearch_record': '网页搜索的历史记录表',
    'brand_search_record': '品牌或公众号搜索的历史记录表',
    'teenager_apply_access_agree_info': '青少年模式访问申请的同意记录表；权限规则待确认',
    'reddot': '未读提示红点信息表；展示规则待确认',
    'reddot_record': '红点提示的业务记录表；业务分类待确认',
    'reddot_last_notify': '红点提示的最近通知记录表',
    'searchrecent': '最近搜索及点击记录表',
    'forwardrecent': '最近转发目标或转发记录表；记录范围待确认',
    'handoff_remind_v0': '跨端接续提醒记录表；提醒触发规则待确认',
    'wacontact': '小程序关联联系人或应用信息表；具体用途待确认',
    'weappbizattrsyncbuffertablev02': '小程序业务属性的同步缓冲表；内容格式待确认',
    'file_checkpoint_v4': '文件资源处理检查点表；处理阶段与进度规则待确认',
    'video_checkpoint_v4': '视频资源处理检查点表；处理阶段与进度规则待确认',
    'talker_checkpoint_v4': '按会话记录的媒体处理检查点表；进度规则待确认',
    'head_image': '头像图像数据表；图像编码与缓存规则待确认',
    'imgrangev0': '图片检索的处理范围记录表；范围边界待确认',
    'imgtableinfo': '图片检索的表状态或元信息记录表',
    'snsmaintimelinebreakflag': '朋友圈主时间线的断点或连续性标记表',
    'snsusertimelinebreakflagv2': '朋友圈用户时间线的断点或连续性标记表',
    'snsmessage_tmp3': '朋友圈评论、互动或通知消息表；消息类别待确认',
    'snsignoreddataitem': '朋友圈已忽略条目记录表；忽略规则待确认',
    'snstopitem_1': '朋友圈顶部或置顶条目状态表；具体展示规则待确认',
    'third_app_icon': '第三方应用图标数据表；应用标识及图像格式待确认',
}
_FTS_FIELDS = {
    'k': 'FTS 配置键', 'v': 'FTS 配置值；含义取决于配置键',
    'block': 'FTS 索引内部数据块；二进制格式由检索引擎管理',
    'sz': 'FTS 文档各列的词元数量编码；不直接当作字节大小',
    'segid': 'FTS 索引段编号', 'term': 'FTS 索引词元或索引分隔键',
    'pgno': 'FTS 索引页定位值；编码由检索引擎管理',
    'rank': 'FTS 结果排序分值或排序配置隐藏列；具体用法待确认',
}
_FTS_TABLE_ROLES = {
    'config': '全文检索引擎的配置辅助表',
    'data': '全文检索引擎的索引数据块辅助表',
    'idx': '全文检索引擎的索引段及词元定位辅助表',
    'docsize': '全文检索引擎的文档词元数量辅助表',
    'content': '全文检索引擎的内容辅助表；匿名字段含义需另行确认',
    'aux': '全文检索相关辅助信息表；具体用途待确认',
}


def _annotation(db, table, column=None):
    basename = Path(db).name.lower()
    table_name = table.lower()
    kind = table_name
    if re.fullmatch(r'message_\d+\.db', basename):
        if re.fullmatch(r'Msg_[0-9a-f]{32}', table, re.I):
            kind = 'message'
        elif kind != 'name2id':
            kind = ''
    elif re.fullmatch(r'media_\d+\.db', basename):
        kind = {'name2id': 'media_name2id', 'voiceinfo': 'voiceinfo'}.get(kind, '')
    elif basename == 'message_resource.db':
        if kind not in ('chatname2id', 'messageresourceinfo'):
            kind = ''
    elif basename == 'message_fts.db':
        if re.fullmatch(r'message_fts_v4_\d+_content', kind):
            kind = 'fts_content'
        else:
            kind = 'fts_name2id' if kind == 'name2id' else ''
    elif basename == 'hardlink.db':
        if kind in ('image_hardlink_info_v4', 'video_hardlink_info_v4', 'file_hardlink_info_v4'):
            kind = 'hardlink_info'
        elif kind != 'dir2id':
            kind = ''
    elif not ((basename == 'contact.db' and kind in ('contact', 'chat_room', 'contact_label'))
              or (basename == 'session.db' and kind == 'sessiontable')
              or (basename == 'sns.db' and kind == 'snstimeline')):
        kind = ''
    known = _KNOWN.get(kind)
    if known and (column is None or column.lower() in known[2]):
        basis = known[1]
        if column and kind == 'message':
            basis = ('sender_model.py:load_name2id' if column.lower() == 'real_sender_id'
                     else 'message/__init__.py:query_messages / _row_to_message')
        elif column and kind == 'contact' and column.lower() == 'big_head_url':
            basis = 'address_book.py:_build_contact_select / _parse_contact_row'
        return {'comment': known[0] if column is None else known[2][column.lower()],
                'confidence': 'high', 'basis': '项目解析依据：engine/services/' + basis}
    contact = _CONTACT_GUESSES.get(table.lower()) if basename == 'contact.db' else None
    if contact and (column is None or column.lower() in contact[1]):
        return {'comment': contact[0] if column is None else contact[1][column.lower()],
                'confidence': 'inferred',
                'basis': '仅按 contact.db 的表名、字段名称及结构推测；项目暂无该含义的解析验证'}
    fts = basename in ('contact_fts.db', 'message_fts.db', 'favorite_fts.db') and 'fts' in table_name
    if column and fts and (column.lower() in _FTS_FIELDS or column.lower() == table_name):
        return {'comment': _FTS_FIELDS.get(column.lower(), 'FTS 虚拟表的同名隐藏控制列；具体操作见 SQLite FTS 定义'),
                'confidence': 'inferred',
                'basis': '按检索库中的表名、字段名称及 SQLite FTS 辅助结构推测；未验证微信私有实现'}
    if column and column.lower() in _GUESSES:
        return {'comment': _GUESSES[column.lower()], 'confidence': 'inferred',
                'basis': '仅按字段名称推测，需结合微信版本及实际数据确认'}
    if column is None:
        comment = _TABLE_GUESSES.get(table_name)
        if re.fullmatch(r'biz_message_\d+\.db', basename) and re.fullmatch(r'msg_[0-9a-f]{32}', table_name):
            comment = '业务账号会话消息表；表名哈希后缀与会话的对应规则待确认'
        elif fts:
            comment = _FTS_TABLE_ROLES.get(table_name.rsplit('_', 1)[-1], '全文检索相关表；索引内容及具体用途待确认')
        elif basename == 'sns.db' and re.fullmatch(r'feeds\w*', table_name):
            comment = '朋友圈动态或信息流记录表；具体字段含义需结合微信版本确认'
        if comment:
            return {'comment': comment, 'confidence': 'inferred',
                    'basis': '仅按表名及所在数据库推测，需结合微信版本及实际结构确认'}
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
                          'origin': 'tool' if rel.lower() in (
                              'data/chats.db', 'chats.db', 'chat_search_index.db') else 'wechat'})
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
        return _table_structure(conn, database, table)


def _table_structure(conn, database, table):
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


def database_structure(root, database):
    with closing(_connect(_database_path(root, database))) as conn:
        # Keep all metadata in one snapshot when the database changes during export.
        conn.execute('BEGIN')
        names = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') ORDER BY name").fetchall()
        return [_table_structure(conn, database, row['name']) for row in names]
