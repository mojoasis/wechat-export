# 聊天记录如何获取，以及数据库结构浏览

## 获取链路

项目从 Windows 微信的本地数据库获取记录。主流程是：发现账号目录 → 取得数据库密钥 → 解密数据库副本 → 建立辅助索引 → 查询消息与媒体 → 通过 Flask 接口显示或导出。这里没有调用腾讯提供的聊天记录查询接口；日常浏览读取本地备份，内容更新需要重新备份。

```mermaid
flowchart LR
    A[本机微信 db_storage] --> B[匹配并验证数据库密钥]
    B --> C[解密为 SQLite 副本]
    C --> D[消息分片及联系人等数据库]
    D --> E[会话索引与消息解析]
    E --> F[Flask 页面及导出]
    D --> G[数据库结构与中文字段说明]
```

1. **发现账号和源数据库。** `engine.utils.find_all_wechat_data_dirs` 检测 `xwechat_files/<账号>/db_storage` 等路径。网页 `/api/backup/run` 调用 `backup.pipeline.run_backup`，按扫描、解密、媒体迁移、索引四个阶段执行。
2. **取得并校验密钥。** `key_scan.run_key_scan` 优先复用已有配置，再依次尝试 MMKV 配置、微信 4.1.10+ 的 WCDB `Config.Cipher` 只读内存扫描、API Hook、传统内存扫描等回退方式。代码确实包含 Hook 回退，但用途是取得密钥，聊天浏览仍走离线副本。`backup.decryptor._resolve_key` 优先按数据库首页 salt 匹配，辅以文件名与 HMAC 验证，避免不同账号同名库用错密钥。
3. **解密副本。** `engine.decrypt.decrypt_database` 使用 SQLCipher 4 风格布局：4096 字节页、AES-256-CBC、80 字节保留区和首页 HMAC-SHA512 校验，写出普通 SQLite 数据库。之后验证 `sqlite_master` 是否可读。一键备份可按源文件指纹复用已有副本，并在独立输出目录迁移媒体文件。
4. **建立会话索引。** `backup.indexer.build_index` 扫描 `message_*.db` 的 `Msg_*` 表，把会话统计、精简消息元数据和联系人信息放入工具生成的 `data/chats.db`。它是辅助索引，不是微信原始表结构的全集。
5. **读取聊天消息。** `/api/messages` 调用 `engine.services.message.query_messages`。给定 `chat_id`，`_find_all_chat_dbs` 计算 `MD5(chat_id)`，在全部消息分片中定位 `Msg_<MD5>` 表，合并该会话跨分片的记录，筛选、排序并分页。消息查询主要读取 `local_id`、`local_type`、`origin_source`、`create_time`、`status`、`message_content`、`real_sender_id` 和 `packed_info_data`。
6. **解析显示内容。** `_row_to_message` 结合消息类型处理文本、Zstandard 压缩数据、XML 和媒体引用。发送者优先通过当前分片的 `Name2Id.rowid → user_name` 映射，再用联系人备注 / 昵称显示；`origin_source` 不能单独可靠地判断“本人发送”。媒体解析还会用到 `hardlink.db`、资源库和已迁移的图片 / 视频 / 语音文件。

主要代码入口：

| 用途 | 文件与函数 |
|------|------------|
| 备份调度 | `src/backup/pipeline.py:run_backup` |
| 网页采集入口 | `src/web/routes/backup_api.py:backup_run` / `backup_decrypt` |
| 密钥获取 | `src/key_scan.py:run_key_scan` |
| 新版只读密钥扫描 | `src/engine/services/config_cipher_extract.py:extract_keys_via_config_cipher` |
| 数据库解密 | `src/engine/decrypt.py:decrypt_database` |
| 备份数据库选择 | `src/backup/decryptor.py:decrypt_for_backup` |
| 会话索引 | `src/backup/indexer.py:build_index` |
| 会话列表 | `src/engine/services/chat.py:get_contacts` |
| 消息查询 / 解码 | `src/engine/services/message/__init__.py:query_messages` / `_row_to_message` |
| 发送者映射 | `src/engine/services/sender_model.py:load_name2id` |

## 新增的数据库结构页面

访问仪表盘 → **数据库结构**，或打开 `/schema`。

