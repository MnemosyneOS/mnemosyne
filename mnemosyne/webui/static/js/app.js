/* ============================================================================
 * Mnemosyne 7.0.0 Web 管理端 — 前端交互逻辑
 * 零依赖：纯原生 JS，无任何 CDN / 外部库。
 * ========================================================================== */
(function () {
  'use strict';

  // ---------------- 状态 ----------------
  var currentPage = 'dashboard';
  var currentUser = null;
  var mode = localStorage.getItem('mnemosyne_mode') || 'b';
  var lang = localStorage.getItem('mnemosyne_lang') || 'zh';
  var graphData = null, graphPlaying = false, graphPlayTimer = null, graphPos = {};
  var graphReveal = 1;            // 时间回放游标 0..1（Hermes reveal）
  var graphLayout = null;         // Hermes 布局：{rings, rec(Map), tr(Map), index, timed}
  var graphAppear = {};           // 节点淡入动画：nodeId -> 出现时间戳
  var graphRAF = null;            // 图谱动画帧句柄
  var graphAdjacency = {};        // Hermes 聚焦邻接表：nodeId -> [neighborId,...]
  var graphNodeById = {};         // nodeId -> node（含 kind/label/content）
  var graphHoverId = null;        // 临时悬停聚焦节点
  var graphSelectedId = null;     // 点击锁定聚焦节点（Hermes selectedId）
  var editingProfileId = null;
  var notaryFilter = 'all';
  // 智能体记忆隔离状态：null=全部
  var currentAgent = localStorage.getItem('mnemosyne_agent') || 'all';
  var agentsCache = [];
  // 银河系动画状态

  function $(id) { return document.getElementById(id); }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ---------------- 智能体过滤辅助 ----------------
  function agentParam(first) {
    if (!currentAgent || currentAgent === 'all') return '';
    return (first ? '?' : '&') + 'agent=' + encodeURIComponent(currentAgent);
  }
  function currentAgentName() {
    var a = agentsCache.find(function (x) { return x.id === currentAgent; });
    return a ? a.name : '全部智能体';
  }

  // ---------------- Toast / Spinner ----------------
  function toast(msg, type) {
    var t = $('toast');
    t.textContent = msg;
    t.className = 'toast show ' + (type || 'success');
    clearTimeout(t._timer);
    t._timer = setTimeout(function () { t.className = 'toast ' + (type || 'success'); }, 3200);
  }
  function showSpinner(on) { $('spinner').style.display = on ? 'flex' : 'none'; }

  // ---------------- API ----------------
  async function api(url, opts) {
    opts = opts || {};
    opts.credentials = 'same-origin';
    var resp = await fetch(url, opts);
    var ct = (resp.headers.get('content-type') || '');
    var data;
    if (ct.indexOf('application/json') >= 0) {
      data = await resp.json();
    } else {
      data = await resp.text();
    }
    if (!resp.ok) {
      if (resp.status === 401) {
        showLogin();
        throw new Error('未登录或会话已过期，请重新登录');
      }
      var msg = (data && data.error) ? data.error : ('请求失败 ' + resp.status);
      throw new Error(msg);
    }
    return data;
  }

  // ---------------- 模式（C/B） ----------------
  function getMode() { return mode; }
  function setMode(m, persist) {
    mode = (m === 'c') ? 'c' : 'b';
    if (persist !== false) localStorage.setItem('mnemosyne_mode', mode);
    applyMode();
  }

  function applyMode() {
    // 侧边栏按模式过滤
    document.querySelectorAll('#nav .nav-item').forEach(function (el) {
      var m = el.getAttribute('data-mode') || 'both';
      el.style.display = (m === 'both' || m === mode) ? '' : 'none';
    });
    // 顶栏模式徽标
    $('mode-badge').textContent = (mode === 'c') ? 'C 端 · 个人用户' : 'B 端 · 企业客户';
    // 模式分段按钮状态
    document.querySelectorAll('.mode-seg-btn').forEach(function (b) {
      b.classList.toggle('active', b.getAttribute('data-mode') === mode);
    });
    // 当前页在模式下不可见时回退到仪表盘
    var cur = document.querySelector('#nav .nav-item.active');
    if (cur && cur.style.display === 'none') showPage('dashboard');
    renderFeatureGrid();
    if (currentPage === 'dashboard') { /* feature grid 已刷新 */ }
  }

  // ---------------- 国际化（轻量：中文为主，英文辅助） ----------------
  var I18N = {
    zh: {
      dashboard: '仪表盘', memories: '记忆管理', budget: '记忆预算器', forgetting: '遗忘经济学',
      notary: '记忆公证所', graph: '记忆图谱', tree: '知识树', audit: '审计日志', sessions: '会话历史',
      profile: '用户画像', namespaces: '多租户切换', capacity: '容量监控', ledger: '账本验证',
      transfer: '导入导出', knowledge: '知识库', heatmap: '热力图/画像', settings: '设置'
    },
    en: {
      dashboard: 'Dashboard', memories: 'Memories', budget: 'Memory Budget', forgetting: 'Forgetting Economy',
      notary: 'Memory Notary', graph: 'Memory Graph', tree: 'Knowledge Tree', audit: 'Audit Log', sessions: 'Sessions',
      profile: 'Profile', namespaces: 'Multi-tenant', capacity: 'Capacity', ledger: 'Ledger', transfer: 'Import/Export',
      knowledge: 'Knowledge Base', heatmap: 'Heatmap/Profile', settings: 'Settings'
    }
  };
  function t(key) {
    var dict = I18N[lang] || I18N.zh;
    return dict[key] || key;
  }

  // ---------------- SVG 图标 ----------------
  var ICONS = {
    dashboard: '<path d="M3 12l2-2v8h8v-6h6v6h8v-8l2 2v10a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    memories: '<path d="M12 3l9 5v6c0 5-3.5 9.5-9 11-5.5-1.5-9-6-9-11V8z"/>',
    budget: '<path d="M12 2a10 10 0 1 0 10 10h-3a7 7 0 1 1-7-7zM12 5v4l3 3-1.4 1.4L9 9V5z"/>',
    forgetting: '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 15h-2v-2h2zm0-4h-2V7h2z"/>',
    notary: '<path d="M12 2l8 3v6c0 5-3.4 9.4-8 11-4.6-1.6-8-6-8-11V5z"/><path d="M12 7l2 4h-4z"/>',
    graph: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="8" cy="9" r="2"/><circle cx="16" cy="8" r="2"/><circle cx="12" cy="16" r="2"/>',
    tree: '<path d="M6 3h4v4H6zM14 3h4v4h-4zM6 17h4v4H6zM14 17h4v4h-4zM8 7v3h8V7M10 11v6M14 11v6"/>',
    audit: '<path d="M19 7h-4V3H9v4H5l1 16h12zM11 3h2v4h-2zM9 11h6v2H9zM9 15h6v2H9z"/>',
    sessions: '<path d="M20 2H4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12l4 4V4a2 2 0 0 0-2-2z"/>',
    profile: '<circle cx="12" cy="7" r="3"/><path d="M12 22c-5 0-9-4-9-9s4-9 9-9 9 4 9 9-4 9-9 9z"/>',
    namespaces: '<path d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z"/>',
    capacity: '<path d="M12 3a9 9 0 1 0 9 9h-2.5a6.5 6.5 0 1 1-6.5-6.5zM12 6v6l4 2"/>',
    ledger: '<path d="M6 2h9l4 4v16H6zM14 2v5h5M9 13h6M9 17h6M9 9h2"/>',
    transfer: '<path d="M12 3v12m0 0l-4-4m4 4l4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"/>',
    knowledge: '<path d="M12 3L4 6v6c0 4.4 3.4 8.4 8 10 4.6-1.6 8-5.6 8-10V6z"/><path d="M9 12l2 2 4-4"/>',
    heatmap: '<rect x="4" y="4" width="5" height="5" rx="1"/><rect x="12" y="4" width="5" height="5" rx="1"/><rect x="4" y="12" width="5" height="5" rx="1"/><rect x="12" y="12" width="5" height="5" rx="1"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M12 1v4M12 19v4M4.2 4.2l2.8 2.8M17 17l2.8 2.8M1 12h4M19 12h4M4.2 19.8L7 17M17 7l2.8-2.8"/>'
  };

  // ---------------- 导航 ----------------
  var NAV = [
    { page: 'dashboard', mode: 'both', icon: 'dashboard' },
    { page: 'memories', mode: 'both', icon: 'memories' },
    { page: 'budget', mode: 'both', icon: 'budget' },
    { page: 'forgetting', mode: 'both', icon: 'forgetting' },
    { page: 'notary', mode: 'both', icon: 'notary' },
    { page: 'graph', mode: 'both', icon: 'graph' },
    { page: 'tree', mode: 'c', icon: 'tree' },
    { page: 'audit', mode: 'b', icon: 'audit' },
    { page: 'sessions', mode: 'both', icon: 'sessions' },
    { page: 'profile', mode: 'both', icon: 'profile' },
    { page: 'namespaces', mode: 'b', icon: 'namespaces' },
    { page: 'capacity', mode: 'b', icon: 'capacity' },
    { page: 'ledger', mode: 'b', icon: 'ledger' },
    { page: 'knowledge', mode: 'both', icon: 'knowledge' },
    { page: 'heatmap', mode: 'c', icon: 'heatmap' },
    { page: 'settings', mode: 'both', icon: 'settings' }
  ];

  function buildNav() {
    var nav = $('nav');
    nav.innerHTML = '';
    NAV.forEach(function (n) {
      var el = document.createElement('div');
      el.className = 'nav-item';
      el.setAttribute('data-page', n.page);
      el.setAttribute('data-mode', n.mode);
      el.innerHTML = '<span class="nav-icon"><svg class="nav-svg" viewBox="0 0 24 24" fill="currentColor">' +
        ICONS[n.icon] + '</svg></span><span class="nav-text">' + t(n.page) + '</span>';
      el.addEventListener('click', function () { showPage(n.page); });
      nav.appendChild(el);
    });
    applyMode();
  }

  // ---------------- 页面切换 ----------------
  function showPage(page) {
    document.querySelectorAll('.page').forEach(function (el) { el.style.display = 'none'; });
    var target = $('page-' + page);
    if (target) target.style.display = 'block';
    document.querySelectorAll('#nav .nav-item').forEach(function (el) { el.classList.remove('active'); });
    document.querySelectorAll('#nav .nav-item[data-page="' + page + '"]').forEach(function (el) { el.classList.add('active'); });
    $('page-title').textContent = t(page) || '仪表盘';
    currentPage = page;

    if (page === 'dashboard') loadDashboard();
    else if (page === 'memories') loadMemories();
    else if (page === 'budget') { /* 空 */ }
    else if (page === 'forgetting') loadForgetting();
    else if (page === 'notary') loadNotary();
    else if (page === 'graph') { if (!graphData) loadGraphTimeline(); else renderGraph(); }
    else if (page === 'tree') loadTree();
    else if (page === 'audit') loadAudit();
    else if (page === 'sessions') loadSessions();
    else if (page === 'profile') { loadProfiles(); loadSnapshot(); }
    else if (page === 'namespaces') loadNamespaces();
    else if (page === 'capacity') loadCapacity();
    else if (page === 'ledger') loadLedger();
    else if (page === 'knowledge') { /* dropzone 已绑定 */ }
    else if (page === 'heatmap') loadHeatmap();
    else if (page === 'settings') loadSettings();
  }

  // ---------------- 认证 ----------------
  async function checkAuth() {
    try {
      var data = await api('/api/auth/status');
      if (data.authenticated) {
        currentUser = data.username || 'admin';
        showApp();
        $('user-chip').textContent = currentUser;
      } else {
        showLogin();
      }
    } catch (e) {
      showLogin();
    }
  }

  function showLogin() {
    $('app-view').style.display = 'none';
    $('login-view').style.display = 'flex';
    currentUser = null;
  }

  function showApp() {
    $('login-view').style.display = 'none';
    $('app-view').style.display = 'block';
    buildNav();
    showPage('dashboard');
    loadNamespacesBadge();
    loadAgents();
  }

  async function login() {
    var u = $('login-username').value.trim();
    var p = $('login-password').value;
    if (!u || !p) { showLoginError('请输入用户名和密码'); return; }
    var btn = $('login-btn');
    btn.disabled = true; btn.textContent = '登录中…';
    try {
      var data = await api('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: u, password: p })
      });
      currentUser = data.username;
      toast('欢迎回来，' + data.username, 'success');
      showApp();
    } catch (e) {
      showLoginError(e.message);
    } finally {
      btn.disabled = false; btn.textContent = '登 录';
    }
  }

  function showLoginError(msg) {
    var el = $('login-error');
    el.textContent = msg;
    el.style.display = 'block';
  }

  async function logout() {
    try { await api('/api/auth/logout', { method: 'POST' }); } catch (e) {}
    showLogin();
    toast('已退出登录', 'success');
  }

  async function changePassword() {
    var oldPw = $('pw-old').value, newPw = $('pw-new').value;
    if (!oldPw || !newPw) { toast('请填写旧密码和新密码', 'error'); return; }
    try {
      var data = await api('/api/auth/change-password', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_password: oldPw, new_password: newPw })
      });
      toast(data.message || '密码修改成功', 'success');
      $('pw-old').value = ''; $('pw-new').value = '';
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------------- 仪表盘 ----------------
  async function loadDashboard() {
    try {
      var data = await api('/api/stats' + agentParam(true));
      $('stat-total').textContent = data.total_memories;
      $('stat-active').textContent = data.active_memories;
      var tc = data.tier_counts || {};
      $('stat-hot').textContent = tc.hot || 0;
      $('stat-warm').textContent = tc.warm || 0;
      $('stat-cold').textContent = tc.cold || 0;
      $('stat-suspicious').textContent = data.suspicious_count || 0;

      var cap = data.capacity || {};
      var active = cap.active_count || 0;
      var max = cap.max_active_memories;
      var fill = $('capacity-fill');
      if (max) {
        var pct = cap.percentage || 0;
        fill.style.width = pct + '%';
        fill.textContent = pct + '%';
        fill.className = 'capacity-fill';
        if (pct > 95) fill.classList.add('critical');
        else if (pct > 80) fill.classList.add('warning');
        $('capacity-text').textContent = active + ' / ' + max + ' 活跃记忆';
      } else {
        fill.style.width = '0%'; fill.textContent = 'N/A'; fill.className = 'capacity-fill';
        $('capacity-text').textContent = active + ' 活跃记忆（无上限）';
      }
      var su = data.storage_usage || {};
      $('storage-mini').innerHTML = '<span>磁盘总量 <b>' + (su.total_mb || 0) + ' MB</b></span>' +
        '<span>已用 <b>' + (su.used_mb || 0) + ' MB</b></span>' +
        '<span>剩余 <b>' + (su.free_mb || 0) + ' MB</b></span>';

      renderTierDonut(tc);
      renderLedgerStatus(data.ledger || {});
      loadRecentMemories();
      loadAuditMini();
      renderFeatureGrid();
    } catch (e) { toast(e.message, 'error'); }
  }

  function renderLedgerStatus(ledger) {
    var hash = ledger.latest_hash || 'N/A';
    var valid = ledger.valid !== false;
    $('ledger-status').innerHTML =
      '<div class="mono" style="color:#00D4FF;font-size:12px;word-break:break-all;">' + esc(String(hash).substring(0, 48)) + '</div>' +
      '<div style="margin-top:6px;color:' + (valid ? '#22C55E' : '#EF4444') + ';">' +
      (valid ? '✓ 账本链完整' : '✗ 校验失败') + '</div>';
  }

  function renderTierDonut(tc) {
    var canvas = $('tier-donut');
    if (!canvas) return;
    var W = canvas.width = canvas.clientWidth || 260;
    var H = canvas.height = 220;
    var ctx = canvas.getContext('2d');
    var cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 14, r = R - 26;
    var sum = (tc.hot || 0) + (tc.warm || 0) + (tc.cold || 0);
    var total = sum || 1;
    var segs = [
      { label: '热层', val: tc.hot || 0, color: '#22C55E' },
      { label: '温层', val: tc.warm || 0, color: '#F59E0B' },
      { label: '冷层', val: tc.cold || 0, color: '#EF4444' }
    ];
    var start = -Math.PI / 2;
    segs.forEach(function (s) {
      var ang = (s.val / total) * Math.PI * 2;
      ctx.beginPath();
      ctx.arc(cx, cy, R, start, start + ang);
      ctx.arc(cx, cy, r, start + ang, start, true);
      ctx.closePath();
      ctx.fillStyle = s.color;
      ctx.fill();
      start += ang;
    });
    ctx.fillStyle = '#E6EAF2';
    ctx.font = '700 26px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(sum, cx, cy - 2);
    ctx.font = '11px sans-serif';
    ctx.fillStyle = '#8b93a7';
    ctx.fillText('记忆总数', cx, cy + 18);
    // 图例
    var lx = 12, ly = 12;
    ctx.textAlign = 'left';
    segs.forEach(function (s) {
      ctx.fillStyle = s.color; ctx.fillRect(lx, ly, 10, 10);
      ctx.fillStyle = '#8b93a7'; ctx.fillText(s.label + ' ' + s.val, lx + 15, ly + 9);
      ly += 18;
    });
  }

  var FEATURES = [
    { page: 'budget', icon: '💰', name: '记忆预算器', desc: 'Token 预算约束检索 + cost_report', mode: 'both' },
    { page: 'forgetting', icon: '🧊', name: '遗忘经济学', desc: '记忆分层迁移（hot/warm/cold）', mode: 'both' },
    { page: 'notary', icon: '⚖️', name: '记忆公证所', desc: 'confidence + 注入检测标记', mode: 'both' },
    { page: 'ledger', icon: '📒', name: '记忆账本', desc: '哈希链账本 + 完整性验证', mode: 'b' },
    { page: 'namespaces', icon: '🗂️', name: '多租户', desc: '命名空间切换与记忆计数', mode: 'b' },
    { page: 'knowledge', icon: '📚', name: '知识库', desc: '上传文本并写入记忆', mode: 'both' },
    { page: 'heatmap', icon: '🌡️', name: '热力图/画像', desc: '日历热力图 + 长期画像', mode: 'c' },
    { page: 'sessions', icon: '💬', name: '会话历史', desc: '对话检索与实时轮次', mode: 'both' },
    { page: 'capacity', icon: '📊', name: '容量监控', desc: '容量 / 磁盘 / 分层', mode: 'b' }
  ];

  function renderFeatureGrid() {
    var grid = $('feature-grid');
    if (!grid) return;
    grid.innerHTML = '';
    FEATURES.forEach(function (f) {
      if (f.mode !== 'both' && f.mode !== mode) return;
      var el = document.createElement('div');
      el.className = 'feature-card';
      el.innerHTML = '<div class="f-icon">' + f.icon + '</div><div class="f-name">' + f.name + '</div>' +
        '<div class="f-desc">' + f.desc + '</div>';
      el.addEventListener('click', function () { showPage(f.page); });
      grid.appendChild(el);
    });
  }

  async function loadRecentMemories() {
    try {
      var data = await api('/api/memories?k=5' + agentParam());
      var list = $('recent-memories');
      if (data.memories && data.memories.length) {
        list.innerHTML = data.memories.map(function (m) {
          return '<div class="mini-item"><div class="mi-title">' + esc((m.content || '').slice(0, 90)) + '</div>' +
            '<div class="mi-meta"><span>' + esc(m.mtype || '') + '</span><span>conf ' + (m.confidence || 0) + '</span>' +
            '<span>' + (m.tier || 'hot') + '</span></div></div>';
        }).join('');
      } else list.innerHTML = '<div class="empty">暂无记忆</div>';
    } catch (e) {}
  }

  async function loadAuditMini() {
    try {
      var data = await api('/api/audit?limit=5' + agentParam());
      var tl = $('audit-timeline');
      tl.innerHTML = (data.entries && data.entries.length) ? data.entries.map(function (e) {
        return '<div class="timeline-item"><div class="action">' + esc(e.action || 'unknown') + '</div>' +
          '<div class="time">' + esc(e.ts || e.timestamp || '') + '</div>' +
          '<div class="details">seq ' + (e.seq || 0) + '</div></div>';
      }).join('') : '<div class="timeline-item"><div class="action">暂无审计记录</div></div>';
    } catch (e) {}
  }

  // ---------------- 记忆管理 ----------------
  async function loadMemories() {
    try {
      var tier = $('tier-filter').value || '';
      var url = '/api/memories?k=100' + (tier ? '&tier=' + encodeURIComponent(tier) : '') + agentParam();
      var data = await api(url);
      var tbody = $('memories-table-body');
      if (data.memories && data.memories.length) {
        tbody.innerHTML = data.memories.map(function (m) {
          var suspicious = m.injection_suspicious;
          return '<tr>' +
            '<td class="id-mono">' + esc(String(m.id || '').slice(0, 8)) + '</td>' +
            '<td>' + esc((m.content || '').slice(0, 100)) + (suspicious ? ' <span class="badge badge-suspicious">可疑</span>' : '') + '</td>' +
            '<td>' + esc(m.mtype || '') + '</td>' +
            '<td>' + (m.confidence != null ? Number(m.confidence).toFixed(2) : '-') + '</td>' +
            '<td>' + (m.importance || 0) + '</td>' +
            '<td><span class="badge badge-' + (m.tier || 'hot') + '">' + (m.tier || 'hot') + '</span></td>' +
            '<td><span class="badge badge-' + (m.status || 'active') + '">' + (m.status || 'active') + '</span></td>' +
            '<td style="white-space:nowrap;">' +
              '<button class="btn btn-secondary btn-sm" onclick="editMemory(\'' + esc(m.id) + '\')">编辑</button> ' +
              '<button class="btn btn-danger btn-sm" onclick="deleteMemory(\'' + esc(m.id) + '\')">删除</button>' +
            '</td></tr>';
        }).join('');
      } else tbody.innerHTML = '<tr><td colspan="8" class="empty">暂无记忆</td></tr>';
    } catch (e) { toast(e.message, 'error'); }
  }

  async function searchMemories() {
    var q = $('search-input').value.trim();
    if (!q) { loadMemories(); return; }
    try {
      var data = await api('/api/memories?action=search&q=' + encodeURIComponent(q) + '&k=30' + agentParam());
      var tbody = $('memories-table-body');
      if (data.memories && data.memories.length) {
        tbody.innerHTML = data.memories.map(function (m) {
          return '<tr>' +
            '<td class="id-mono">' + esc(String(m.id || '').slice(0, 8)) + '</td>' +
            '<td>' + esc((m.content || '').slice(0, 100)) + '</td>' +
            '<td>' + esc(m.mtype || '') + '</td>' +
            '<td>' + (m.score != null ? Number(m.score).toFixed(4) : '-') + '</td>' +
            '<td>' + (m.importance || 0) + '</td>' +
            '<td><span class="badge badge-' + (m.tier || 'hot') + '">' + (m.tier || 'hot') + '</span></td>' +
            '<td><span class="badge badge-' + (m.status || 'active') + '">' + (m.status || 'active') + '</span></td>' +
            '<td><button class="btn btn-secondary btn-sm" onclick="editMemory(\'' + esc(m.id) + '\')">编辑</button></td>' +
            '</tr>';
        }).join('');
      } else tbody.innerHTML = '<tr><td colspan="8" class="empty">无匹配结果</td></tr>';
    } catch (e) { toast(e.message, 'error'); }
  }

  function openAddModal() {
    editingProfileId = null;
    $('add-modal-title').textContent = '添加记忆';
    $('new-memory-content').value = '';
    $('new-memory-type').value = 'semantic';
    $('new-memory-importance').value = 3;
    $('edit-memory-id').value = '';
    $('add-modal').style.display = 'flex';
  }
  function closeAddModal() { $('add-modal').style.display = 'none'; }

  async function editMemory(id) {
    try {
      var data = await api('/api/memories/' + encodeURIComponent(id));
      var m = data.memory;
      $('add-modal-title').textContent = '编辑记忆';
      $('new-memory-content').value = m.content || '';
      $('new-memory-type').value = m.mtype || 'semantic';
      $('new-memory-importance').value = m.importance || 3;
      $('edit-memory-id').value = id;
      $('add-modal').style.display = 'flex';
    } catch (e) { toast(e.message, 'error'); }
  }

  async function saveMemory() {
    var content = $('new-memory-content').value.trim();
    if (!content) { toast('内容不能为空', 'error'); return; }
    var id = $('edit-memory-id').value;
    var payload = {
      content: content,
      mtype: $('new-memory-type').value,
      importance: parseInt($('new-memory-importance').value || '3', 10)
    };
    if (currentAgent && currentAgent !== 'all') payload.agent = currentAgent;
    try {
      if (id) {
        await api('/api/memories/' + encodeURIComponent(id), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        toast('记忆已更新', 'success');
      } else {
        await api('/api/memories', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        toast('记忆已添加', 'success');
      }
      closeAddModal();
      loadMemories();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function deleteMemory(id) {
    if (!confirm('确定删除这条记忆？（软删除，可审计）')) return;
    try {
      await api('/api/memories/' + encodeURIComponent(id), { method: 'DELETE' });
      toast('记忆已删除', 'success');
      loadMemories();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------------- 记忆预算器 ----------------
  async function runBudget() {
    var q = $('budget-query').value.trim();
    if (!q) { toast('请输入检索问题', 'error'); return; }
    var budget = parseInt($('budget-tokens').value || '500', 10);
    var k = parseInt($('budget-k').value || '10', 10);
    showSpinner(true);
    try {
      var data = await api('/api/budget-recall', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: q, budget_tokens: budget, k: k })
      });
      var results = data.results || [];
      $('budget-results').innerHTML = results.length ? results.map(function (r) {
        return '<div class="mini-item"><div class="mi-title">' + esc((r.content || '').slice(0, 120)) + '</div>' +
          '<div class="mi-meta"><span>score ' + Number(r.score || 0).toFixed(4) + '</span><span>~' + (r.tokens || 0) + ' tokens</span>' +
          '<span>' + esc(r.mtype || '') + '</span></div></div>';
      }).join('') : '<div class="empty">无匹配记忆</div>';
      var cr = data.cost_report || {};
      $('cost-report').innerHTML = kv([
        ['预算 tokens', cr.budget_tokens],
        ['已选条数', cr.selected_count],
        ['已消耗 tokens', cr.tokens_consumed],
        ['节省 tokens', cr.tokens_saved, cr.tokens_saved > 0 ? 'green' : ''],
        ['无预算基线 tokens', cr.top_k_tokens],
        ['查询 tokens', cr.query_tokens]
      ]);
    } catch (e) { toast(e.message, 'error'); }
    finally { showSpinner(false); }
  }

  // ---------------- 遗忘经济学 ----------------
  async function loadForgetting() {
    try {
      var data = await api('/api/stats' + agentParam(true));
      var tc = data.tier_counts || {};
      $('forgetting-tiers').innerHTML =
        statCard('热层 hot', tc.hot || 0, 'green') +
        statCard('温层 warm', tc.warm || 0, 'yellow') +
        statCard('冷层 cold', tc.cold || 0, 'red');
    } catch (e) { toast(e.message, 'error'); }
  }

  async function runDemoteCycle() {
    var budget = parseInt($('demote-budget').value || '0', 10);
    showSpinner(true);
    try {
      var data = await api('/api/demote-cycle', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ budget_bytes: budget })
      });
      var r = data.report || {};
      $('demote-report').innerHTML = kv([
        ['迁移条数', r.migrations_count],
        ['迁入冷层', r.cold_migrated],
        ['降级 ID 数', (r.demoted || []).length],
        ['预算字节', r.budget_bytes],
        ['结果', data.message || '完成', 'green']
      ]);
      toast('降级周期完成', 'success');
      loadForgetting();
    } catch (e) { toast(e.message, 'error'); }
    finally { showSpinner(false); }
  }

  // ---------------- 记忆公证所 ----------------
  async function loadNotary() {
    try {
      var data = await api('/api/notary?limit=200' + agentParam());
      renderNotary(data.items || []);
    } catch (e) { toast(e.message, 'error'); }
  }

  function renderNotary(items) {
    var list = $('notary-list');
    var filtered = notaryFilter === 'suspicious' ? items.filter(function (i) { return i.suspicious; }) : items;
    if (!filtered.length) { list.innerHTML = '<div class="empty">' + (notaryFilter === 'suspicious' ? '无可疑记忆' : '暂无记忆') + '</div>'; return; }
    list.innerHTML = filtered.map(function (i) {
      var confColor = i.confidence >= 0.7 ? 'green' : (i.confidence >= 0.4 ? 'yellow' : 'red');
      return '<div class="mini-item' + (i.suspicious ? ' suspicious' : '') + '">' +
        '<div class="mi-title">' + (i.suspicious ? '⚠️ ' : '') + esc((i.content || '').slice(0, 120)) + '</div>' +
        '<div class="mi-meta">' +
        '<span>confidence <b style="color:var(--' + confColor + ')">' + Number(i.confidence || 0).toFixed(2) + '</b></span>' +
        '<span>injection ' + Number(i.injection_score || 0).toFixed(2) + '</span>' +
        '<span>' + esc(i.verification || 'unverified') + '</span>' +
        (i.suspicious ? '<span class="badge badge-suspicious">可疑</span>' : '<span class="badge badge-ok">正常</span>') +
        '</div>' +
        (i.injection_flags && i.injection_flags.length ? '<div class="mi-meta"><span>flags: ' + esc(i.injection_flags.join(', ')) + '</span></div>' : '') +
        '</div>';
    }).join('');
  }

  // ---------------- 记忆图谱 ----------------
  function loadGraphTimeline() {
    return api('/api/graph/timeline' + agentParam(true)).then(function (data) {
      graphData = data;
      graphPlaying = false;
      graphReveal = 1;                       // 默认回放到最新（全量显示）
      graphAppear = {};
      graphHoverId = null; graphSelectedId = null;
      // 邻接表 + 节点索引（Hermes adjacency：双向，供聚焦星座图使用；用数组便于 indexOf）
      graphAdjacency = {}; graphNodeById = {};
      (data.nodes || []).forEach(function (n) { graphNodeById[n.id] = n; graphAdjacency[n.id] = []; });
      (data.edges || []).forEach(function (e) {
        if (graphAdjacency[e.source] && graphAdjacency[e.source].indexOf(e.target) < 0) graphAdjacency[e.source].push(e.target);
        if (graphAdjacency[e.target] && graphAdjacency[e.target].indexOf(e.source) < 0) graphAdjacency[e.target].push(e.source);
      });
      var rec = computeRecency(data.nodes || []);
      graphLayout = buildGraphLayout(data.nodes || [], rec.rec, rec.minTs, rec.maxTs, rec.timed);
      buildTrack();
      renderGraph();
    }).catch(function (e) { toast(e.message, 'error'); });
  }

  function buildTrack() {
    var track = $('graph-track');
    track.querySelectorAll('.graph-track-dot').forEach(function (d) { d.remove(); });
    var old = document.getElementById('graph-track-cursor');
    if (old) old.remove();
    var rings = (graphLayout && graphLayout.rings) || [];
    if (!rings.length) return;
    var cursorEl = document.createElement('div');
    cursorEl.className = 'graph-track-cursor'; cursorEl.id = 'graph-track-cursor';
    track.appendChild(cursorEl);
    // 每个环一个时间刻度点（位置按该环的 ratio）
    rings.forEach(function (ring, i) {
      var dot = document.createElement('div');
      dot.className = 'graph-track-dot';
      dot.title = ring.label || ('ring ' + i);
      dot.className += ' memory';
      dot.style.left = ((ring.ratio / 1) * 100) + '%';
      dot.addEventListener('click', function (ev) {
        ev.stopPropagation();
        graphReveal = ring.ratio; graphPlaying = false; setPlayIcon(false); renderGraph();
      });
      track.appendChild(dot);
    });
    track.onclick = function (ev) {
      var rect = track.getBoundingClientRect();
      var ratio = (ev.clientX - rect.left) / rect.width;
      graphReveal = Math.max(0.02, Math.min(1, ratio));
      graphPlaying = false; setPlayIcon(false); renderGraph();
    };
    positionCursor();
  }

  function positionCursor() {
    var cursorEl = document.getElementById('graph-track-cursor');
    var track = $('graph-track');
    if (!cursorEl || !track) return;
    cursorEl.style.left = (clamp(graphReveal, 0, 1) * 100) + '%';
  }

  function setPlayIcon(playing) {
    var icon = $('graph-play-icon');
    if (!icon) return;
    icon.innerHTML = playing ? '<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>' : '<path d="M8 5v14l11-7z"/>';
  }

  function togglePlay() {
    var nodes = (graphData && graphData.nodes) || [];
    if (!nodes.length) return;
    if (graphPlaying) {
      graphPlaying = false; clearInterval(graphPlayTimer); setPlayIcon(false); return;
    }
    if (graphReveal >= 1) graphReveal = 0;   // 回放到头则从头再来
    graphPlaying = true; setPlayIcon(true);
    graphPlayTimer = setInterval(function () {
      graphReveal += 0.02;
      if (graphReveal >= 1) {
        graphReveal = 1; graphPlaying = false; clearInterval(graphPlayTimer); setPlayIcon(false);
      }
      renderGraph();
    }, 120);
  }

  function formatDate(iso) {
    var m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return iso;
    return m[1] + '年' + parseInt(m[2], 10) + '月' + parseInt(m[3], 10) + '日';
  }

  // ========================================================================
  // Hermes starmap 算法核心（逐点对应 apps/desktop/src/app/starmap/*.ts）
  // ========================================================================

  // geometry.ts:11 FNV-1a —— 稳定的按 id 布局种子（角度 / 星场）
  function fnvHash(input) {
    var h = 2166136261;
    var s = String(input);
    for (var i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  // time-axis.ts:9 LEAD_IN —— 最老节点不贴 0，留一拍“长出来”的空拍
  var LEAD_IN = 0.06;
  function recForRatio(ratio) { return LEAD_IN + (1 - LEAD_IN) * clamp(ratio, 0, 1); }

  // constants.ts:6/7 —— 环带内/外半径（按比例映射到画布）
  var RING_INNER = 58, RING_OUTER = 340, RING_STEPS = 4;

  // geometry.ts:22 nodeRadius —— memory 固定 4.4；skill 随 useCount 增长
  function nodeRadius(n) {
    if (n.kind === 'memory') return 4.4;
    var base = (n.state === 'archived' || n.state === 'stale') ? 2.4 : 3;
    return base + Math.sqrt(Math.max(0, n.use_count || 0)) * 0.55 + (n.pinned ? 0.8 : 0);
  }

  // time-axis.ts:22 computeRecency —— 时间归一化为 recency [LEAD_IN, 1]
  function computeRecency(nodes) {
    var known = nodes.map(function (n) { return (typeof n.ts === 'number' && isFinite(n.ts)) ? n.ts : null; })
      .filter(function (v) { return v !== null; });
    var minTs = known.length ? Math.min.apply(null, known) : null;
    var maxTs = known.length ? Math.max.apply(null, known) : null;
    var timed = minTs !== null && maxTs !== null && maxTs > minTs;
    var ordered = nodes.slice().sort(function (a, b) {
      var at = (typeof a.ts === 'number') ? a.ts : Infinity;
      var bt = (typeof b.ts === 'number') ? b.ts : Infinity;
      return at === bt ? String(a.id).localeCompare(String(b.id)) : at - bt;
    });
    var rec = {};
    nodes.forEach(function (n) {
      var ratio;
      if (timed && typeof n.ts === 'number' && minTs !== null && maxTs !== null) {
        ratio = (n.ts - minTs) / (maxTs - minTs);
      } else {
        var i = ordered.map(function (o) { return o.id; }).indexOf(n.id);
        ratio = ordered.length > 1 ? i / (ordered.length - 1) : 0;
      }
      rec[n.id] = recForRatio(ratio);
    });
    return { rec: rec, minTs: minTs, maxTs: maxTs, timed: timed };
  }

  // simulation.ts:30 ringRadius / :37 placeRadius —— 环带内半径（偏向带中间）
  function ringRadius(i, core, band) { return core + i * band; }
  function placeRadius(i, id, core, band) {
    var outer = ringRadius(i, core, band);
    var inner = i > 0 ? ringRadius(i - 1, core, band) : core - band * 0.5;
    var h = (fnvHash(id) % 1000) / 1000;
    return outer - (0.15 + 0.7 * h) * (outer - inner);
  }

  // simulation.ts:91 chooseUnit —— “nice 日历单位”，环数≈[5,12]
  var GRAPH_UNITS = [
    { kind: 'day', step: 1 }, { kind: 'day', step: 2 }, { kind: 'day', step: 7 }, { kind: 'day', step: 14 },
    { kind: 'month', step: 1 }, { kind: 'month', step: 2 }, { kind: 'month', step: 3 }, { kind: 'month', step: 6 }, { kind: 'month', step: 12 }
  ];
  var GRAPH_DAY = 86400;
  function bucketStart(ts, unit) {
    if (unit.kind === 'day') {
      var period = unit.step * GRAPH_DAY;
      return Math.floor(ts / period) * period;
    }
    var d = new Date(ts * 1000);
    d.setUTCHours(0, 0, 0, 0);
    var absMonth = Math.floor((d.getUTCFullYear() * 12 + d.getUTCMonth()) / unit.step) * unit.step;
    d.setUTCFullYear(Math.floor(absMonth / 12), absMonth % 12, 1);
    return Math.floor(d.getTime() / 1000);
  }
  function chooseUnit(stamps, spanDays) {
    var target = clamp(Math.round(4 + Math.log2(Math.max(1, spanDays / 60))), 5, 12);
    var best = GRAPH_UNITS[0], bestScore = Infinity;
    GRAPH_UNITS.forEach(function (u) {
      var starts = {};
      stamps.forEach(function (t) { starts[bucketStart(t, u)] = true; });
      var count = Object.keys(starts).length;
      if (!count) return;
      var score = Math.abs(count - target) + (count > target ? 0.5 : 0);
      if (score < bestScore) { bestScore = score; best = u; }
    });
    return best;
  }
  function bucketLabel(ts, unit) {
    if (unit.kind === 'day') return formatDate(new Date(ts * 1000).toISOString().slice(0, 10));
    var d = new Date(ts * 1000);
    return (d.getUTCFullYear() + '-' + (d.getUTCMonth() + 1));
  }

  // simulation.ts:172 buildLayout —— 每填充日历桶一个等宽环；节点落到环带内、带内簇状点燃
  function buildGraphLayout(nodes, recMap, minTs, maxTs, timed) {
    // Hermes 像素常量：RING_CORE = radiusForRecency(recForRatio(0))≈74.92
    // RING_BAND = (RING_OUTER - RING_CORE)/RING_STEPS ≈ 66.27
    var RING_CORE = 58 + recForRatio(0) * (340 - 58);
    var RING_BAND = (340 - RING_CORE) / RING_STEPS;

    var stamps = nodes.map(function (n) { return (typeof n.ts === 'number' && isFinite(n.ts)) ? n.ts : NaN; })
      .filter(function (t) { return !isNaN(t); });

    if (!(timed && minTs !== null && maxTs !== null && maxTs > minTs && stamps.length)) {
      // evenLayout：无时间跨度 → RING_STEPS+1 等宽环，连续半径
      var evenRings = [];
      for (var i = 0; i <= RING_STEPS; i++) {
        evenRings.push({ label: null, r: RING_CORE + i * RING_BAND, ratio: recForRatio(i / RING_STEPS) });
      }
      var trEven = {}, idxEven = {};
      nodes.forEach(function (n) { trEven[n.id] = 58 + (recMap[n.id] || 0) * (340 - 58); idxEven[n.id] = 0; });
      return { rings: evenRings, rec: recMap, tr: trEven, index: function () { return 0; }, timed: timed };
    }

    var spanDays = (maxTs - minTs) / GRAPH_DAY;
    var unit = chooseUnit(stamps, spanDays);
    var startSet = {}, starts = [];
    stamps.forEach(function (t) { var b = bucketStart(t, unit); if (!(b in startSet)) { startSet[b] = true; starts.push(b); } });
    starts.sort(function (a, b) { return a - b; });
    if (starts.length < 2) {
      return buildGraphLayout(nodes, recMap, minTs, maxTs, false);  // 回退 evenLayout
    }
    var indexOfStart = {};
    starts.forEach(function (s, i) { indexOfStart[s] = i; });
    var last = Math.max(1, starts.length - 1);
    var rings = starts.map(function (s, i) { return { label: bucketLabel(s, unit), r: RING_CORE + i * RING_BAND, ratio: recForRatio(i / last) }; });

    var buckets = starts.map(function () { return []; });
    nodes.forEach(function (n) {
      var ts = (typeof n.ts === 'number' && isFinite(n.ts)) ? n.ts : NaN;
      var i = isFinite(ts) ? (indexOfStart[bucketStart(ts, unit)] !== undefined ? indexOfStart[bucketStart(ts, unit)] : starts.length - 1) : starts.length - 1;
      buckets[i].push(n);
    });
    var tsOf = function (n) { return (typeof n.ts === 'number' && isFinite(n.ts)) ? n.ts : Infinity; };
    var recByNode = {}, trByNode = {}, idxByNode = {};
    var CLUSTER_SIZE = 5;
    buckets.forEach(function (bucket, i) {
      bucket.sort(function (a, b) { var at = tsOf(a), bt = tsOf(b); return at === bt ? String(a.id).localeCompare(String(b.id)) : at - bt; });
      var hi = rings[i].ratio;
      var lo = i > 0 ? rings[i - 1].ratio : 0;
      var m = bucket.length;
      var clusters = Math.max(1, Math.round(m / CLUSTER_SIZE));
      bucket.forEach(function (n, k) {
        var c = Math.min(clusters - 1, Math.floor((k / m) * clusters));
        var jitter = ((fnvHash(n.id) % 100) / 100 - 0.5) * (0.5 / clusters);
        var f = clamp((c + 1) / clusters + jitter, 0.02, 1);
        recByNode[n.id] = lo + f * (hi - lo);
        trByNode[n.id] = placeRadius(i, n.id, RING_CORE, RING_BAND);
        idxByNode[n.id] = i;
      });
    });
    return { rings: rings, rec: recByNode, tr: trByNode, index: function (n) { return idxByNode[n.id] !== undefined ? idxByNode[n.id] : starts.length - 1; }, timed: timed };
  }

  // render.ts:83 SCRAMBLE_CHARS + :748 drawScramble —— 中心 Matrix 数字雨
  var SCRAMBLE_CHARS = 'ﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾜﾝｦｱｳｴｵｶｷｹｺｻｼｽｾﾀﾁﾂﾃﾅﾆﾇﾈ0123456789:.=*+<>Ξ╳';
  function drawScramble(ctx, cx, cy, coreRx) {
    var cell = clamp(coreRx / 18, 5, 13);
    var half = Math.max(3, Math.round(coreRx / cell));
    var now = performance.now();
    var t = now / 1000;
    ctx.save();
    ctx.font = cell + 'px "JetBrains Mono", "Hiragino Sans", "Noto Sans JP", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    for (var r = -half; r <= half; r++) {
      var rowSeed = (r * 19349663) >>> 0 || 1;
      var dir = (rowSeed & 1) ? 1 : -1;
      var speed = 8 + (rowSeed % 16);
      var scroll = (now / 1000) * speed * dir;
      var ny = (r * cell) / coreRx;
      var rowDim = 1 - 0.5 * Math.min(1, Math.abs(ny));
      var kMin = Math.floor((-coreRx - scroll) / cell) - 1;
      var kMax = Math.ceil((coreRx - scroll) / cell) + 1;
      for (var k = kMin; k <= kMax; k++) {
        var sx = k * cell + scroll;
        var nx = sx / coreRx;
        var d2 = nx * nx + ny * ny;
        if (d2 > 1) continue;
        var seed = (rowSeed ^ ((k >>> 0) * 73856093)) >>> 0;
        var ch = SCRAMBLE_CHARS[seed % SCRAMBLE_CHARS.length] || '0';
        var edge = clamp((1 - Math.sqrt(d2)) / 0.4, 0, 1);
        var flick = 0.7 + 0.3 * (((seed >>> 5) % 100) / 100);
        var phase = (seed & 7) * 0.35;
        var glow = Math.sin(nx * 4.5 + t * 1.3 + phase) * Math.sin(ny * 4.5 - t * 0.9 + phase);
        var pop = 1 + clamp((glow - 0.25) / 0.75, 0, 1) * 2.6;
        var a = clamp(0.3 * edge * flick * rowDim * pop, 0, 0.9);
        if (a < 0.02) continue;
        ctx.fillStyle = 'rgba(59,130,246,' + a.toFixed(3) + ')';
        ctx.fillText(ch, cx + sx, cy + r * cell);
      }
    }
    ctx.restore();
  }

  // 节点淡入：新出现的节点在 280ms 内由透明渐显（Hermes reveal 平滑过渡）
  function fadeAlpha(id, now) {
    var t0 = graphAppear[id];
    if (!t0) return 1;
    return Math.min(1, (now - t0) / 280);
  }

  // 碰撞松弛：近似 Hermes d3-force forceCollide（2 次迭代，软性推开重叠）
  function resolveGraphCollisions(cx, cy, scale) {
    for (var pass = 0; pass < 2; pass++) {
      var ids = Object.keys(graphPos);
      for (var i = 0; i < ids.length; i++) {
        for (var j = i + 1; j < ids.length; j++) {
          var a = graphPos[ids[i]], b = graphPos[ids[j]];
          var dx = b.x - a.x, dy = b.y - a.y;
          var dist = Math.sqrt(dx * dx + dy * dy);
          var minDist = (nodeRadius(a.node) + nodeRadius(b.node) + 2) * scale;
          if (dist > 0.01 && dist < minDist) {
            var push = (minDist - dist) / 2;
            var nx = dx / dist, ny = dy / dist;
            a.x -= nx * push; a.y -= ny * push;
            b.x += nx * push; b.y += ny * push;
          }
        }
      }
    }
  }

  // Hermes render.ts:714 fitScale —— 稳定节点缩放（聚焦时不因回放相机而放大）
  function graphFitScale() {
    var wrap = $('graph-canvas-wrap');
    var W = (wrap && wrap.clientWidth) || 600, H = (wrap && wrap.clientHeight) || 540;
    var rings = (graphLayout && graphLayout.rings) || [];
    var outer = rings.length ? rings[rings.length - 1].r : RING_OUTER;
    return Math.max(0.3, Math.min(W, H) / (2 * outer + 40));
  }

  function graphFocusId() {
    return graphSelectedId || graphHoverId || null;
  }

  // render.ts:582-733 聚焦星座图 —— 绘出聚焦节点内容卡 + 邻接节点喷字
  function drawFocusConstellation(ctx, focusId, focusSet, scale, W, H) {
    var focusNode = graphNodeById[focusId];
    var fp = graphPos[focusId];
    if (!focusNode || !fp) return;
    var nodeK = graphFitScale();
    var placed = [];
    // ① 聚焦节点内容卡（透明底卡，悬停时展示标题+正文）
    var tipRect = drawFocusTooltip(ctx, focusNode, fp, nodeK, W, H);
    if (tipRect) placed.push(tipRect);
    // ② 邻接节点喷字（greedy 防重叠，避开内容卡与日期）
    ctx.font = '11px ui-sans-serif, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    var LBL_M = 6, LBL_H = 15, step = LBL_H + 3;
    focusSet.forEach(function (id) {
      if (id === focusId) return;
      var n = graphNodeById[id], p = graphPos[id];
      if (!n || !p) return;
      var label = ellipsize(ctx, (n.label || n.content || '').replace(/\s+/g, ' ').trim(), Math.min(180, W * 0.32));
      if (!label) return;
      var bw = ctx.measureText(label).width + 10;
      var x = clamp(p.x - bw / 2, LBL_M, Math.max(LBL_M, W - bw - LBL_M));
      var top = p.y - (nodeRadius(n) * nodeK + 7) - LBL_H + 4;
      var clampY = function (v) { return clamp(v, LBL_M, Math.max(LBL_M, H - LBL_H - LBL_M)); };
      var y = null;
      for (var k = 0; k <= 7 && y == null; k += 1) {
        var dys = k === 0 ? [0] : [k * step, -k * step];
        for (var di = 0; di < dys.length; di += 1) {
          var cand = { x: x, y: clampY(top + dys[di]), w: bw, h: LBL_H };
          if (!placed.some(function (r) { return rectsOverlap(cand, r); })) { y = cand.y; break; }
        }
      }
      if (y == null) return;
      placed.push({ x: x, y: y, w: bw, h: LBL_H });
      // 浅白底卡 + 深字（与白画布高对比，Hermes chip）
      ctx.fillStyle = 'rgba(255,255,255,0.94)';
      ctx.fillRect(x - 2, y - 1, bw + 4, LBL_H + 2);
      ctx.strokeStyle = 'rgba(15,23,42,0.12)'; ctx.lineWidth = 1;
      ctx.strokeRect(x - 2, y - 1, bw + 4, LBL_H + 2);
      ctx.fillStyle = '#111827';
      ctx.fillText(label, x + bw / 2, y + LBL_H / 2 + 0.5);
    });
    ctx.textBaseline = 'alphabetic';
  }

  function drawFocusTooltip(ctx, node, p, nodeK, W, H) {
    if (!node) return null;
    var PADX = 9, PADY = 7, LINE_H = 16, BADGE_H = 14, ROW_GAP = 4;
    // ① 类型徽标行（技能/记忆 + 类型 + 日期）
    var badge = (node.kind === 'skill' ? '技能' : '记忆') +
      (node.mtype ? ' · ' + String(node.mtype) : '') +
      (node.date ? ' · ' + formatDate(node.date) : '');
    // ② 标题（label 优先）
    var title = String(node.label || node.content || '').replace(/\s+/g, ' ').trim();
    if (!title) title = node.kind === 'skill' ? '技能' : '记忆';
    // ③ 正文预览（content，取前几行；与标题去重）
    var body = '';
    if (node.content) {
      var c = String(node.content).replace(/\r/g, '');
      var firstLine = (c.split('\n')[0] || '').trim();
      if (firstLine !== title) body = c;
      else {
        // 首行即标题 → 从第二行起取正文
        var rest = c.split('\n').slice(1).join('\n').trim();
        body = rest || '';
      }
    }
    body = body.replace(/\s+/g, ' ').trim();

    ctx.font = '10px ui-sans-serif, sans-serif';
    var maxW = Math.min(380, W - 16) - PADX * 2;
    ctx.font = '600 12px ui-sans-serif, sans-serif';
    var titleLines = wrapText(ctx, title, maxW);
    var titleW = 0;
    titleLines.forEach(function (l) { titleW = Math.max(titleW, ctx.measureText(l).width); });
    // 正文最多显示 4 行
    ctx.font = '11px ui-sans-serif, sans-serif';
    var bodyLines = body ? wrapText(ctx, body, maxW).slice(0, 4) : [];
    var bodyW = 0;
    bodyLines.forEach(function (l) { bodyW = Math.max(bodyW, ctx.measureText(l).width); });
    var useW = Math.max(titleW, bodyW, ctx.measureText(badge).width);
    var bgW = useW + PADX * 2;
    var bgH = BADGE_H + ROW_GAP + titleLines.length * LINE_H +
      (bodyLines.length ? ROW_GAP + bodyLines.length * (LINE_H - 2) : 0) + PADY * 2;

    var bx = clamp(p.x - bgW / 2, 4, Math.max(4, W - bgW - 4));
    var by = clamp(p.y - (nodeRadius(node) * nodeK + 10) - bgH, 4, Math.max(4, H - bgH - 4));

    ctx.save();
    ctx.fillStyle = 'rgba(17,24,39,0.95)';
    rect(ctx, bx, by, bgW, bgH, 7);
    ctx.fill();
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    // 徽标行
    ctx.font = '9px ui-sans-serif, sans-serif';
    ctx.fillStyle = 'rgba(156,163,175,0.95)';
    ctx.fillText(badge, bx + PADX, by + PADY + BADGE_H / 2);
    // 标题（反白高亮）
    var ty = by + PADY + BADGE_H + ROW_GAP;
    ctx.font = '600 12px ui-sans-serif, sans-serif';
    ctx.fillStyle = '#ffffff';
    titleLines.forEach(function (line, i) { ctx.fillText(line, bx + PADX, ty + LINE_H * i + LINE_H / 2); });
    // 正文
    if (bodyLines.length) {
      var byy = ty + titleLines.length * LINE_H + ROW_GAP;
      ctx.font = '11px ui-sans-serif, sans-serif';
      ctx.fillStyle = 'rgba(229,231,235,0.85)';
      bodyLines.forEach(function (line, i) {
        var clipped = i === bodyLines.length - 1 && bodyLines.length >= 4 ? ellipsize(ctx, line, maxW) : line;
        ctx.fillText(clipped, bx + PADX, byy + i * (LINE_H - 2) + (LINE_H - 2) / 2);
      });
    }
    ctx.restore();
    return { x: bx, y: by, w: bgW, h: bgH };
  }

  function rect(ctx, x, y, w, h, r) {
    r = r || 0;
    ctx.beginPath();
    if (r > 0) {
      ctx.moveTo(x + r, y);
      ctx.arcTo(x + w, y, x + w, y + h, r);
      ctx.arcTo(x + w, y + h, x, y + h, r);
      ctx.arcTo(x, y + h, x, y, r);
      ctx.arcTo(x, y, x + w, y, r);
    } else {
      ctx.rect(x, y, w, h);
    }
    ctx.closePath();
  }

  function ellipsize(ctx, text, maxW) {
    text = String(text == null ? '' : text);
    if (ctx.measureText(text).width <= maxW) return text;
    var lo = 0, hi = text.length;
    while (lo < hi) {
      var mid = (lo + hi + 1) >> 1;
      if (ctx.measureText(text.slice(0, mid) + '…').width <= maxW) lo = mid; else hi = mid - 1;
    }
    return text.slice(0, lo) + '…';
  }

  function wrapText(ctx, text, maxW) {
    var out = [], cur = '';
    for (var i = 0; i < text.length; i += 1) {
      var c = text[i];
      if (ctx.measureText(cur + c).width <= maxW) {
        cur += c;
      } else {
        out.push(cur); cur = c;
      }
    }
    if (cur) out.push(cur);
    return out;
  }

  function rectsOverlap(a, b) {
    return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
  }

  function drawGraphNode(ctx, p, r, kind, alpha) {
    ctx.globalAlpha = alpha;
    if (kind === 'skill') {
      var g = ctx.createRadialGradient(p.x - r * 0.35, p.y - r * 0.4, r * 0.1, p.x, p.y, r);
      g.addColorStop(0, '#ffffff');
      g.addColorStop(0.35, '#3B82F6');
      g.addColorStop(1, '#1e40af');
      ctx.fillStyle = g;
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, Math.PI * 2); ctx.fill();
    } else {
      var pts = 4, rot = -Math.PI / 2;  // 菱形尖朝上
      ctx.beginPath();
      for (var i = 0; i < pts; i++) {
        var a = rot + (i / pts) * Math.PI * 2;
        var px = p.x + Math.cos(a) * r * 1.15, py = p.y + Math.sin(a) * r * 1.15;
        if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
      }
      ctx.closePath();
      var g2 = ctx.createRadialGradient(p.x - r * 0.3, p.y - r * 0.35, r * 0.1, p.x, p.y, r * 1.15);
      g2.addColorStop(0, '#ffe8c0');
      g2.addColorStop(0.4, '#F59E0B');
      g2.addColorStop(1, '#b45309');
      ctx.fillStyle = g2;
      ctx.fill();
    }
    ctx.globalAlpha = 1;
  }

  function renderGraph() {
    var canvas = $('graph-canvas');
    var wrap = $('graph-canvas-wrap');
    var empty = $('graph-empty');
    if (!graphData || !graphData.nodes || !graphData.nodes.length || !graphLayout) {
      canvas.style.display = 'none'; empty.style.display = 'flex';
      $('graph-legend-date').textContent = ''; graphPos = {}; graphAppear = {};
      if (graphRAF) { cancelAnimationFrame(graphRAF); graphRAF = null; }
      return;
    }
    empty.style.display = 'none'; canvas.style.display = 'block';
    var dpr = window.devicePixelRatio || 1;
    var W = wrap.clientWidth || 600, H = wrap.clientHeight || 540;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#FFFFFF';
    ctx.fillRect(0, 0, W, H);

    var cx = W / 2, cy = H / 2;
    var maxR = Math.max(60, Math.min(W, H) / 2 - 70);
    var scale = maxR / RING_OUTER;
    var rings = graphLayout.rings || [];
    var recById = graphLayout.rec || {};
    var trById = graphLayout.tr || {};
    var nodeK = graphFitScale();

    // ① 中心 Matrix 数字雨
    var coreRx = (rings.length ? rings[0].r : RING_INNER) * scale * 1.25;
    drawScramble(ctx, cx, cy, coreRx);

    // ② 等宽同心圆环 + 日期标注
    rings.forEach(function (ring) {
      var rr = ring.r * scale;
      ctx.beginPath(); ctx.arc(cx, cy, rr, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(210,210,210,0.55)'; ctx.lineWidth = 1; ctx.stroke();
      if (ring.label) {
        ctx.save();
        ctx.fillStyle = '#B0B0B0';
        ctx.font = '11px sans-serif';
        ctx.textAlign = 'left';
        ctx.fillText(ring.label, cx + rr * 0.82, cy - rr * 0.82);
        ctx.restore();
      }
    });

    // ③ 回放筛选：seen(rec) = rec <= reveal + eps
    var seen = {};
    graphData.nodes.forEach(function (n) {
      var r = recById[n.id] !== undefined ? recById[n.id] : 1;
      seen[n.id] = r <= graphReveal + 1e-3;
    });

    // ④ 节点位置
    graphPos = {};
    var now = Date.now();
    graphData.nodes.forEach(function (n) {
      if (!seen[n.id]) return;
      var tr = (trById[n.id] !== undefined ? trById[n.id] : RING_INNER + (recById[n.id] || 0) * (RING_OUTER - RING_INNER)) * scale;
      var angle = ((fnvHash(n.id) % 3600) / 3600) * Math.PI * 2;
      graphPos[n.id] = { x: cx + Math.cos(angle) * tr, y: cy + Math.sin(angle) * tr, node: n };
      if (!graphAppear[n.id]) graphAppear[n.id] = now;
    });
    Object.keys(graphAppear).forEach(function (id) { if (!graphPos[id]) delete graphAppear[id]; });

    // ⑤ 碰撞松弛
    resolveGraphCollisions(cx, cy, scale);

    // ⑥ 聚焦状态：focusId + 邻接集合（Hermes focusId=selected??hover；focusSet=adjacency[focus]）
    var focusId = graphFocusId();
    var focusSet = focusId ? (graphAdjacency[focusId] || []) : null;
    var isFocused = !!focusId && !!focusSet;

    // ⑦ 连线 —— 仅聚焦时显示其网络关系（不聚焦只见点，对齐 Hermes 默认态）
    if (isFocused) {
      var drawn = {};
      (graphData.edges || []).forEach(function (e) {
        var a = graphPos[e.source], b = graphPos[e.target];
        if (!a || !b) return;
        var lit = e.source === focusId || e.target === focusId ||
          (focusSet.indexOf(e.source) >= 0 && focusSet.indexOf(e.target) >= 0);
        if (!lit) return;
        var key = [e.source, e.target].sort().join('|');
        if (drawn[key]) return; drawn[key] = true;
        // 聚焦节点处收缩端点（render.ts:421 focusRingR）
        var x1 = a.x, y1 = a.y, x2 = b.x, y2 = b.y;
        var frr = nodeRadius(a.node) * nodeK + 4;
        if (e.source === focusId) {
          var d1 = Math.hypot(x2 - x1, y2 - y1) || 1;
          x1 += ((x2 - x1) / d1) * frr; y1 += ((y2 - y1) / d1) * frr;
        }
        var frt = nodeRadius(b.node) * nodeK + 4;
        if (e.target === focusId) {
          var d2 = Math.hypot(x1 - x2, y1 - y2) || 1;
          x2 += ((x1 - x2) / d2) * frt; y2 += ((y1 - y2) / d2) * frt;
        }
        ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
        ctx.strokeStyle = 'rgba(15,23,42,0.85)'; ctx.lineWidth = 1.4; ctx.stroke();
      });
    }

    // ⑧ 节点：默认只见点；聚焦时高亮 focus+邻接、弱化其余（Hermes nodeHigh / 0.16）
    var anyFading = false;
    graphData.nodes.forEach(function (n) {
      var p = graphPos[n.id];
      var isFocus = isFocused && n.id === focusId;
      var isNeighbor = isFocused && focusSet.indexOf(n.id) >= 0;
      var high = isFocus || isNeighbor;
      // 未回放到的节点，聚焦星座仍显示（探索优先于时间筛选）
      if (!p || (!seen[n.id] && !high)) return;
      var r = Math.max(1.5, nodeRadius(n) * scale);
      var alpha = fadeAlpha(n.id, now);
      if (alpha < 1) anyFading = true;
      var ageScale = high ? 1 : (isFocused ? 0.5 : 1);
      var baseA = high ? 1 : (isFocused ? 0.14 : 0.82);
      drawGraphNode(ctx, p, Math.max(1.2, r * ageScale), n.kind, baseA * alpha);
      // 聚焦节点外描边（render.ts:524 isFocus stroke）
      if (isFocus) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = 'rgba(15,23,42,0.95)'; ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 3.5, 0, Math.PI * 2); ctx.stroke();
      }
    });

    // ⑨ 聚焦星座：聚焦节点内容卡 + 邻接节点喷字
    if (isFocused) drawFocusConstellation(ctx, focusId, focusSet, scale, W, H);

    positionCursor();

    // ⑩ 平滑过渡：仍有节点淡入时继续下一帧
    if (anyFading) {
      if (graphRAF) cancelAnimationFrame(graphRAF);
      graphRAF = requestAnimationFrame(renderGraph);
    }
  }

  function findNodeAt(mx, my) {
    var best = null, bestDist = 22 * 22;
    Object.keys(graphPos).forEach(function (id) {
      var p = graphPos[id], dx = p.x - mx, dy = p.y - my, d = dx * dx + dy * dy;
      if (d < bestDist) { bestDist = d; best = p.node; }
    });
    return best;
  }

  function bindGraphHover() {
    var canvas = $('graph-canvas'), tooltip = $('graph-tooltip');
    // 悬停 → 临时聚焦该节点，展现其关系网络
    canvas.addEventListener('mousemove', function (ev) {
      var rect = canvas.getBoundingClientRect();
      var hit = findNodeAt(ev.clientX - rect.left, ev.clientY - rect.top);
      var id = hit ? hit.id : null;
      if (id !== graphHoverId) {
        graphHoverId = id;
        tooltip.style.display = 'none';
        if (graphRAF) { cancelAnimationFrame(graphRAF); graphRAF = null; }
        renderGraph();
      }
    });
    // 点击 → 锁定聚焦（再点空白/再次点击同一节点取消）
    canvas.addEventListener('click', function (ev) {
      var rect = canvas.getBoundingClientRect();
      var hit = findNodeAt(ev.clientX - rect.left, ev.clientY - rect.top);
      graphSelectedId = hit ? (graphSelectedId === hit.id ? null : hit.id) : null;
      if (graphRAF) { cancelAnimationFrame(graphRAF); graphRAF = null; }
      renderGraph();
    });
    canvas.addEventListener('mouseleave', function () {
      graphHoverId = null;
      tooltip.style.display = 'none';
      if (graphRAF) { cancelAnimationFrame(graphRAF); graphRAF = null; }
      renderGraph();
    });
  }

  function exportGraph() {
    var canvas = $('graph-canvas');
    if (!canvas || canvas.style.display === 'none') { toast('暂无图谱数据', 'error'); return; }
    var a = document.createElement('a');
    a.download = 'mnemosyne-memory-graph.png';
    a.href = canvas.toDataURL('image/png');
    a.click();
    toast('图谱已导出', 'success');
  }

  // ---------------- 知识树 ----------------
  async function loadTree() {
    try {
      var data = await api('/api/tree?limit=80' + agentParam());
      var container = $('tree-container');
      if (data.tree && data.tree.length) {
        container.innerHTML = data.tree.map(function (node) {
          return '<div class="tree-node">' +
            '<div class="tree-row entity" onclick="toggleTreeNode(this)">' +
            '<span class="tw">▶</span><span class="tl">' + esc(node.label) + '</span>' +
            '<span class="tc">' + node.count + ' 条</span></div>' +
            '<div class="tree-children">' + (node.children || []).map(function (c) {
              return '<div class="tree-row"><span class="tw"></span>' +
                '<span class="tl">' + esc(c.label) + '</span>' +
                '<span class="tc">' + esc(c.mtype || '') + ' · conf ' + Number(c.confidence || 0).toFixed(2) + '</span></div>';
            }).join('') + '</div></div>';
        }).join('');
      } else container.innerHTML = '<div class="empty">暂无实体，写入含实体的记忆后生成知识树</div>';
    } catch (e) { toast(e.message, 'error'); }
  }

  function toggleTreeNode(row) {
    var tw = row.querySelector('.tw');
    var children = row.parentElement.querySelector('.tree-children');
    if (children) {
      children.classList.toggle('open');
      tw.classList.toggle('open');
    }
  }

  // ---------------- 审计日志 ----------------
  async function loadAudit() {
    try {
      var data = await api('/api/audit?limit=200' + agentParam());
      var tl = $('audit-timeline-full');
      tl.innerHTML = (data.entries && data.entries.length) ? data.entries.map(function (e) {
        return '<div class="timeline-item"><div class="action">' + esc(e.action || 'unknown') + '</div>' +
          '<div class="time">' + esc(e.ts || e.timestamp || '') + '</div>' +
          '<div class="details">seq ' + (e.seq || 0) + '</div></div>';
      }).join('') : '<div class="timeline-item"><div class="action">暂无审计记录</div></div>';
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------------- 会话历史 ----------------
  async function loadSessions() {
    try {
      var data = await api('/api/sessions');
      var tbody = $('sessions-table-body');
      if (data.sessions && data.sessions.length) {
        tbody.innerHTML = data.sessions.map(function (s) {
          return '<tr><td class="id-mono">' + esc(s.session_id || s.id || '') + '</td><td>' + (s.turn_count != null ? s.turn_count : '-') + '</td></tr>';
        }).join('');
      } else tbody.innerHTML = '<tr><td colspan="2" class="empty">暂无会话</td></tr>';
      loadTurns();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function searchSessions() {
    var q = $('session-search').value.trim();
    if (!q) { loadSessions(); return; }
    try {
      var data = await api('/api/sessions?q=' + encodeURIComponent(q) + '&k=30');
      var tbody = $('sessions-table-body');
      if (data.results && data.results.length) {
        tbody.innerHTML = data.results.map(function (r) {
          return '<tr><td class="id-mono">' + esc(r.session_id || '') + '</td><td>' + esc((r.content || '').slice(0, 120)) + '</td></tr>';
        }).join('');
      } else tbody.innerHTML = '<tr><td colspan="2" class="empty">无匹配结果</td></tr>';
    } catch (e) { toast(e.message, 'error'); }
  }

  async function loadTurns() {
    try {
      var data = await api('/api/sessions/turns?limit=30');
      var list = $('recent-turns');
      list.innerHTML = (data.turns && data.turns.length) ? data.turns.map(function (r) {
        var role = r.role === 'user' ? '用户' : (r.role === 'assistant' ? '助手' : esc(r.role || ''));
        return '<div class="mini-item"><div class="mi-title"><b>' + role + '：</b>' + esc((r.content || '').slice(0, 120)) + '</div>' +
          '<div class="mi-meta"><span>' + esc((r.session_id || '').slice(0, 16)) + '</span><span>' + esc(r.ts || '') + '</span></div></div>';
      }).join('') : '<div class="empty">暂无会话轮次</div>';
    } catch (e) {}
  }

  // ---------------- 用户画像 ----------------
  async function loadProfiles() {
    try {
      var data = await api('/api/profiles');
      var list = $('profile-content');
      var profiles = data.profiles || [];
      if (profiles.length) {
        list.innerHTML = profiles.map(function (p) {
          return '<div class="mini-item">' +
            '<div class="mi-title">' + esc(p.content) + (p.is_default ? ' <span class="badge badge-active">默认</span>' : '') + '</div>' +
            '<div class="mi-meta"><span>创建 ' + esc((p.created_at || '').slice(0, 16)) + '</span>' +
            '<span>更新 ' + esc((p.updated_at || '').slice(0, 16)) + '</span></div>' +
            '<div class="row-end" style="margin-top:8px;">' +
            (p.is_default ? '' : '<button class="btn btn-secondary btn-sm" onclick="setDefaultProfile(\'' + esc(p.id) + '\')">设为默认</button>') +
            '<button class="btn btn-secondary btn-sm" onclick="editProfile(\'' + esc(p.id) + '\')">编辑</button>' +
            '<button class="btn btn-danger btn-sm" onclick="deleteProfile(\'' + esc(p.id) + '\')">删除</button>' +
            '</div></div>';
        }).join('');
      } else list.innerHTML = '<div class="empty">暂无画像条目，点击右上角“新增画像”</div>';
    } catch (e) { toast(e.message, 'error'); }
  }

  function showProfileForm() {
    editingProfileId = null;
    $('profile-content-input').value = '';
    $('profile-form').style.display = 'block';
  }
  function hideProfileForm() { $('profile-form').style.display = 'none'; }

  function editProfile(id) {
    // 从当前列表中找到内容
    var list = $('profile-content');
    // 简单做法：从已加载数据查找
    api('/api/profiles').then(function (data) {
      var p = (data.profiles || []).find(function (x) { return x.id === id; });
      if (p) {
        editingProfileId = id;
        $('profile-content-input').value = p.content;
        $('profile-form').style.display = 'block';
      }
    });
  }

  async function saveProfile() {
    var content = $('profile-content-input').value.trim();
    if (!content) { toast('画像内容不能为空', 'error'); return; }
    try {
      if (editingProfileId) {
        await api('/api/profiles/' + encodeURIComponent(editingProfileId), {
          method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: content })
        });
        toast('画像已更新', 'success');
      } else {
        await api('/api/profiles', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: content })
        });
        toast('画像已添加', 'success');
      }
      hideProfileForm();
      loadProfiles(); loadSnapshot();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function deleteProfile(id) {
    if (!confirm('确定删除这条画像？')) return;
    try {
      await api('/api/profiles/' + encodeURIComponent(id), { method: 'DELETE' });
      toast('画像已删除', 'success');
      loadProfiles(); loadSnapshot();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function setDefaultProfile(id) {
    try {
      await api('/api/profiles/' + encodeURIComponent(id) + '/default', { method: 'POST' });
      toast('已设为默认画像', 'success');
      loadProfiles(); loadSnapshot();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function loadSnapshot() {
    try {
      var data = await api('/api/snapshot?max_chars=2000');
      $('snapshot-preview').textContent = data.snapshot || '[快照为空]';
    } catch (e) { $('snapshot-preview').textContent = '[快照构建失败] ' + e.message; }
  }

  // ---------------- 多租户 ----------------
  async function loadNamespaces() {
    try {
      var data = await api('/api/namespaces');
      var list = $('namespace-list');
      var namespaces = data.namespaces || [];
      if (!namespaces.length) { list.innerHTML = '<div class="empty">暂无命名空间</div>'; return; }
      list.innerHTML = namespaces.map(function (n) {
        return '<div class="mini-item"><div class="mi-title">' + esc(n.name) +
          (n.current ? ' <span class="badge badge-active">当前</span>' : '') + '</div>' +
          '<div class="mi-meta"><span>' + (n.count || 0) + ' 条记忆</span></div>' +
          '<div class="row-end" style="margin-top:8px;">' +
          (n.current ? '' : '<button class="btn btn-primary btn-sm" onclick="switchNamespace(\'' + esc(n.name) + '\')">切换</button>') +
          '</div></div>';
      }).join('');
    } catch (e) { toast(e.message, 'error'); }
  }

  async function loadNamespacesBadge() {
    try {
      var data = await api('/api/namespaces');
      $('ns-badge').textContent = data.current || 'default';
    } catch (e) {}
  }

  async function switchNamespace(name) {
    if (!confirm('切换到命名空间「' + name + '」？')) return;
    showSpinner(true);
    try {
      await api('/api/namespace/switch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ namespace: name })
      });
      toast('已切换到 ' + name, 'success');
      loadNamespaces(); loadNamespacesBadge(); loadDashboard();
    } catch (e) { toast(e.message, 'error'); }
    finally { showSpinner(false); }
  }

  // ---------------- 容量监控 ----------------
  async function loadCapacity() {
    try {
      var data = await api('/api/stats' + agentParam(true));
      var cap = data.capacity || {};
      var su = data.storage_usage || {};
      $('capacity-detail').innerHTML = kv([
        ['活跃记忆', cap.active_count != null ? cap.active_count : data.active_memories],
        ['记忆总数', data.total_memories],
        ['已删除', data.deleted_memories],
        ['最大活跃上限', cap.max_active_memories != null ? cap.max_active_memories : '无限制'],
        ['使用率', (cap.percentage != null ? cap.percentage + '%' : 'N/A')],
        ['磁盘总量 (MB)', su.total_mb],
        ['磁盘已用 (MB)', su.used_mb],
        ['磁盘剩余 (MB)', su.free_mb, su.free_mb > 1000 ? 'green' : 'yellow'],
        ['后端', data.backend],
        ['命名空间', data.namespace]
      ]);
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------------- 账本 ----------------
  async function loadLedger() {
    verifyLedgerDetail();
    loadLedgerEntries();
  }

  async function verifyLedger() {
    try {
      var data = await api('/api/ledger/verify');
      renderLedgerStatus(data);
      toast(data.valid ? '账本验证通过' : '账本验证失败', data.valid ? 'success' : 'error');
    } catch (e) { toast(e.message, 'error'); }
  }

  async function verifyLedgerDetail() {
    try {
      var data = await api('/api/ledger/verify');
      $('ledger-verify-result').innerHTML = kv([
        ['完整性', data.valid ? '有效 ✓' : '无效 ✗', data.valid ? 'green' : 'red'],
        ['总条目数', data.total_entries],
        ['最新哈希', data.latest_hash ? String(data.latest_hash).slice(0, 32) + '…' : 'N/A'],
        ['校验状态', data.verified ? '已校验' : '异常', data.verified ? 'green' : 'red']
      ]);
    } catch (e) { toast(e.message, 'error'); }
  }

  async function loadLedgerEntries() {
    try {
      var data = await api('/api/ledger/entries?limit=50');
      var list = $('ledger-entries');
      list.innerHTML = (data.entries && data.entries.length) ? data.entries.map(function (e) {
        return '<div class="mini-item"><div class="mi-title">' + esc(e.action || 'unknown') + '</div>' +
          '<div class="mi-meta"><span>seq ' + (e.seq || 0) + '</span><span>' + esc(e.ts || e.timestamp || '') + '</span>' +
          '<span class="id-mono">' + esc(String(e.hash || '').slice(0, 16)) + '</span></div></div>';
      }).join('') : '<div class="empty">暂无账本条目</div>';
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------------- 导入导出 ----------------
  async function exportMemories() {
    showSpinner(true);
    try {
      var resp = await fetch('/api/export', { credentials: 'same-origin' });
      if (!resp.ok) {
        var j = await resp.json().catch(function () { return {}; });
        throw new Error(j.error || ('导出失败 ' + resp.status));
      }
      var blob = await resp.blob();
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = 'mnemosyne-memories.zip'; a.click();
      URL.revokeObjectURL(url);
      toast('导出成功', 'success');
    } catch (e) { toast(e.message, 'error'); }
    finally { showSpinner(false); }
  }

  async function importMemories() {
    var file = $('import-file').files[0];
    if (!file) { toast('请选择文件', 'error'); return; }
    var fd = new FormData();
    fd.append('file', file);
    showSpinner(true);
    try {
      var resp = await fetch('/api/import', { method: 'POST', credentials: 'same-origin', body: fd });
      var data = await resp.json().catch(function () { return {}; });
      if (!resp.ok) throw new Error(data.error || '导入失败');
      $('import-result').innerHTML = kv([
        ['导入条数', data.imported, data.imported > 0 ? 'green' : 'yellow'],
        ['命名空间', data.namespace || 'default'],
        ['结果', data.error ? data.error : '成功', data.error ? 'red' : 'green']
      ]);
      toast('导入完成：' + (data.imported || 0) + ' 条', 'success');
      loadDashboard();
    } catch (e) { toast(e.message, 'error'); }
    finally { showSpinner(false); }
  }

  async function exportConfig() {
    try {
      var resp = await fetch('/api/config/export', { credentials: 'same-origin' });
      var blob = await resp.blob();
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url; a.download = 'web_config.json'; a.click();
      URL.revokeObjectURL(url);
      toast('Web 配置已导出', 'success');
    } catch (e) { toast(e.message, 'error'); }
  }

  async function importConfig() {
    var file = $('config-file').files[0];
    if (!file) { toast('请选择配置文件', 'error'); return; }
    try {
      var text = await file.text();
      var body = JSON.parse(text);
      var data = await api('/api/config/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      });
      toast(data.message || '配置已导入', 'success');
      loadProfiles();
    } catch (e) { toast('配置导入失败：' + e.message, 'error'); }
  }

  // ---------------- 知识库上传 ----------------
  function initKnowledge() {
    var dz = $('dropzone'), input = $('kb-file');
    dz.addEventListener('click', function () { input.click(); });
    dz.addEventListener('dragover', function (e) { e.preventDefault(); dz.classList.add('drag'); });
    dz.addEventListener('dragleave', function () { dz.classList.remove('drag'); });
    dz.addEventListener('drop', function (e) {
      e.preventDefault(); dz.classList.remove('drag');
      if (e.dataTransfer.files.length) uploadKnowledge(e.dataTransfer.files[0]);
    });
    input.addEventListener('change', function () {
      if (input.files.length) uploadKnowledge(input.files[0]);
    });
  }

  async function uploadKnowledge(file) {
    var ext = (file.name.split('.').pop() || '').toLowerCase();
    var supported = ['txt', 'md', 'markdown', 'csv', 'json'];
    if (supported.indexOf(ext) < 0) { toast('暂不支持 .' + ext + ' 格式，请上传 .txt / .md / .csv / .json', 'error'); return; }
    $('kb-meta').textContent = '已选择：' + file.name + '（' + (file.size / 1024).toFixed(1) + ' KB）';
    var fd = new FormData();
    fd.append('file', file);
    $('kb-progress').style.display = 'block';
    $('kb-progress-fill').style.width = '30%';
    try {
      var resp = await fetch('/api/knowledge/upload', { method: 'POST', credentials: 'same-origin', body: fd });
      var data = await resp.json().catch(function () { return {}; });
      $('kb-progress-fill').style.width = '100%';
      if (!resp.ok) throw new Error(data.error || '上传失败');
      $('kb-result').innerHTML = kv([
        ['写入条数', data.written, data.written > 0 ? 'green' : 'yellow'],
        ['拆分块数', data.chunks],
        ['文件名', data.filename]
      ]);
      toast('知识库上传完成：写入 ' + data.written + ' 条', 'success');
      loadDashboard();
    } catch (e) { toast(e.message, 'error'); }
    finally {
      setTimeout(function () { $('kb-progress').style.display = 'none'; $('kb-progress-fill').style.width = '0%'; }, 800);
    }
  }

  // ---------------- 热力图 / 长期画像 ----------------
  async function loadHeatmap() {
    try {
      var data = await api('/api/heatmap' + agentParam(true));
      var notice = $('heatmap-notice');
      // 实时生成：数据即刻展示，不再等待 5 天
      notice.className = 'notice ok';
      notice.textContent = '实时生成 · 累计 ' + data.span_days + ' 天 / 活跃 ' + data.active_days +
        ' 天 · 新增 ' + (data.total_added || 0) + ' 条 / 访问 ' + (data.total_accessed || 0) + ' 次';
      drawHeatmap(data.days || []);
      loadInsights();
    } catch (e) { toast(e.message, 'error'); }
  }

  function drawHeatmap(days) {
    var canvas = $('heatmap-canvas');
    if (!canvas) return;
    var W = canvas.width = canvas.clientWidth || 900;
    var H = canvas.height = 160;
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, W, H);
    var cell = 13, gap = 3, left = 40, top = 24;
    var weeks = Math.floor((W - left - 10) / (cell + gap));
    var maxVal = 0;
    days.forEach(function (d) { maxVal = Math.max(maxVal, d.added); });
    maxVal = maxVal || 1;

    days.forEach(function (d, i) {
      var col = Math.floor(i / 7), row = i % 7;
      if (col >= weeks) return;
      var x = left + col * (cell + gap), y = top + row * (cell + gap);
      var intensity = d.added > 0 ? Math.min(1, 0.25 + (d.added / maxVal) * 0.75) : 0.05;
      ctx.fillStyle = d.added > 0
        ? 'rgba(0,212,255,' + intensity.toFixed(2) + ')'
        : 'rgba(255,255,255,0.04)';
      ctx.fillRect(x, y, cell, cell);
      ctx.strokeStyle = 'rgba(255,255,255,0.05)';
      ctx.strokeRect(x, y, cell, cell);
    });

    ctx.fillStyle = '#8b93a7';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'left';
    ['日', '一', '二', '三', '四', '五', '六'].forEach(function (label, i) {
      ctx.fillText(label, 12, top + i * (cell + gap) + cell / 2 + 3);
    });
    ctx.fillStyle = '#8b93a7';
    ctx.fillText('色深 = 当日新增记忆数量（越亮越多）', left, H - 8);
  }

  async function loadInsights() {
    try {
      var data = await api('/api/insights' + agentParam(true));
      $('insights-text').textContent = data.description || '';
      $('insights-meta').innerHTML = kv([
        ['关注主题', (data.top_entities || []).map(function (e) { return e.name; }).join('、') || '-'],
        ['主要记忆类型', (data.top_mtypes || []).map(function (m) { return m.type; }).join('、') || '-'],
        ['最活跃时段', data.top_hour != null ? (data.top_hour + ':00') : '-'],
        ['最活跃星期', data.top_dow || '-'],
        ['记忆总量', data.total_memories]
      ]);
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------------- 设置 ----------------
  async function loadSettings() {
    // 账户信息
    $('current-username').textContent = currentUser || 'admin';
    try {
      var nsData = await api('/api/namespaces');
      $('settings-namespace').textContent = nsData.current || 'default';
    } catch (e) { $('settings-namespace').textContent = 'default'; }
    // Agent 下拉
    try {
      var data = await api('/api/agent-config');
      var sel = $('agent-select');
      sel.innerHTML = '<option value="">请选择 Agent</option>' + (data.agents || []).map(function (a) {
        return '<option value="' + esc(a) + '"' + (data.agent_config && data.agent_config.agent === a ? ' selected' : '') + '>' + esc(a) + '</option>';
      }).join('');
      if (data.agent_config && data.agent_config.agent) {
        $('agent-status').textContent = '当前已保存：' + data.agent_config.agent + '（偏好保存，自动配置后续版本提供）';
      }
    } catch (e) {}
    loadAgents();
    loadSources();
  }

  // ---------------- 智能体管理 ----------------
  async function loadAgents() {
    try {
      var data = await api('/api/agents');
      agentsCache = data.agents || [];
      renderAgentSelector();
      renderAgentList();
    } catch (e) { toast(e.message, 'error'); }
  }

  function renderAgentSelector() {
    var sel = $('agent-select-top');
    if (!sel) return;
    var html = '<option value="all"' + (currentAgent === 'all' ? ' selected' : '') + '>全部智能体</option>';
    agentsCache.forEach(function (a) {
      html += '<option value="' + esc(a.id) + '"' + (currentAgent === a.id ? ' selected' : '') + '>' +
        esc(a.name) + '（' + a.count + '）</option>';
    });
    sel.innerHTML = html;
  }

  function selectAgent(id) {
    currentAgent = id || 'all';
    localStorage.setItem('mnemosyne_agent', currentAgent);
    renderAgentSelector();
    // 实时刷新当前页数据
    if (currentPage === 'dashboard') { loadDashboard(); loadAgents(); }
    else if (currentPage === 'memories') { loadMemories(); }
    else if (currentPage === 'graph') { graphData = null; loadGraphTimeline(); }
    else if (currentPage === 'notary') { loadNotary(); }
    else if (currentPage === 'tree') { loadTree(); }
    else if (currentPage === 'audit') { loadAudit(); }
    else if (currentPage === 'forgetting') { loadForgetting(); }
    else if (currentPage === 'heatmap') { loadHeatmap(); }
    else if (currentPage === 'capacity') { loadCapacity(); }
    else if (currentPage === 'sessions' || currentPage === 'profile' || currentPage === 'knowledge') { /* 全局视图 */ }
    else { loadDashboard(); }
    toast('已切换到：' + currentAgentName(), 'success');
  }

  function renderAgentList() {
    var list = $('agent-list');
    if (!list) return;
    if (!agentsCache.length) {
      list.innerHTML = '<div class="empty">暂无智能体，请在上方添加</div>';
      return;
    }
    list.innerHTML = agentsCache.map(function (a) {
      return '<div class="mini-item"><div class="mi-title">' + esc(a.name) +
        ' <span class="id-mono">(' + esc(a.project || '') + ')</span></div>' +
        '<div class="mi-meta"><span>' + a.count + ' 条记忆</span><span>近7天 ' + (a.recent7 || 0) + ' 条</span></div>' +
        '<div class="row-end" style="margin-top:6px;">' +
        '<button class="btn btn-danger btn-sm" onclick="deleteAgent(\'' + esc(a.id) + '\')">删除</button>' +
        '</div></div>';
    }).join('');
  }

  async function addAgent() {
    var name = $('agent-new-name').value.trim();
    if (!name) { toast('请输入智能体名称', 'error'); return; }
    try {
      await api('/api/agents', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: name }) });
      $('agent-new-name').value = '';
      toast('智能体已添加', 'success');
      loadAgents();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function deleteAgent(id) {
    if (!confirm('删除该智能体配置？（其记忆数据保留，可在“全部智能体”中查看）')) return;
    try {
      await api('/api/agents/' + encodeURIComponent(id), { method: 'DELETE' });
      if (currentAgent === id) { currentAgent = 'all'; localStorage.setItem('mnemosyne_agent', 'all'); }
      toast('智能体已删除', 'success');
      loadAgents();
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------------- 外部数据源 ----------------
  async function loadSources() {
    try {
      var data = await api('/api/sources');
      renderSourceList(data.sources || []);
    } catch (e) {}
  }

  function renderSourceList(sources) {
    var list = $('source-list');
    if (!list) return;
    if (!sources.length) {
      list.innerHTML = '<div class="empty">暂无外部数据源</div>';
      return;
    }
    list.innerHTML = sources.map(function (s) {
      var last = s.last_result ? (s.last_result.error ? ('失败：' + esc(s.last_result.error)) : ('写入 ' + s.last_result.written + ' 条')) : '未同步';
      return '<div class="mini-item"><div class="mi-title">' + esc(s.name) + ' <span class="badge badge-active">' + esc(s.type) + '</span></div>' +
        '<div class="mi-meta"><span>' + esc(s.url || s.path || '') + '</span></div>' +
        '<div class="mi-meta"><span>' + last + '</span>' + (s.last_sync ? '<span>' + esc(String(s.last_sync).slice(0, 16)) + '</span>' : '') + '</div>' +
        '<div class="row-end" style="margin-top:6px;">' +
        '<button class="btn btn-secondary btn-sm" onclick="syncSource(\'' + esc(s.id) + '\')">立即同步</button>' +
        '<button class="btn btn-danger btn-sm" onclick="deleteSource(\'' + esc(s.id) + '\')">删除</button>' +
        '</div></div>';
    }).join('');
  }

  async function addSource() {
    var name = $('src-name').value.trim();
    var type = $('src-type').value;
    var url = $('src-url').value.trim();
    var agent = $('src-agent').value.trim();
    if (!name) { toast('请输入数据源名称', 'error'); return; }
    var body = { name: name, type: type, agent: agent || null };
    if (type === 'api') body.url = url; else body.path = url;
    try {
      await api('/api/sources', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      $('src-name').value = ''; $('src-url').value = ''; $('src-agent').value = '';
      toast('数据源已添加', 'success');
      loadSources();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function deleteSource(id) {
    if (!confirm('删除该外部数据源？')) return;
    try {
      await api('/api/sources/' + encodeURIComponent(id), { method: 'DELETE' });
      toast('数据源已删除', 'success');
      loadSources();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function syncSource(id) {
    showSpinner(true);
    try {
      var data = await api('/api/sources/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: id }) });
      toast(data.message || '同步完成', 'success');
      loadSources();
      loadAgents();
    } catch (e) { toast(e.message, 'error'); }
    finally { showSpinner(false); }
  }

  async function changeUsername() {
    var nu = $('new-username').value.trim();
    if (!nu) { toast('请输入新用户名', 'error'); return; }
    try {
      var data = await api('/api/auth/change-username', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: nu })
      });
      currentUser = data.username || nu;
      $('user-chip').textContent = currentUser;
      $('current-username').textContent = currentUser;
      $('new-username').value = '';
      toast(data.message || '用户名已修改', 'success');
    } catch (e) { toast(e.message, 'error'); }
  }

  async function renameNamespace() {
    var nn = $('namespace-new').value.trim();
    if (!nn) { toast('请输入新命名空间名称', 'error'); return; }
    if (!confirm('将当前命名空间重命名为「' + nn + '」？（克隆数据并切换，原命名空间保留）')) return;
    showSpinner(true);
    try {
      var data = await api('/api/namespace/rename', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ namespace: nn })
      });
      toast(data.message || '命名空间已重命名', 'success');
      $('namespace-new').value = '';
      $('settings-namespace').textContent = data.namespace || nn;
      loadNamespacesBadge();
      loadDashboard();
    } catch (e) { toast(e.message, 'error'); }
    finally { showSpinner(false); }
  }

  async function saveAgent() {
    var agent = $('agent-select').value;
    if (!agent) { toast('请选择一个 Agent', 'error'); return; }
    try {
      await api('/api/agent-config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ agent: agent })
      });
      $('agent-status').textContent = '已选择 ' + agent + '，自动配置功能将在后续版本提供，当前仅保存偏好。';
      toast('Agent 偏好已保存', 'success');
    } catch (e) { toast(e.message, 'error'); }
  }

  // ---------------- 工具函数 ----------------
  function kv(pairs) {
    return pairs.map(function (p) {
      var val = p[1];
      if (val == null || val === '') val = '-';
      var cls = p[2] || '';
      return '<div class="kv"><div class="k">' + esc(p[0]) + '</div><div class="v ' + cls + '">' + esc(String(val)) + '</div></div>';
    }).join('');
  }
  function statCard(label, val, color) {
    return '<div class="stat-card"><div class="stat-value mono" style="color:var(--' + color + ')">' + val + '</div><div class="stat-label">' + esc(label) + '</div></div>';
  }

  // ---------------- 粒子背景 ----------------
  function initBackground() {
    var canvas = $('bg-canvas');
    var ctx = canvas.getContext('2d');
    var particles = [];
    function resize() {
      canvas.width = window.innerWidth;
      canvas.height = window.innerHeight;
    }
    function spawn() {
      for (var i = 0; i < 70; i++) {
        particles.push({
          x: Math.random() * canvas.width, y: Math.random() * canvas.height,
          vx: (Math.random() - 0.5) * 0.3, vy: (Math.random() - 0.5) * 0.3,
          r: Math.random() * 1.6 + 0.4, hue: Math.random() < 0.6 ? '#00D4FF' : '#A855F7'
        });
      }
    }
    function tick() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      particles.forEach(function (p) {
        p.x += p.vx; p.y += p.vy;
        if (p.x < 0 || p.x > canvas.width) p.vx *= -1;
        if (p.y < 0 || p.y > canvas.height) p.vy *= -1;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fillStyle = p.hue;
        ctx.globalAlpha = 0.5;
        ctx.fill();
      });
      ctx.globalAlpha = 1;
      requestAnimationFrame(tick);
    }
    resize(); spawn(); tick();
    window.addEventListener('resize', resize);
  }

  // ---------------- 事件绑定 ----------------
  function bindEvents() {
    $('login-btn').addEventListener('click', login);
    $('login-password').addEventListener('keydown', function (e) { if (e.key === 'Enter') login(); });
    $('logout-btn').addEventListener('click', logout);
    $('sidebar-toggle').addEventListener('click', function () {
      $('sidebar').classList.toggle('collapsed');
      $('main-content').classList.toggle('expanded');
    });
    $('lang-switch').addEventListener('click', function () {
      lang = (lang === 'zh') ? 'en' : 'zh';
      localStorage.setItem('mnemosyne_lang', lang);
      $('lang-label').textContent = (lang === 'zh') ? '中文' : 'English';
      buildNav();
      $('page-title').textContent = t(currentPage) || '仪表盘';
    });
    // 登录模式选择
    document.querySelectorAll('#login-mode-seg .mode-seg-btn').forEach(function (b) {
      b.addEventListener('click', function () { setMode(b.getAttribute('data-mode')); });
    });
    document.querySelectorAll('#settings-mode-seg .mode-seg-btn').forEach(function (b) {
      b.addEventListener('click', function () { setMode(b.getAttribute('data-mode')); });
    });
    // 公证所过滤
    document.querySelectorAll('#notary-filter .seg-btn').forEach(function (b) {
      b.addEventListener('click', function () {
        document.querySelectorAll('#notary-filter .seg-btn').forEach(function (x) { x.classList.remove('active'); });
        b.classList.add('active');
        notaryFilter = b.getAttribute('data-f');
        loadNotary();
      });
    });
    // 图谱
    $('graph-play-btn').addEventListener('click', togglePlay);
    $('graph-export').addEventListener('click', exportGraph);
    // 智能体选择器
    $('agent-select-top').addEventListener('change', function () {
      selectAgent(this.value);
    });
    window.addEventListener('resize', function () {
      if (currentPage === 'graph' && graphData) renderGraph();
      if (currentPage === 'heatmap') { /* 重绘可选 */ }
    });
    // 弹窗遮罩点击关闭
    $('add-modal').addEventListener('click', function (e) { if (e.target === $('add-modal')) closeAddModal(); });
    // 知识库
    initKnowledge();
  }

  // ---------------- 启动 ----------------
  bindEvents();
  initBackground();
  bindGraphHover();
  setMode(mode, false);
  checkAuth();

  // 暴露给内联 onclick
  window.showPage = showPage;
  window.openAddModal = openAddModal;
  window.closeAddModal = closeAddModal;
  window.saveMemory = saveMemory;
  window.editMemory = editMemory;
  window.deleteMemory = deleteMemory;
  window.runBudget = runBudget;
  window.runDemoteCycle = runDemoteCycle;
  window.verifyLedger = verifyLedger;
  window.verifyLedgerDetail = verifyLedgerDetail;
  window.exportMemories = exportMemories;
  window.importMemories = importMemories;
  window.exportConfig = exportConfig;
  window.importConfig = importConfig;
  window.toggleTreeNode = toggleTreeNode;
  window.setDefaultProfile = setDefaultProfile;
  window.editProfile = editProfile;
  window.deleteProfile = deleteProfile;
  window.showProfileForm = showProfileForm;
  window.hideProfileForm = hideProfileForm;
  window.saveProfile = saveProfile;
  window.switchNamespace = switchNamespace;
  window.changePassword = changePassword;
  window.changeUsername = changeUsername;
  window.renameNamespace = renameNamespace;
  window.saveAgent = saveAgent;
  window.selectAgent = selectAgent;
  window.addAgent = addAgent;
  window.deleteAgent = deleteAgent;
  window.addSource = addSource;
  window.deleteSource = deleteSource;
  window.syncSource = syncSource;
})();
