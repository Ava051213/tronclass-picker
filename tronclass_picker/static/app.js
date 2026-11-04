/**
 * tronclass-picker 前端应用（严格按模块代码渲染版）
 * - 模块一：顶部筛选标签 (Pill Tags)
 * - 模块二：文件列表表格 (标准 Table，修复图标裁切)
 * - 模块三：底部悬浮操作栏
 * - 右侧日志面板保持原样
 */

(() => {
  // ---------- State ----------
  const state = {
    files: [],
    todos: [],
    notices: [],
    user: '',
    courses: 0,
    busy: false,
    status: '准备中…',
    progress: { done: 0, total: 0 },
    current: '',
    synced: '',
    logs: [],

    // UI state
    courseFilter: '',
    onlyNew: false,
    onlyUndone: false,
    searchQuery: '',
    pickedKeys: new Set(),
    collapsedCourses: new Set(),

    // Download tracking
    downloadingKeys: new Set(),
  };

  // ---------- Constants ----------
  const POLL_INTERVAL = 1500;
  const KINDS = {
    pdf: ['PDF', '#e33e39'],
    ppt: ['PPT', '#b4652f'], pptx: ['PPT', '#b4652f'],
    doc: ['DOC', '#2f5fa8'], docx: ['DOC', '#2f5fa8'],
    xls: ['XLS', '#3b6d11'], xlsx: ['XLS', '#3b6d11'], csv: ['CSV', '#3b6d11'],
    zip: ['ZIP', '#6b6b66'], rar: ['ZIP', '#6b6b66'], '7z': ['ZIP', '#6b6b66'],
    tar: ['ZIP', '#6b6b66'], gz: ['ZIP', '#6b6b66'],
    py: ['CODE', '#534ab7'], java: ['CODE', '#534ab7'], c: ['CODE', '#534ab7'],
    cpp: ['CODE', '#534ab7'], h: ['CODE', '#534ab7'], chisel: ['CODE', '#534ab7'],
    scala: ['CODE', '#534ab7'], ipynb: ['CODE', '#534ab7'],
    mp4: ['VID', '#8a5a9b'], mp3: ['AUD', '#8a5a9b'], wav: ['AUD', '#8a5a9b'],
  };

  // ---------- DOM Refs ----------
  const $ = (id) => document.getElementById(id);
  const els = {
    meta: $('meta'), q: $('q'), loginBtn: $('login'), reloadBtn: $('reload'), downloadBtn: $('download'),
    filterTags: $('filter-tags'),
    filterNewCb: null,   // 动态生成，后续绑定
    filterUndoneCb: null,
    filesTbody: $('files-tbody'),
    checkAll: $('checkAll'),
    progress: $('progress'), synced: $('synced'),
    bulk: $('bulk'), bulkText: $('bulkText'), bulkGo: $('bulkGo'), bulkClear: $('bulkClear'),
    logClear: $('log-clear'), logBody: $('log-body'),
    scrollContainer: $('scroll-container'),
    toast: $('toast'),
    emptyState: $('empty-state'),
  };

  // ---------- Helpers ----------
  const esc = (s) => String(s).replace(/[&<>"]/g, c => ({'&':'&','<':'<','>':'>','"':'"'}[c]));
  const human = (n) => {
    if (!n) return '';
    if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' GB';
    if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n >= 1024) return Math.round(n / 1024) + ' KB';
    return n + ' B';
  };
  const kindOf = (name) => {
    const m = String(name).toLowerCase().match(/\.([a-z0-9]+)$/);
    return KINDS[m ? m[1] : ''] || ['FILE', '#a8a49c'];
  };
  const toast = (text) => {
    const el = els.toast;
    el.textContent = text;
    el.classList.remove('opacity-0');
    el.classList.add('opacity-100');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
      el.classList.remove('opacity-100');
      el.classList.add('opacity-0');
    }, 1900);
  };

  // ---------- Filtering ----------
  function visibleFiles() {
    const q = state.searchQuery.toLowerCase();
    return state.files.filter(f => {
      if (state.courseFilter && f.course !== state.courseFilter) return false;
      if (state.onlyNew && !f.isNew) return false;
      if (state.onlyUndone && f.done) return false;
      if (q && !(f.course + ' ' + f.name).toLowerCase().includes(q)) return false;
      return true;
    });
  }

  // ---------- Rendering ----------
  function renderAll() {
    renderHeader();
    renderFilters();
    renderFileTable();
    renderStats();
    updateBulkBar();
  }

  function renderHeader() {
    els.meta.textContent = state.user
      ? `${state.user} · ${state.courses} 门课`
      : '未连接';
    els.reloadBtn.disabled = state.busy;
    els.loginBtn.disabled = state.busy;
    const n = state.pickedKeys.size;
    els.downloadBtn.textContent = n ? `下载 ${n} 个` : '下载';
    els.downloadBtn.disabled = state.busy || !n;
  }

  // 模块一：顶部筛选标签 (Pill Tags) —— 严格按用户给出的 class 渲染
  function renderFilters() {
    const counts = new Map();
    for (const f of state.files) counts.set(f.course, (counts.get(f.course) || 0) + 1);

    const frag = document.createDocumentFragment();

    // “全部”按钮：选中时用蓝色，未选中用白色
    const allBtn = document.createElement('button');
    allBtn.className = state.courseFilter
      ? 'px-3 py-1 rounded-full border border-[#d0d7de] bg-white text-gray-700 hover:bg-gray-50'
      : 'px-3 py-1 rounded-full border border-blue-600 bg-blue-50 text-blue-700 font-medium';
    allBtn.textContent = '全部 ' + state.files.length;
    allBtn.onclick = () => { state.courseFilter = ''; renderAll(); };
    frag.appendChild(allBtn);

    // 各课程按钮
    for (const [name, n] of [...counts.entries()].sort((a, b) => b[1] - a[1])) {
      const b = document.createElement('button');
      b.className = state.courseFilter === name
        ? 'px-3 py-1 rounded-full border border-blue-600 bg-blue-50 text-blue-700 font-medium'
        : 'px-3 py-1 rounded-full border border-[#d0d7de] bg-white text-gray-700 hover:bg-gray-50';
      b.textContent = name + ' ' + n;
      b.onclick = () => { state.courseFilter = state.courseFilter === name ? '' : name; renderAll(); };
      frag.appendChild(b);
    }

    // 右侧 Checkbox 开关区
    const rightWrap = document.createElement('div');
    rightWrap.className = 'ml-auto flex gap-4 text-sm text-gray-600';

    const newLabel = document.createElement('label');
    newLabel.className = 'flex items-center gap-1.5 cursor-pointer hover:text-gray-900';
    const newCb = document.createElement('input');
    newCb.type = 'checkbox';
    newCb.className = 'rounded border-gray-300';
    newCb.checked = state.onlyNew;
    newCb.onchange = () => { state.onlyNew = newCb.checked; renderAll(); };
    newLabel.appendChild(newCb);
    newLabel.appendChild(document.createTextNode('仅看新增'));
    rightWrap.appendChild(newLabel);

    const undoneLabel = document.createElement('label');
    undoneLabel.className = 'flex items-center gap-1.5 cursor-pointer hover:text-gray-900';
    const undoneCb = document.createElement('input');
    undoneCb.type = 'checkbox';
    undoneCb.className = 'rounded border-gray-300';
    undoneCb.checked = state.onlyUndone;
    undoneCb.onchange = () => { state.onlyUndone = undoneCb.checked; renderAll(); };
    undoneLabel.appendChild(undoneCb);
    undoneLabel.appendChild(document.createTextNode('仅看未下载'));
    rightWrap.appendChild(undoneLabel);

    frag.appendChild(rightWrap);

    els.filterTags.replaceChildren(frag);
  }

  // 模块二：文件列表表格 —— 严格按用户给出的 class 和内联样式渲染
  function renderFileTable() {
    const rows = visibleFiles();
    const tbody = els.filesTbody;
    tbody.replaceChildren();

    if (!rows.length) {
      els.emptyState.classList.remove('hidden');
      return;
    }
    els.emptyState.classList.add('hidden');

    let course = null;
    let skip = false;
    for (const f of rows) {
      // 课程分组行
      if (f.course !== course) {
        course = f.course;
        skip = state.collapsedCourses.has(course);
        const groupTr = document.createElement('tr');
        groupTr.className = 'bg-[#f3f4f6]';
        groupTr.dataset.course = course;
        const courseCount = state.files.filter(x => x.course === course).length;
        groupTr.innerHTML = `
          <td colspan="6" class="px-4 py-2 text-[#24292f] font-semibold border-b border-[#d0d7de] flex items-center gap-2 cursor-pointer select-none group-hover:bg-[#e5e7ea] transition-colors">
            <span class="caret${skip ? ' closed' : ''} shrink-0"></span>
            <span class="truncate">${esc(course)}</span>
            <span class="ml-auto text-[#6b6b66] text-xs font-normal">${courseCount}</span>
          </td>
        `;
        groupTr.onclick = (e) => {
          if (!e.target.closest('.caret')) return;
          toggleCollapse(course);
        };
        tbody.appendChild(groupTr);
      }
      if (skip) continue;

      const kind = kindOf(f.name);
      const rowKey = f.key;
      const isDownloading = state.downloadingKeys.has(rowKey);
      const isDone = f.done;
      const isNew = f.isNew;
      const isSelected = state.pickedKeys.has(rowKey);

      const tr = document.createElement('tr');
      tr.className = `hover:bg-[#f6f8fa] transition-colors group ${isSelected ? 'bg-[#f6f8fa]' : ''}`;
      tr.dataset.key = rowKey;

      // 类型标签：修复 PPT/ZIP 裁切，inline-flex 固定尺寸居中
      let kindClass = 'inline-flex items-center justify-center w-[32px] h-[18px] rounded bg-orange-100 text-orange-600 font-bold text-[10px]';
      if (kind[0] === 'ZIP') {
        kindClass = 'inline-flex items-center justify-center w-[32px] h-[18px] rounded bg-gray-100 text-gray-600 font-bold text-[10px]';
      }

      // 状态标签（新增/下载中/已下载）
      let statusHtml = '';
      if (isNew) statusHtml += `<span class="ml-1 inline-flex items-center justify-center w-[32px] h-[16px] rounded bg-[#e6f1fb] text-[#185fa5] text-[10px] font-medium">新增</span>`;
      if (isDownloading) statusHtml += `<span class="ml-1 inline-flex items-center justify-center w-[32px] h-[16px] rounded bg-[#faeeda] text-[#854f0b] text-[10px] font-medium">下载中</span>`;
      if (isDone) statusHtml += `<span class="ml-1 inline-flex items-center justify-center w-[32px] h-[16px] rounded bg-[#e2e8f0] text-[#6b6b66] text-[10px] font-medium">已下载</span>`;

      tr.innerHTML = `
        <td class="px-4 py-2">
          <input type="checkbox" class="rounded border-gray-300" ${isSelected ? 'checked' : ''}>
        </td>
        <td class="px-4 py-2">
          <span class="${kindClass}">${kind[0]}</span>
        </td>
        <td class="px-4 py-2 text-gray-800 font-medium truncate">${esc(f.name)}${statusHtml}</td>
        <td class="px-4 py-2 text-right text-gray-500 whitespace-nowrap">${esc(f.date || '')}</td>
        <td class="px-4 py-2 text-right text-gray-500 whitespace-nowrap">${human(f.size)}</td>
        <td class="px-4 py-2 text-center">
          <button class="text-blue-600 hover:underline" data-dl="${esc(rowKey)}" ${isDownloading || isDone ? 'disabled' : ''}>
            ${isDownloading ? '下载中…' : (isDone ? '已下载' : '下载')}
          </button>
        </td>
      `;
      tr.title = f.course + (f.source ? ' · ' + f.source : '');
      tbody.appendChild(tr);
    }
  }

  function allChecked(rows) {
    return rows.length > 0 && rows.every(f => state.pickedKeys.has(f.key));
  }

  function toggleCollapse(course) {
    if (state.collapsedCourses.has(course)) state.collapsedCourses.delete(course);
    else state.collapsedCourses.add(course);
    renderAll();
  }

  function togglePick(key) {
    if (state.pickedKeys.has(key)) state.pickedKeys.delete(key);
    else state.pickedKeys.add(key);
    renderAll();
  }

  function toggleAll() {
    const rows = visibleFiles();
    const on = rows.length > 0 && rows.every(f => state.pickedKeys.has(f.key));
    for (const f of rows) {
      if (on) state.pickedKeys.delete(f.key);
      else state.pickedKeys.add(f.key);
    }
    renderAll();
  }

  function renderStats() {
    els.progress.textContent = state.busy
      ? (state.progress.total ? state.progress.done + ' / ' + state.progress.total + (state.current ? '　' + state.current : '') : (state.status || ''))
      : (state.status || '');
    els.synced.textContent = state.synced || '';
  }

  function updateBulkBar() {
    const n = state.pickedKeys.size;
    els.bulk.style.display = n > 0 ? '' : 'none';
    els.bulkText.textContent = '已选 ' + n + ' 项';
  }

  // ---------- Log Panel (右侧日志面板，保持原样) ----------
  function renderLogs(logs) {
    if (!logs || !logs.length) {
      els.logBody.innerHTML = '<div class="text-gray-500 text-xs">暂无日志</div>';
      return;
    }
    const frag = document.createDocumentFragment();
    for (const entry of logs) {
      const div = document.createElement('div');
      div.className = 'log-entry';
      const levelClass = {
        INFO: 'text-blue-400',
        WARN: 'text-yellow-400',
        ERROR: 'text-red-400',
        SUCCESS: 'text-green-400',
        AUTH: 'text-purple-400',
      }[entry.level] || 'text-blue-400';
      div.innerHTML = `<span class="text-gray-500 mr-2 font-mono">${esc(entry.ts)}</span><span class="${levelClass} font-bold mr-2">[${esc(entry.level || 'INFO')}]</span>${esc(entry.message)}`;
      frag.appendChild(div);
    }
    els.logBody.replaceChildren(frag);
    els.logBody.scrollTop = els.logBody.scrollHeight;
  }

  // ---------- API ----------
  async function fetchState() {
    try {
      const res = await fetch('/api/state');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      applyState(data);
    } catch (e) {
      els.meta.textContent = '与本地服务断开';
    }
  }

  async function fetchLogs() {
    try {
      const res = await fetch('/api/logs');
      if (!res.ok) return;
      const data = await res.json();
      if (data.ok && data.logs) {
        renderLogs(data.logs);
      }
    } catch (e) {
      console.error('fetch logs error', e);
    }
  }

  function applyState(data) {
    if (!data.busy && state.downloadingKeys.size > 0) {
      const newFilesByKey = new Map((data.files || []).map(f => [f.key, f]));
      for (const key of state.downloadingKeys) {
        const f = newFilesByKey.get(key);
        if (f && f.done) {
          state.downloadingKeys.delete(key);
        }
      }
    }

    state.busy = !!data.busy;
    state.files = data.files || [];
    state.todos = data.todos || [];
    state.notices = data.notices || [];
    state.user = data.user || '';
    state.courses = data.courses || 0;
    state.status = data.status || '';
    state.progress = data.progress || { done: 0, total: 0 };
    state.current = data.current || '';
    state.synced = data.synced || '';

    const existingKeys = new Set(state.files.map(f => f.key));
    for (const k of [...state.pickedKeys]) {
      if (!existingKeys.has(k)) state.pickedKeys.delete(k);
    }

    if (data.logs) {
      state.logs = data.logs;
      renderLogs(state.logs);
    }

    renderAll();
  }

  async function post(path, body) {
    try {
      const res = await fetch(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
      });
      if (!res.ok) toast('请求失败 ' + res.status);
    } catch (e) {
      toast('本地服务已断开');
    }
    fetchState();
  }

  async function downloadKeys(keys) {
    if (!keys.length) return toast('先选几个文件');
    for (const k of keys) state.downloadingKeys.add(k);
    for (const k of keys) state.pickedKeys.delete(k);
    updateBulkBar();
    renderHeader();
    renderFileTable();

    await post('/api/download', { keys });
  }

  // ---------- Event Handlers ----------
  function initEvents() {
    els.downloadBtn.onclick = () => downloadKeys([...state.pickedKeys]);
    els.bulkGo.onclick = () => downloadKeys([...state.pickedKeys]);
    els.bulkClear.onclick = () => { state.pickedKeys.clear(); renderAll(); };
    els.reloadBtn.onclick = () => post('/api/scan');
    els.loginBtn.onclick = () => post('/api/login');

    els.q.oninput = (e) => {
      state.searchQuery = e.target.value.trim();
      renderAll();
    };

    // 表头全选
    els.checkAll.onchange = () => {
      const rows = visibleFiles();
      const shouldCheck = !els.checkAll.checked;
      for (const f of rows) {
        if (shouldCheck) state.pickedKeys.add(f.key);
        else state.pickedKeys.delete(f.key);
      }
      renderAll();
    };

    // 表格事件委托
    els.filesTbody.onclick = (e) => {
      // 下载按钮
      const dlBtn = e.target.closest('[data-dl]');
      if (dlBtn) {
        e.stopPropagation();
        downloadKeys([dlBtn.dataset.dl]);
        return;
      }
      // 行复选框
      const cb = e.target.closest('input[type="checkbox"]');
      if (cb && cb !== els.checkAll) {
        const row = cb.closest('tr[data-key]');
        if (row) {
          e.stopPropagation();
          togglePick(row.dataset.key);
        }
        return;
      }
      // 课程分组折叠（由 groupTr.onclick 处理）
    };

    els.logClear.onclick = async () => {
      await post('/api/logs/clear', {});
      state.logs = [];
      renderLogs([]);
    };

    // Keyboard
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && document.activeElement.tagName !== 'INPUT') {
        e.preventDefault();
        els.q.focus();
      }
      if (e.key === 'Escape') els.q.blur();
    });
  }

  // ---------- Polling Loop ----------
  function startPolling() {
    (async function loop() {
      await fetchState();
      await fetchLogs();
      setTimeout(loop, state.busy ? 700 : POLL_INTERVAL);
    })();
  }

  // ---------- Init ----------
  function init() {
    initEvents();
    startPolling();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();