- 数据库下拉列表递归发现当前备份 / 解密目录中的 `.db`，同时列出源 `DB_DIR` 中尚未出现在解密目录的数据库。单个坏库不会妨碍浏览其他库。
- 选定数据库后列出全部实际表和视图，包括不被现有聊天解析器识别的表、SQLite 内部表和 FTS 辅助表。支持按表名或中文说明搜索。
- 表详情显示真实字段名、声明类型、主键序号、NOT NULL 声明、默认值、生成 / 隐藏列、原始建表 SQL、索引和外键；可继续搜索字段名及中文含义。NOT NULL 一栏显示显式声明，不能单独当作所有 SQLite 主键空值语义的结论。
- 页面上方选择 **当前数据库 / 全部数据库**、**JSON / Markdown**，点击 **导出表结构**。导出包含完整结构及中文说明和依据，不受页面搜索筛选影响；全部库导出保留未解密 / 读取失败状态，其余库继续导出。
- 结构通过 `sqlite_master` 及 SQLite PRAGMA 读取，不扫描业务数据行。已知辅助库 `data/chats.db`、根目录 `chats.db` 和 `chat_search_index.db` 标为“工具生成”，不按目录笼统判定库来源。
- 网页“解密数据库”现在递归处理源目录全部 `.db`，保留相对目录；一键备份继续使用原有数据库白名单。缺少密钥的库会列为跳过，获取相应密钥后可再解密。

“所有表”指实际可访问数据库中存在的表；不同微信版本、账号和已解密范围会不同。仅知道加密库文件名无法读取其表结构，尚未解密的库会显示状态，不会虚构字段。

## 中文含义的依据

注释以代码内的小型映射和名称推测提供，不依赖联网模型，也不写回数据库。

| 标记 | 含义 | 例子 |
|------|------|------|
| 项目依据 | 项目现有解析代码已使用该表 / 字段，并标明来源；不是官方定义 | 消息表 `real_sender_id` 通过当前分片 `Name2Id` 映射发送者 |
| 名称推测 | 仅根据字段名解释，时间单位和枚举值等可能仍需确认 | 未识别表中的 `create_time` 推测为创建时间，单位待确认 |
| 未知 | 暂无足够依据，保留原名与类型 | 尚未识别的二进制字段 |

同名字段在不同表中不自动获得“项目依据”标记。例如任意未知表中的 `real_sender_id` 不会直接套用消息分片映射规则。新增已核实含义时，维护 `src/engine/services/schema.py` 中的 `_KNOWN` 上下文映射；未验证的解释放在名称推测映射中。

`contact.db` 已补充联系人与陌生人、公众号、群聊详情与成员、OpenIM 文案、操作记录、入群申请及凭据等表的中文说明。未被项目解析器验证的字段标为“名称推测”；例如 `delete_flag` 推测是删除状态，但不宣称某个数值一定表示删除；`member_id` 推测是成员编号，但与 `contact.id` 的关联仍待确认。不会将陌生人表的同名字段直接标为已验证。

消息排序与上传 / 下载状态、WCDB 编码或压缩辅助标记、业务账号消息、会话未读与草稿、FTS 内部辅助表也提供名称推测。`c2`、`c5`、预留编号字段等缺少可读语义时仍保留未知，避免用虚构解释掩盖实际不确定性。

注释针对项目当前读取的 **Windows 个人微信 4.x** 数据库。匹配条件同时限定库文件名和表名；实际字段、类型、约束仍以当前文件的 SQLite 元数据为准。表名中的版本号属于匹配条件，不能把某个 `_v4` 表的解释自动套到未来版本。

### 已有读取依据的表与关联

| 数据库 / 表 | 说明重点 | 项目依据 |
|------------|----------|----------|
| `message_N.db / Msg_<32位MD5>` | 会话消息、本地编号、秒级创建时间、正文与发送者映射；49 表示应用消息，包含链接、文件等 | `message/__init__.py`、`sender_model.py` |
| `contact.db / contact`、`chat_room`、`contact_label` | 联系人、群主、标签定义；扩展数据中的标签编号需关联标签表 | `chat.py`、`contact_extra.py`、`name_resolver.py` |
| `session.db / SessionTable` | 会话标识与消息摘要 | `chat.py:_load_sessions` |
| `media_N.db / VoiceInfo`、`Name2Id` | 语音二进制数据及所属会话；创建时间不是语音时长 | `media.py:_query_voice_blob` |
| `message_resource.db / MessageResourceInfo`、`ChatName2Id` | 按会话、消息本地编号及类型定位资源扩展数据 | `message/media_resolve.py:_lookup_resource_file_name` |
| `hardlink.db / image_hardlink_info_v4`、`video_hardlink_info_v4`、`file_hardlink_info_v4`、`dir2id` | 资源 MD5、文件名和目录映射；目录分量不一定是微信用户名 | `media.py`、`message/media_resolve.py` |
| `message_fts.db / name2id`、`message_fts_v4_N_content` | 已提取文本及其消息、会话、类型和时间定位信息；不把其他 FTS 辅助表当成同一结构 | `search_index.py`、`search.py` |
| `sns.db / SnsTimeLine` | 朋友圈记录标识、发布者与 XML 内容；动态识别的其他表仍保留未知项 | `sns.py` |

