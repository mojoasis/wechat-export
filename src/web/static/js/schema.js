/* Schema metadata only; database names and SQL always render as text. */
(() => {
  const el = id => document.getElementById('schema-' + id);
  const labels = { high: '项目依据', inferred: '名称推测', unknown: '未知' };
  const statuses = { ready: '可读取', not_decrypted: '未解密', unreadable: '不可读取' };
  let databases = [], tables = [], columns = [], selectedTable = '', revision = 0;
  let exporting = false, refreshing = false;

  function node(tag, text, className) {
    const result = document.createElement(tag);
    result.textContent = text == null ? '' : String(text);
    if (className) result.className = className;
    return result;
  }

  async function get(path, params) {
    const response = await fetch('/api/schema/' + path + (params ? '?' + new URLSearchParams(params) : ''));
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '读取失败，请刷新重试');
    return data;
  }

  function clear(message) {
    el('structure').hidden = true;
    el('status').textContent = message;
  }

  function updateExport() {
    const ready = databases.some(d => d.path === el('database').value && d.status === 'ready');
    el('export').disabled = exporting || refreshing || (el('export-scope').value === 'all' ? !databases.length : !ready);
  }

  async function exportSchema() {
    if (el('export').disabled || exporting) return;
    const format = el('export-format').value;
    const scope = el('export-scope').value;
    const params = { format, scope };
    if (scope === 'current') params.db = el('database').value;
    exporting = true;
    updateExport();
    el('export').textContent = '正在导出…';
    el('export-status').textContent = '正在读取结构；全部数据库较多时请稍候…';
    try {
      const response = await fetch('/api/schema/export?' + new URLSearchParams(params));
      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || '导出失败，请重试');
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = 'database-schema.' + (format === 'markdown' ? 'md' : 'json');
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      el('export-status').textContent = '结构文件已生成，已交给浏览器下载。';
    } catch (error) {
      el('export-status').textContent = '导出失败：' + error.message;
    } finally {
      exporting = false;
      el('export').textContent = '导出表结构';
      updateExport();
    }
  }

  function renderTables() {
    const query = el('table-search').value.trim().toLowerCase();
    const matches = tables.filter(t => (t.name + ' ' + t.comment).toLowerCase().includes(query));
    el('table-count').textContent = `显示 ${matches.length} / ${tables.length} 张表或视图`;
    const fragment = document.createDocumentFragment();
    for (const table of matches) {
      const button = node('button', table.name);
      button.type = 'button';
      button.setAttribute('aria-pressed', String(table.name === selectedTable));
      button.append(node('small', (table.type === 'view' ? '视图 · ' : '') + table.comment));
      button.addEventListener('click', () => loadTable(table.name));
      fragment.append(button);
    }
    if (!matches.length) fragment.append(node('p', tables.length ? '没有匹配的数据表。' : '暂无可浏览的数据表。', 'schema-muted'));
    el('tables').replaceChildren(fragment);
  }

  function renderColumns() {
    const query = el('column-search').value.trim().toLowerCase();
    const matches = columns.filter(c => (c.name + ' ' + c.comment).toLowerCase().includes(query));
    el('column-count').textContent = `显示 ${matches.length} / ${columns.length} 个字段（主键序号表示复合主键中的位置；NOT NULL 仅显示显式声明）`;
    const fragment = document.createDocumentFragment();
    for (const c of matches) {
      const tr = document.createElement('tr');
      const marker = c.hidden === 2 || c.hidden === 3 ? '（生成列）' : c.hidden ? '（隐藏列）' : '';
      for (const value of [c.name + marker, c.type || '未声明', c.primary_key || '—', c.not_null ? '是' : '否', c.default === null ? '未声明' : c.default, c.comment]) {
        tr.append(node('td', value));
      }
      const evidence = document.createElement('td');
      evidence.append(node('span', labels[c.confidence] || '未知', 'schema-confidence'), node('div', c.basis));
      tr.append(evidence);
      fragment.append(tr);
    }
    el('columns').replaceChildren(fragment);
  }

  async function loadTable(name) {
    const current = ++revision;
    selectedTable = name;
    renderTables();
    clear('正在读取表结构…');
    try {
      const data = await get('table', { db: el('database').value, table: name });
      if (current !== revision) return;
      columns = data.columns;
      el('table-name').textContent = data.name;
      el('table-comment').textContent = `${data.comment} · ${labels[data.confidence]} · ${data.basis}`;
      el('column-search').value = '';
      renderColumns();
      el('sql').textContent = data.sql || 'SQLite 内部结构，无显式建表 SQL。';
      el('indexes').replaceChildren();
      for (const index of data.indexes) {
        const names = index.columns.filter(c => c.key).map(c => c.name || (c.cid === -2 ? '表达式（见 SQL）' : '隐式 rowid'));
        el('indexes').append(node('p', `${index.name} · ${index.unique ? '唯一' : '普通'}${index.partial ? ' · 部分索引' : ''} · ${names.join(', ')}`),
          node('pre', index.sql || 'SQLite 自动创建的索引'));
      }
      if (!data.indexes.length) el('indexes').append(node('p', '未声明索引'));
      el('foreign-keys').textContent = data.foreign_keys.length ? data.foreign_keys.map(f =>
        `${f.from} → ${f.table}.${f.to || '主键'}（ON UPDATE ${f.on_update} / ON DELETE ${f.on_delete}）`).join('\n') : '未声明外键；字段说明中可能包含应用层关联。';
      el('status').textContent = '';
      el('structure').hidden = false;
    } catch (error) {
      if (current === revision) clear(error.message);
    }
  }

  async function loadTables() {
    updateExport();
    const current = ++revision;
    tables = []; selectedTable = '';
    el('table-search').value = '';
    renderTables();
    clear('正在读取数据表…');
    const db = databases.find(d => d.path === el('database').value);
    if (!db || db.status !== 'ready') {
      clear(db ? (db.error || '该数据库尚未解密。请先执行全量解密，再返回刷新。') : '请先备份或解密数据库。');
      return;
    }
    try {
      const data = await get('tables', { db: db.path });
      if (current !== revision) return;
      tables = data.tables;
      renderTables();
      if (tables.length) loadTable(tables[0].name);
      else clear('该数据库中没有数据表或视图。');
    } catch (error) {
      if (current === revision) clear(error.message);
    }
  }

  async function refresh() {
    refreshing = true;
    updateExport();
    const current = ++revision;
    const previous = el('database').value;
    el('database').disabled = true;
    el('refresh').disabled = true;
    tables = []; selectedTable = '';
    renderTables(); clear('正在扫描数据库…');
    try {
      const data = await get('databases');
      if (current !== revision) return;
      databases = data.databases;
      el('paths').textContent = `当前解密目录：${data.decrypted_dir || '未设置'}；微信源目录：${data.source_dir || '未设置'}`;
      const ready = databases.filter(d => d.status === 'ready').length;
      el('summary').textContent = `共 ${databases.length} 个数据库，${ready} 个可读取，${databases.length - ready} 个未解密或不可读取。`;
      el('database').replaceChildren();
      for (const db of databases) {
        const option = node('option', `${db.path} · ${statuses[db.status]}${db.origin === 'tool' ? ' · 工具生成' : ''}`);
        option.value = db.path;
        el('database').append(option);
      }
      if (!databases.length) el('database').append(node('option', '暂无数据库'));
      el('database').value = databases.some(d => d.path === previous) ? previous : (databases.find(d => d.status === 'ready') || databases[0] || {}).path || '';
      el('database').disabled = !databases.length;
      loadTables();
    } catch (error) {
      if (current === revision) clear(error.message);
    } finally {
      refreshing = false;
      updateExport();
      el('refresh').disabled = false;
    }
  }

  el('database').addEventListener('change', loadTables);
  el('refresh').addEventListener('click', refresh);
  el('export').addEventListener('click', exportSchema);
  el('export-scope').addEventListener('change', updateExport);
  el('table-search').addEventListener('input', renderTables);
  el('column-search').addEventListener('input', renderColumns);
  refresh();
})();
