"use strict";
const els = {
  account: document.getElementById('account'),
  status: document.getElementById('status'),
  statusText: document.getElementById('statusText'),
  retry: document.getElementById('retryBtn'),
  scope: document.getElementById('scope'),
  reload: document.getElementById('reload'),
  login: document.getElementById('login'),
  progress: document.getElementById('progress'),
  bar: document.querySelector('#progress i'),
  courses: document.getElementById('courses'),
  types: document.getElementById('types'),
  q: document.getElementById('q'),
  restricted: document.getElementById('restricted'),
  selAll: document.getElementById('selAll'),
  selNone: document.getElementById('selNone'),
  rows: document.getElementById('rows'),
  listwrap: document.getElementById('listwrap'),
  empty: document.getElementById('empty'),
  skeleton: document.getElementById('skeleton'),
  emptyReload: document.getElementById('emptyReload'),
  sortName: document.getElementById('sortName'),
  sortSize: document.getElementById('sortSize'),
  cur: document.getElementById('cur'),
  count: document.getElementById('count'),
  notices: document.getElementById('notices'),
  download: document.getElementById('download'),
  tabLog: document.getElementById('tabLog'),
  tabNotice: document.getElementById('tabNotice'),
  panelLog: document.getElementById('panelLog'),
  panelNotice: document.getElementById('panelNotice'),
  log: document.getElementById('log'),
  noticeList: document.getElementById('noticeList'),
  nBadge: document.getElementById('nBadge'),
  toast: document.getElementById('toast'),
};

const CHECK_SVG = '<svg width="10" height="10" viewBox="0 0 10 10" fill="none"><path d="M1.5 5.5 4 8 8.5 2" stroke="#fff" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>';

let files = [];
let signature = '';
let lastLogLen = 0;
let noticeSig = '';
let busy = false;
let courseFilter = '';
let typeFilter = '';
let sortKey = '';
let sortDir = 1;
const checked = new Set();
const done = new Set();

const TYPE_ORDER = ['PDF', 'PPT', 'Word', 'Excel', '压缩包', '图片', '其他'];

function typeOf(name) {
  const m = String(name).toLowerCase().match(/\.([a-z0-9]+)$/);
  const e = m ? m[1] : '';
  if (e === 'pdf') return 'PDF';
  if (e === 'ppt' || e === 'pptx') return 'PPT';
  if (e === 'doc' || e === 'docx') return 'Word';
  if (e === 'xls' || e === 'xlsx' || e === 'csv') return 'Excel';
  if (['zip', 'rar', '7z', 'tar', 'gz'].includes(e)) return '压缩包';
  if (['png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp'].includes(e)) return '图片';
  return '其他';
}

function human(n) {
  if (!n) return '';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' MB';
  if (n >= 1024) return Math.round(n / 1024) + ' KB';
  return n + ' B';
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function toast(text) {
  els.toast.textContent = text;
  els.toast.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => els.toast.classList.remove('show'), 1800);
}

// ---------- 派生视图 ----------
function view() {
  const q = els.q.value.trim().toLowerCase();
  const showRestricted = els.restricted.checked;
  let list = files.filter(f => {
    if (f.restricted && !showRestricted) return false;
    if (courseFilter && f.course !== courseFilter) return false;
    if (typeFilter && typeOf(f.name) !== typeFilter) return false;
    if (q && !((f.course + ' ' + f.name).toLowerCase().includes(q))) return false;
    return true;
  });
  if (sortKey) {
    list = [...list].sort((a, b) => {
      let d = 0;
      if (sortKey === 'size') d = (a.size || 0) - (b.size || 0);
      else d = String(a.name).localeCompare(String(b.name), 'zh');
      return d * sortDir;
    });
  }
  return list;
}

// ---------- 侧栏 ----------
function renderSide() {
  const byCourse = new Map();
  const byType = new Map();
  for (const f of files) {
    byCourse.set(f.course, (byCourse.get(f.course) || 0) + 1);
    const t = typeOf(f.name);
    byType.set(t, (byType.get(t) || 0) + 1);
  }
  const cFrag = document.createDocumentFragment();
  const allBtn = document.createElement('button');
  allBtn.className = 'c-item' + (courseFilter ? '' : ' active');
  allBtn.innerHTML = '<span>全部课程</span><i>' + files.length + '</i>';
  allBtn.addEventListener('click', () => { courseFilter = ''; renderSide(); renderRows(); });
  cFrag.appendChild(allBtn);
  for (const [name, num] of [...byCourse.entries()].sort((a, b) => b[1] - a[1])) {
    const btn = document.createElement('button');
    btn.className = 'c-item' + (courseFilter === name ? ' active' : '');
    btn.title = name;
    btn.innerHTML = '<span>' + esc(name) + '</span><i>' + num + '</i>';
    btn.addEventListener('click', () => {
      courseFilter = courseFilter === name ? '' : name;
      renderSide(); renderRows();
    });
    cFrag.appendChild(btn);
  }
  els.courses.replaceChildren(cFrag);

  const tFrag = document.createDocumentFragment();
  const chipAll = document.createElement('button');
  chipAll.className = typeFilter ? '' : 'active';
  chipAll.innerHTML = '全部<i>' + files.length + '</i>';
  chipAll.addEventListener('click', () => { typeFilter = ''; renderSide(); renderRows(); });
  tFrag.appendChild(chipAll);
  for (const t of TYPE_ORDER) {
    const num = byType.get(t) || 0;
    if (!num && t !== '其他') continue;
    const chip = document.createElement('button');
    chip.className = typeFilter === t ? 'active' : '';
    chip.innerHTML = esc(t) + '<i>' + num + '</i>';
    chip.addEventListener('click', () => {
      typeFilter = typeFilter === t ? '' : t;
      renderSide(); renderRows();
    });
    tFrag.appendChild(chip);
  }
  els.types.replaceChildren(tFrag);
}