这些关联由项目读取代码建立，不一定在 SQLite 中声明了 `FOREIGN KEY`。结构页的“声明的外键”只展示 PRAGMA 实际返回的约束；返回空列表不表示表之间没有业务关联。

| 作用域 | 读取代码中的映射 |
|--------|------------------|
| 当前消息分片 | `Msg_*.real_sender_id → Name2Id.rowid → user_name`（发送者） |
| 当前媒体分片 | `VoiceInfo.chat_name_id → Name2Id.rowid → user_name`（会话） |
| 当前资源库 | `MessageResourceInfo.chat_id → ChatName2Id.rowid → user_name`（会话） |
| 当前全文检索库 | `message_fts_v4_N_content.c4 → name2id.rowid → username`（会话） |
| 当前硬链接库 | `*_hardlink_info_v4.dir1 / dir2 → dir2id.rowid → username`（目录分量） |

本地消息编号不能单独作为全局唯一标识：不同会话表、不同分片可能重复。跨表定位还需保留会话与分片上下文。protobuf 内的字段路径只作为扩展数据的解释，不会被显示成虚构的 SQLite 列；没有依据的时间单位、状态枚举和高位含义继续保留“待确认”。

### 社区资料的适用边界

已对照以下两篇文章：

- [企业微信 PC 端本地数据库结构中的巧妙设计](https://www.52pojie.cn/thread-2109667-1-1.html)：对象是企业微信，其 `user.db`、`conversation_table`、`message_lookup_table` 等不能直接套到本项目的个人微信。可借鉴其按库说明业务职责、明确逻辑关联的写法，具体性能推断不能仅凭表名或列定义确认。
- [实现微信聊天记录导出中 windows 环境下本地数据解析](https://www.52pojie.cn/thread-2125672-1-1.html)：对象是个人微信，许多字段说明对应第三方 `wcdb_api.dll` 返回的 JSON。JSON 中的字符串类型、hex 编码及 `is_send`、`sender_username`、`table_name` 等接口字段，不足以证明底层 SQLite 存在同名列或采用相同存储类型。

第二篇将 `local_type` 描述为低 32 位主类型、高 32 位子类型。项目消息读取器当前按低 16 位识别常见主类型，应用消息再解析 XML 子类型；两种取法在文中的示例上结果相同，不能据此确认全部版本的高位布局。注释明确说明项目当前做法，本次不改变消息解析规则。文章提醒的服务端大整数精度问题也需保留：若以后向浏览器提供完整 `server_id`，超过 JavaScript 安全整数范围的值应以字符串传递。

社区资料用于交叉核对，不会自动提升为“项目依据”。已知表 / 字段说明均能追溯到项目自身的读取代码，不依赖第三方 DLL。

## 读取与验证边界

结构接口将数据库路径限制在当前解密目录内，以只读方式打开；静态快照在没有非空 WAL 时使用 SQLite `immutable` 模式，避免生成辅助文件。存在非空 WAL 时保留 WAL 感知读取，以免遗漏尚在 WAL 中的已提交结构；SQLite 此时可能维护共享内存 / WAL 辅助文件，但不会改写数据库内容。

全量解密在写入之前检查输出目录边界和文件身份，拒绝输出指向源库的硬链接或目录链接。结构显示使用文本节点，表名、字段名和 SQL 中的特殊字符不会作为 HTML 执行。

自动化测试覆盖元数据、索引 / 外键、标识符转义、未知字段、路径越界、异常文件、WAL 中已提交的结构、静态快照无辅助文件写入、全量解密额外数据库及源文件硬链接保护。网页用人工构造的演示库验证；真实微信版本和密钥提取能力仍以实际环境为准。

结构导出直接作为下载响应返回，不在服务端额外保存文件。单个库在同一只读事务中读取完整元数据；全部库逐库读取，不保证多个库处于同一时刻。导出不包含数据行，会保留实际表名、字段名、默认值和建表 SQL。