// ---------- 文件表 ----------
function renderRows() {
  const list = view();
  const frag = document.createDocumentFragment();
  let lastCourse = null;
  for (const f of list) {
    if (!sortKey && f.course !== lastCourse) {
      lastCourse = f.course;
      const g = document.createElement('div');
      g.className = 'group';
      g.textContent = f.course;
      frag.appendChild(g);
    }
    const row = document.createElement('div');
    row.className = 'row' + (checked.has(f.key) ? ' on' : '') + (f.restricted ? ' restricted-row' : '');
    row.dataset.key = f.key;
    row.innerHTML =
      '<div class="c-check"><span class="box">' + CHECK_SVG + '</span></div>' +
      '<div class="c-name">' +
        (done.has(f.key) ? '<span class="tag-done">已存</span>' : '') +
        esc(f.name) +
        (f.restricted ? '<span class="tag-restricted">受限</span>' : '') +
      '</div>' +
      '<div class="c-course">' + esc(f.course) + '</div>' +
      '<div class="c-size">' + human(f.size) + '</div>';
    row.title = f.name;
    row.addEventListener('click', () => {
      if (checked.has(f.key)) checked.delete(f.key); else checked.add(f.key);
      row.classList.toggle('on', checked.has(f.key));
      updateCount();
    });
    frag.appendChild(row);
  }
  els.rows.replaceChildren(frag);

  const empty = !busy && files.length === 0 && !sortKey;
  const noMatch = !busy && files.length > 0 && view().length === 0;
  els.empty.hidden = !(empty || noMatch);
  if (empty || noMatch) {
    els.empty.querySelector('.big').textContent = noMatch ? '没匹配到任何文件' : '这个范围里还没有可下载的文件';
    els.empty.querySelector('.small').textContent = noMatch ? '清空搜索或换个条件试试' : '换个课程，或清空搜索试试';
  }
  els.skeleton.hidden = !(busy && files.length === 0);
  updateCount();
}

function updateCount() {
  const visible = view().length;
  const restricts = files.filter(f => f.restricted).length;
  const parts = ['已选 <b>' + checked.size + '</b> 个', '显示 ' + visible + ' 个'];
  if (restricts && !els.restricted.checked) parts.push('另有 ' + restricts + ' 个受限未显示');
  els.count.innerHTML = parts.join('　·　');
  els.download.textContent = checked.size ? '下载选中（' + checked.size + '）' : '下载选中';
}

function setSort(key) {
  if (sortKey === key) {
    if (sortDir === 1) sortDir = -1;
    else { sortKey = ''; sortDir = 1; }
  } else {
    sortKey = key; sortDir = 1;
  }
  els.sortName.querySelector('.arrow').textContent = sortKey === 'name' ? (sortDir === 1 ? '↑' : '↓') : '';
  els.sortSize.querySelector('.arrow').textContent = sortKey === 'size' ? (sortDir === 1 ? '↑' : '↓') : '';
  renderRows();
}

// ---------- 日志 / 通知 ----------
function renderLogs(logs) {
  if (logs.length === lastLogLen) return;
  const wrap = els.log.parentElement;
  const atBottom = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 40;
  lastLogLen = logs.length;
  els.log.innerHTML = logs.map(item => {
    const text = typeof item === 'string' ? item : (item.message || item.text || '');
    const raw = typeof item === 'string' ? 20 : item.level;
    const byName = { ERROR: 40, WARN: 30, WARNING: 30 };
    const lvl = typeof raw === 'number' ? raw : (byName[(item.name || '').toUpperCase()] || 20);
    const cls = lvl >= 40 ? 'err' : lvl >= 30 ? 'warn' : '';
    return cls ? '<span class="' + cls + '">' + esc(text) + '</span>' : esc(text);
  }).join('\n');
  if (atBottom) wrap.scrollTop = wrap.scrollHeight;
}

function renderNotices(items) {
  const sig = JSON.stringify(items);
  if (sig === noticeSig) return;
  noticeSig = sig;
  if (!items.length) { els.noticeList.innerHTML = '<div class="empty-note"><div class="big">还没有汇总过通知</div><div class="small">点左下角「汇总通知」开始</div></div>'; els.nBadge.hidden = true; return; }
  els.nBadge.textContent = items.length;
  els.nBadge.hidden = false;
  els.noticeList.innerHTML = items.map(n =>
    '<div class="n-item">' +
      '<div class="n-title">' + esc(n.title) + '</div>' +
      '<div class="n-meta">' + esc(n.course || (n.scope === 'platform' ? '平台公告' : '—')) + (n.time ? ' · ' + esc(n.time) : '') + '</div>' +
      (n.body ? '<div class="n-body">' + esc(n.body) + '</div>' : '') +
    '</div>'
  ).join('');
}

function switchTab(tab) {
  const logOn = tab === 'log';
  els.tabLog.classList.toggle('active', logOn);
  els.tabNotice.classList.toggle('active', !logOn);
  els.panelLog.classList.toggle('show', logOn);
  els.panelNotice.classList.toggle('show', !logOn);
}

// ---------- 渲染 ----------
function render(d) {
  busy = d.busy;
  const count = d.courseCount ?? (typeof d.courses === 'number' ? d.courses : 0);
  els.account.textContent = d.user ? d.user + ' · ' + count + ' 门课程' : '未连接';

  const statusText = d.status || '';
  const isBad = /登录已失效|与本地服务断开/.test(statusText);
  els.status.classList.toggle('danger', isBad);
  els.statusText.textContent = statusText;
  els.retry.hidden = !/与本地服务断开/.test(statusText);

  els.login.classList.toggle('outline-accent', /登录已失效/.test(statusText));

  done.clear();
  for (const k of (d.downloaded || [])) done.add(k);
  if (!d.downloaded) for (const f of (d.files || [])) if (f.done) done.add(f.key);

  const sig = (d.files || []).map(f => f.key).join('|') + '#' + done.size;
  if (sig !== signature) {
    signature = sig;
    files = d.files || [];
    renderSide();
    renderRows();
  }

  if (d.scope) {
    for (const b of els.scope.querySelectorAll('button')) {
      b.classList.toggle('active', b.dataset.scope === d.scope);
    }
  }

  const p = d.progress || { done: 0, total: 0 };
  const showBar = busy && p.total > 0;
  els.progress.classList.toggle('on', !!showBar);
  if (showBar) els.bar.style.width = Math.min(100, (p.done / p.total) * 100) + '%';
  els.cur.textContent = (busy && d.current && p.total) ? '正在下载 ' + p.done + '/' + p.total + '：' + d.current : '';

  renderLogs(d.logs || []);
  renderNotices(d.notices || []);

  for (const b of [els.download, els.notices, els.reload, els.login]) b.disabled = busy;
  updateCount();
}

async function post(path, body) {
  await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }).catch(() => {});
  poke();
}

let timer = null;
let inflight = false;
async function tick() {
  if (inflight) return;
  inflight = true;
  try {
    const res = await fetch('/api/state');
    if (!res.ok) throw new Error('http');
    render(await res.json());
  } catch (e) {
    els.status.classList.add('danger');
    els.statusText.textContent = '与本地服务断开，请确认窗口还在运行';
    els.retry.hidden = false;
    for (const b of [els.download, els.notices, els.reload, els.login]) b.disabled = false;
  } finally {
    inflight = false;
    clearTimeout(timer);
    timer = setTimeout(tick, 900);
  }
}
function poke() { clearTimeout(timer); tick(); }

// ---------- 事件 ----------
els.q.addEventListener('input', renderRows);
els.restricted.addEventListener('change', renderRows);
els.selAll.addEventListener('click', () => { for (const f of view()) checked.add(f.key); renderRows(); });
els.selNone.addEventListener('click', () => { checked.clear(); renderRows(); });
els.sortName.addEventListener('click', () => setSort('name'));
els.sortSize.addEventListener('click', () => setSort('size'));
els.tabLog.addEventListener('click', () => switchTab('log'));
els.tabNotice.addEventListener('click', () => switchTab('notice'));
els.emptyReload.addEventListener('click', () => post('/api/reload'));
els.retry.addEventListener('click', poke);

els.download.addEventListener('click', () => {
  if (!checked.size) return toast('先勾选要下载的文件');
  post('/api/download', { keys: [...checked] });
});
els.notices.addEventListener('click', () => { switchTab('notice'); post('/api/notices'); });
els.reload.addEventListener('click', () => post('/api/reload'));
els.login.addEventListener('click', () => post('/api/login'));

els.scope.addEventListener('click', e => {
  const btn = e.target.closest('button');
  if (!btn || btn.classList.contains('active')) return;
  post('/api/reload', { scope: btn.dataset.scope });
});

document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement !== els.q) {
    e.preventDefault(); els.q.focus();
  }
  if (e.key === 'Escape') els.q.blur();
});

tick();
