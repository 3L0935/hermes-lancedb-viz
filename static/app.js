// ═══════════════════════════════════════════════
// LanceDB — Navigation + Pages
// ═══════════════════════════════════════════════

const API = '/api';
let currentPage = 'dashboard';

function switchPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  const pageEl = document.getElementById('page-' + name);
  if (pageEl) pageEl.classList.add('active');
  const navEl = document.querySelector('.nav-item[data-page="' + name + '"]');
  if (navEl) navEl.classList.add('active');
  currentPage = name;

  const fleg = document.getElementById('fresh-legend');
  if (fleg) fleg.style.display = name === 'graph' ? 'flex' : 'none';

  if (name === 'dashboard') loadDashboard();
  else if (name === 'memories') loadMemories();
  else if (name === 'timeline') loadTimeline();
  else if (name === 'tags') loadTags();
  else if (name === 'duplicates') loadDuplicates();
  else if (name === 'embedding') loadEmbedding();
  else if (name === 'clusters') loadClusters();
  else if (name === 'stale') loadStale();
  else if (name === 'graph') { if (typeof loadGraph === 'function') loadGraph(); }
  updateTopStats();
}

function updateTopStats() {
  fetch(API + '/stats').then(r => r.json()).then(s => {
    setEl('mem-count', s.total_memories != null ? s.total_memories : '--');
    setEl('ent-count', s.total_entities != null ? s.total_entities : '--');
    setEl('db-size', s.db_size_mb != null ? s.db_size_mb + ' MB' : '');
  }).catch(() => {});
}

function catBadge(cat) {
  return '<span class="cat-badge cat-' + (cat || 'fact') + '">' + (colorLabel[cat] || cat || 'Fact') + '</span>';
}

function age(ts) {
  if (!ts) return '?';
  const h = (Date.now()/1000 - ts) / 3600;
  if (h < 1) return Math.round(h*60) + 'm';
  if (h < 24) return Math.round(h) + 'h';
  if (h < 720) return Math.round(h/24) + 'd';
  return (h/720).toFixed(1) + 'mo';
}

function badgeForTier(t) {
  if (t === '1') return '<span style="background:rgba(239,68,68,0.15);color:#fca5a5;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600;">T1</span>';
  if (t === '2') return '<span style="background:rgba(245,158,11,0.15);color:#fbbf24;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600;">T2</span>';
  if (t === '3') return '<span style="background:rgba(99,102,241,0.15);color:#a5b4fc;padding:1px 6px;border-radius:4px;font-size:10px;font-weight:600;">T3</span>';
  return '';
}

// ═══════════════════════════════════════════════
// Dashboard
// ═══════════════════════════════════════════════

async function loadDashboard() {
  const el = document.getElementById('dash-stats');
  el.innerHTML = '<div class="loading"><div class="spinner"></div><div>Loading...</div></div>';
  try {
    const [stats, dash] = await Promise.all([
      fetch(API + '/stats').then(r => r.json()),
      fetch(API + '/dashboard').then(r => r.json())
    ]);

    const tiers = dash.tiers || {};
    const cats = dash.categories || {};

    // Stats cards: categories + tiers + totals
    const statsHtml = [];
    Object.entries(cats).sort((a,b) => b[1]-a[1]).forEach(([k,v]) => {
      statsHtml.push([(colorLabel[k] || k), v || 0, colorHex[k] || colorHexDef]);
    });
    if (tiers['1']) statsHtml.push(['T1 Critical', tiers['1'], '#fca5a5']);
    if (tiers['2']) statsHtml.push(['T2 Important', tiers['2'], '#fbbf24']);
    if (tiers['3']) statsHtml.push(['T3 Context', tiers['3'], '#a5b4fc']);
    statsHtml.push(
      ['Total', stats.total_memories || 0, 'var(--accent)'],
      ['Entities', stats.total_entities || 0, '#6ee7b7'],
      ['DB Size', (stats.db_size_mb || '?') + ' MB', 'var(--muted)'],
    );
    el.innerHTML = statsHtml.map(([label,val,color]) =>
      '<div class="stat-card"><div class="stat-value" style="color:' + color + ';font-size:' + (typeof val === 'string' ? '16px' : '22px') + '">' + val + '</div><div class="stat-label">' + label + '</div></div>'
    ).join('');

    // Categories bar chart
    const catMax = Math.max(1, ...Object.values(cats));
    document.getElementById('dash-categories').innerHTML = Object.entries(cats)
      .sort((a,b) => b[1]-a[1]).map(([k,v]) =>
        '<div class="bar-row"><span class="bar-label">' + (colorLabel[k] || k) + '</span><div class="bar-track"><div class="bar-fill" style="width:' + (v/catMax*100).toFixed(0) + '%;background:' + (colorHex[k] || colorHexDef) + '"></div></div><span class="bar-count">' + v + '</span></div>'
      ).join('');

    // Tiers bar chart
    const tMax = Math.max(1, ...Object.values(tiers));
    document.getElementById('dash-tiers').innerHTML = Object.entries(tiers)
      .sort((a,b) => a[0].localeCompare(b[0])).map(([k,v]) =>
        '<div class="bar-row"><span class="bar-label">Tier ' + k + '</span><div class="bar-track"><div class="bar-fill" style="width:' + (v/tMax*100).toFixed(0) + '%;background:' + ({'1':'#fca5a5','2':'#fbbf24','3':'#a5b4fc'}[k] || '#6366f1') + '"></div></div><span class="bar-count">' + v + '</span></div>'
      ).join('') || '<div class="empty-state">No tier data</div>';

    // Tags
    const tags = dash.tags || {};
    const tagEntries = Object.entries(tags).sort((a,b) => b[1]-a[1]).slice(0, 10);
    const tagMax = Math.max(1, ...tagEntries.map(([,v])=>v));
    document.getElementById('dash-tags').innerHTML = tagEntries.length
      ? tagEntries.map(([k,v]) =>
          '<div class="bar-row"><span class="bar-label">' + k + '</span><div class="bar-track"><div class="bar-fill" style="width:' + (v/tagMax*100).toFixed(0) + '%;background:#a78bfa"></div></div><span class="bar-count">' + v + '</span></div>'
        ).join('')
      : '<div class="empty-state">No tags</div>';

    // Top accessed
    const top = dash.top_accessed || [];
    document.getElementById('dash-accessed').innerHTML = top.length
      ? top.map(m => '<div class="timeline-item" onclick="showMemoryDetail(\'' + m.id + '\')">' + catBadge(m.category) + ' ' + (m.content || '').substring(0,80) + ' <span style="color:var(--muted);font-size:10px;">(' + (m.access_count || 0) + 'x)</span></div>').join('')
      : '<div class="empty-state">No data</div>';

  } catch(e) {
    el.innerHTML = '<div class="error-state">Error: ' + e.message + '</div>';
  }
}

// ═══════════════════════════════════════════════
// Memories
// ═══════════════════════════════════════════════

let memOffset = 0, MEM_LIMIT = 20, memSelected = new Set();
function toggleSelectAll() {
  const cb = document.getElementById('mem-sel-all');
  document.querySelectorAll('.mem-cb').forEach(c => c.checked = cb.checked);
  updateBulkBar();
}
function updateBulkBar() {
  memSelected = new Set();
  document.querySelectorAll('.mem-cb:checked').forEach(c => memSelected.add(c.value));
  const bar = document.getElementById('mem-bulk-bar');
  if (memSelected.size > 0) { bar.classList.add('visible'); document.getElementById('mem-bulk-count').textContent = memSelected.size + ' selected'; }
  else bar.classList.remove('visible');
}

async function loadMemories() {
  const tbody = document.getElementById('mem-tbody');
  tbody.innerHTML = '<tr><td colspan="7" class="loading">Loading...</td></tr>';
  const params = new URLSearchParams({
    offset: memOffset, limit: MEM_LIMIT,
    search: document.getElementById('mem-search').value,
    category: document.getElementById('mem-category').value,
    type: document.getElementById('mem-type').value,
    tier: document.getElementById('mem-tier').value || '',
  });
  try {
    const r = await fetch(API + '/memories?' + params.toString());
    const data = await r.json();
    const mems = data.memories || [];
    tbody.innerHTML = mems.map(m =>
      '<tr onclick="showMemoryDetail(\'' + m.id + '\')" style="cursor:pointer;">' +
        '<td class="cb-col" onclick="event.stopPropagation()"><input type="checkbox" class="mem-cb" value="' + m.id + '" onchange="updateBulkBar()"></td>' +
        '<td style="max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">' + (m.content || '').substring(0,120) + '</td>' +
        '<td>' + catBadge(m.category) + '</td>' +
        '<td>' + badgeForTier(m.tier) + '</td>' +
        '<td>' + (m.tags || []).slice(0,3).map(t => '<span class="tag-badge" style="font-size:9px;padding:1px 5px;">' + t + '</span>').join('') + '</td>' +
        '<td><div class="quality-bar"><div class="quality-fill" style="width:' + Math.max(4, ((m.quality||0)*100)).toFixed(0) + '%;background:' + (m.quality>=0.7?'#6ee7b7':m.quality>=0.4?'#fbbf24':'#fca5a5') + ';min-width:4px;"></div></div></td>' +
        '<td style="color:var(--muted);font-size:10px;">' + age(m.created_at) + '</td>' +
      '</tr>'
    ).join('');
    const total = data.total || 0;
    const pages = Math.ceil(total / MEM_LIMIT) || 1;
    document.getElementById('mem-pagination').innerHTML =
      '<button class="btn-neon btn-focus" onclick="memOffset=Math.max(0,memOffset-MEM_LIMIT);loadMemories()" ' + (memOffset<=0?'disabled':'') + '>&larr;</button>' +
      '<span class="page-info">' + (Math.floor(memOffset/MEM_LIMIT)+1) + '/' + pages + ' (' + total + ' total)</span>' +
      '<button class="btn-neon btn-focus" onclick="memOffset=' + Math.min(memOffset+MEM_LIMIT,((pages-1)*MEM_LIMIT)) + ';loadMemories()" ' + (memOffset>= ((pages-1)*MEM_LIMIT||0)?'disabled':'') + '>&rarr;</button>';
    document.getElementById('mem-sel-all').checked = false;
  } catch(e) { tbody.innerHTML = '<tr><td colspan="7" class="error-state">Error: ' + e.message + '</td></tr>'; }
}

async function deleteSingleMemory(memoryId) {
  if (!confirm("Delete this memory?")) return;
  try {
    const r = await fetch("/api/delete", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({memory_id: memoryId})
    });
    const d = await r.json();
    if (d.error) { alert("Error: " + d.error); return; }
    loadDuplicates();
  } catch(e) { alert("Delete failed: " + e.message); }
}
async function bulkDeleteMemories() {
  if (memSelected.size === 0) return;
  if (!confirm('Delete ' + memSelected.size + ' memories?')) return;
  try {
    await fetch(API + '/memories/bulk-delete', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({memory_ids: [...memSelected]}) });
    memSelected = new Set(); loadMemories(); updateTopStats();
  } catch(e) { alert('Error: '+e.message); }
}

function showMemoryDetail(id) {
  fetch(API + '/memory?id=' + encodeURIComponent(id)).then(r => r.json()).then(m => {
    if (m.error) { alert(m.error); return; }
    const html =
      '<div class="modal-overlay" onclick="if(event.target===this)this.remove()">' +
        '<div class="modal">' +
          '<div class="modal-title">' + catBadge(m.category) + ' <span style="color:var(--muted);font-size:11px;">' + (m.type || '') + '</span></div>' +
          '<div style="background:rgba(20,20,40,0.5);border:1px solid var(--border);border-radius:8px;padding:12px;font-family:\'Fira Code\',monospace;font-size:12px;line-height:1.5;white-space:pre-wrap;word-break:break-word;min-height:100px;max-height:400px;overflow-y:auto;">' + (m.content || '') + '</div>' +
          '<div style="margin-top:10px;color:var(--muted);font-size:11px;">Quality: ' + ((m.quality||0)*100).toFixed(0) + '% · Accessed: ' + (m.access_count||0) + 'x · Created: ' + new Date((m.created_at||0)*1000).toLocaleString() + '</div>' +
          '<div style="margin-top:8px;">' + (m.entities||[]).map(e => '<span class="tag-badge">' + e + '</span>').join('') + '</div>' +
          '<div style="margin-top:8px;">' + (m.tags||[]).map(t => '<span class="tag-badge">' + t + '</span>').join('') + '</div>' +
          '<div class="modal-actions"><button class="btn-neon btn-focus" onclick="this.closest(\'.modal-overlay\').remove()">Close</button></div>' +
        '</div>' +
      '</div>';
    document.body.insertAdjacentHTML('beforeend', html);
  }).catch(e => alert('Error: ' + e.message));
}

// ═══════════════════════════════════════════════
// Tags
// ═══════════════════════════════════════════════

async function loadTags() {
  const el = document.getElementById('tags-container');
  el.innerHTML = '<div class="loading"><div class="spinner"></div><div>Loading...</div></div>';
  try {
    const r = await fetch(API + '/tags');
    let tags = await r.json();
    if (tags && typeof tags.tags === 'object' && !Array.isArray(tags.tags)) tags = tags.tags;
    const entries = Object.entries(tags).sort((a,b) => b[1]-a[1]);
    const maxCount = Math.max(1, ...entries.map(([,v])=>v));
    el.innerHTML = entries.length
      ? entries.map(([tag, count]) =>
          '<div class="dup-group" style="display:flex;align-items:center;gap:10px;padding:8px 14px;">' +
            '<span class="tag-badge" style="font-size:12px;padding:4px 12px;min-width:80px;text-align:center;">' + tag + '</span>' +
            '<div style="flex:1;"><div class="quality-bar" style="width:100%;"><div class="quality-fill" style="width:' + (count/maxCount*100).toFixed(0) + '%;background:var(--accent)"></div></div></div>' +
            '<span style="color:var(--muted);font-size:11px;min-width:28px;text-align:right;">' + count + '</span>' +
            '<button class="btn-neon btn-delete" onclick="deleteTag(\'' + tag + '\')">Delete</button>' +
          '</div>'
        ).join('')
      : '<div class="empty-state">No tags</div>';
  } catch(e) { el.innerHTML = '<div class="error-state">Error: ' + e.message + '</div>'; }
}

async function deleteTag(tag) {
  if (!confirm('Delete tag "' + tag + '"?')) return;
  try {
    await fetch(API + '/tags/delete', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({tag}) });
    loadTags();
  } catch(e) { alert('Error: '+e.message); }
}

// ═══════════════════════════════════════════════
// Timeline
// ═══════════════════════════════════════════════

async function loadTimeline() {
  const el = document.getElementById('tl-container');
  const catFilter = document.getElementById('tl-category').value;
  el.innerHTML = '<div class="loading"><div class="spinner"></div><div>Loading...</div></div>';
  try {
    const r = await fetch(API + '/timeline');
    let data = await r.json();
    if (data && !Array.isArray(data) && Array.isArray(data.timeline)) data = data.timeline;
    if (!Array.isArray(data)) data = [];
    if (catFilter) data = data.map(g => ({ ...g, memories: (g.memories||[]).filter(m => m.category === catFilter), count: (g.memories||[]).filter(m => m.category === catFilter).length })).filter(g => g.count > 0);
    const total = data.reduce((s,g) => s + g.count, 0);
    setEl('tl-count', total + ' memories');
    if (!data.length) { el.innerHTML = '<div class="empty-state">No memories</div>'; return; }
    el.innerHTML = '<div class="timeline"><div class="timeline-line"></div>' +
      data.map(g =>
        '<div class="timeline-group"><div class="timeline-dot"></div><div class="timeline-date">' + g.date + ' <span style="font-weight:400;color:var(--muted);font-size:11px;">(' + g.count + ')</span></div>' +
        (g.memories||[]).slice(0,10).map(m =>
          '<div class="timeline-item" onclick="showMemoryDetail(\'' + m.id + '\')">' + catBadge(m.category) + ' ' + (m.content||'').substring(0,120) + ' <span style="color:var(--muted);font-size:10px;">' + age(m.created_at) + '</span></div>'
        ).join('') +
        (g.count > 10 ? '<div style="color:var(--muted);font-size:10px;padding:4px 12px;">+' + (g.count-10) + ' more</div>' : '') +
        '</div>'
      ).join('') +
    '</div>';
  } catch(e) { el.innerHTML = '<div class="error-state">Error: ' + e.message + '</div>'; }
}

// ═══════════════════════════════════════════════
// Duplicates
// ═══════════════════════════════════════════════

async function loadDuplicates() {
  const el = document.getElementById('dup-container');
  const threshold = document.getElementById('dup-threshold').value;
  el.innerHTML = '<div class="loading"><div class="spinner"></div><div>Scanning...</div></div>';
  try {
    const r = await fetch(API + '/duplicates?threshold=' + threshold);
    let groups = await r.json();
    if (groups && !Array.isArray(groups) && Array.isArray(groups.duplicates)) groups = groups.duplicates;
    if (!groups || !Array.isArray(groups) || !groups.length) { el.innerHTML = '<div class="empty-state">No duplicates</div>'; return; }
    el.innerHTML = groups.map((g, gi) =>
      '<div class="dup-group">' +
        '<div class="dup-group-header"><span style="font-size:13px;font-weight:600;color:var(--accent);">Group ' + (gi+1) + ' <span style="font-weight:400;color:var(--muted);font-size:11px;">(' + g.size + ')</span></span></div>' +
        (g.memories||[]).map((m, mi) =>
          '<div class="dup-item" onclick="showMemoryDetail(\'' + m.id + '\')">' +
            '<span style="color:' + (mi===0?'var(--accent)':'var(--muted)') + ';font-size:10px;min-width:20px;">#' + (mi+1) + '</span>' +
            catBadge(m.category) +
            '<span style="flex:1;">' + (m.content||'').substring(0,150) + '</span>' +
            '<span style="color:var(--muted);font-size:10px;">' + ((m.quality||0)*100).toFixed(0) + '%</span>' +
            '<button class="btn-neon btn-delete" style="padding:2px 8px;font-size:10px;margin-left:4px;" onclick="event.stopPropagation();deleteSingleMemory(\'' + m.id + '\')">✕</button>' +
          '</div>'
        ).join('') +
      '</div>'
    ).join('');
  } catch(e) { el.innerHTML = '<div class="error-state">Error: ' + e.message + '</div>'; }
}

// ═══════════════════════════════════════════════
// Embeddings (UMAP projection)
// ═══════════════════════════════════════════════

async function loadEmbedding() {
  const canvas = document.getElementById('emb-canvas');
  const ctx = canvas.getContext('2d');
  const container = document.getElementById('emb-container');
  const tooltip = document.getElementById('emb-tooltip');
  canvas.width = container.clientWidth || 800;
  canvas.height = 500;
  const nNeighbors = document.getElementById('emb-neighbors').value;
  const minDist = document.getElementById('emb-mindist').value;
  setEl('emb-count', 'Loading...');

  try {
    const r = await fetch(API + '/projection?n_neighbors=' + nNeighbors + '&min_dist=' + minDist);
    let points = await r.json();
    if (points && !Array.isArray(points) && Array.isArray(points.points)) points = points.points;
    if (!points || !Array.isArray(points) || !points.length) {
      ctx.fillStyle = '#64748b'; ctx.font = '12px Fira Sans'; ctx.textAlign = 'center';
      ctx.fillText('Not enough data (need 3+ memories)', canvas.width/2, canvas.height/2);
      setEl('emb-count', '0 points'); return;
    }
    setEl('emb-count', points.length + ' points');

    const xs = points.map(p => p.x), ys = points.map(p => p.y);
    const xMin = Math.min(...xs), xMax = Math.max(...xs), yMin = Math.min(...ys), yMax = Math.max(...ys);
    const xRange = xMax - xMin || 1, yRange = yMax - yMin || 1;
    const pad = 40, w = canvas.width - pad*2, h = canvas.height - pad*2;

    const categories = [...new Set(points.map(p => p.category))];
    document.getElementById('emb-legend').innerHTML = categories.map(c =>
      '<span style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--muted);"><span style="width:10px;height:10px;border-radius:50%;background:' + (colorHex[c] || colorHexDef) + '"></span>' + (colorLabel[c] || c) + '</span>'
    ).join('');

    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const pointData = points.map(p => ({
      x: pad + ((p.x - xMin) / xRange) * w, y: pad + ((p.y - yMin) / yRange) * h,
      r: 5, color: colorHex[p.category] || colorHexDef, id: p.id, content: p.content
    }));
    pointData.forEach(pt => {
      ctx.beginPath(); ctx.arc(pt.x, pt.y, pt.r, 0, Math.PI*2);
      ctx.fillStyle = pt.color; ctx.globalAlpha = 0.7; ctx.fill(); ctx.globalAlpha = 1;
      ctx.strokeStyle = 'rgba(255,255,255,0.2)'; ctx.lineWidth = 1; ctx.stroke();
    });

    container.onmousemove = function(e) {
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      let found = null;
      for (const pt of pointData) { if ((mx-pt.x)**2 + (my-pt.y)**2 < 100) { found = pt; break; } }
      if (found) {
        tooltip.style.display = 'block';
        tooltip.style.left = (e.clientX - rect.left + 12) + 'px';
        tooltip.style.top = (e.clientY - rect.top - 10) + 'px';
        tooltip.innerHTML = (found.content || '').substring(0,120);
      } else tooltip.style.display = 'none';
    };
    container.onclick = function(e) {
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left, my = e.clientY - rect.top;
      for (const pt of pointData) { if ((mx-pt.x)**2 + (my-pt.y)**2 < 100) { showMemoryDetail(pt.id); return; } }
    };
  } catch(e) {
    ctx.fillStyle = '#64748b'; ctx.font = '12px Fira Sans'; ctx.textAlign = 'center';
    ctx.fillText('Error: ' + e.message, canvas.width/2, canvas.height/2);
  }
}

// ═══════════════════════════════════════════════
// Clusters (named from common entities)
// ═══════════════════════════════════════════════

function clusterName(members) {
  // Find most common entity across members
  const wordCounts = {};
  (members || []).forEach(m => {
    const text = (m.content || '') + ' ' + (m.entities || []).join(' ');
    text.toLowerCase().split(/[\s,;:|]+/).filter(w => w.length > 2 && !['the','and','for','with','est','les','des','une','que','pas','sur','dans','par','plus','the'].includes(w)).forEach(w => {
      wordCounts[w] = (wordCounts[w] || 0) + 1;
    });
  });
  const sorted = Object.entries(wordCounts).sort((a,b) => b[1]-a[1]);
  if (sorted.length >= 2) return sorted[0][0] + ' · ' + sorted[1][0];
  if (sorted.length === 1) return sorted[0][0];
  return 'Cluster';
}

async function loadClusters() {
  const el = document.getElementById('cl-container');
  const threshold = document.getElementById('cl-threshold').value;
  const minSize = document.getElementById('cl-minsize').value;
  el.innerHTML = '<div class="loading"><div class="spinner"></div><div>Clustering...</div></div>';
  try {
    const r = await fetch(API + '/clusters?threshold=' + threshold + '&min_size=' + minSize);
    let clusters = await r.json();
    if (clusters && !Array.isArray(clusters) && Array.isArray(clusters.clusters)) clusters = clusters.clusters;
    if (!Array.isArray(clusters)) clusters = [];
    setEl('cl-count', clusters.length + ' clusters');
    if (!clusters.length) { el.innerHTML = '<div class="empty-state">No clusters</div>'; return; }
    const clColors = ['#3b82f6','#f59e0b','#22c55e','#a78bfa','#f43f5e','#6366f1','#14b8a6','#e879f9','#fb923c','#64748b'];
    el.innerHTML = clusters.map((c, i) => {
      const name = clusterName(c.members || []);
      return '<div class="dup-group">' +
        '<div class="dup-group-header"><span style="font-size:13px;font-weight:600;color:' + clColors[i%clColors.length] + '">' + name + ' <span style="font-weight:400;color:var(--muted);font-size:11px;">(' + c.size + ')</span></span></div>' +
        (c.members||[]).slice(0,15).map(m =>
          '<div class="dup-item" onclick="showMemoryDetail(\'' + m.id + '\')">' + catBadge(m.category) + ' ' + (m.content||'').substring(0,130) + '</div>'
        ).join('') +
        (c.size > 15 ? '<div style="color:var(--muted);font-size:10px;padding:4px 8px;">+' + (c.size-15) + ' more</div>' : '') +
      '</div>';
    }).join('');
  } catch(e) { el.innerHTML = '<div class="error-state">Error: ' + e.message + '</div>'; }
}

// ═══════════════════════════════════════════════
// Stale
// ═══════════════════════════════════════════════

async function loadStale() {
  const el = document.getElementById('stale-container');
  const days = document.getElementById('stale-days').value;
  const quality = document.getElementById('stale-quality').value;
  el.innerHTML = '<div class="loading"><div class="spinner"></div><div>Scanning...</div></div>';
  try {
    const r = await fetch(API + '/stale?days=' + days + '&quality_max=' + quality);
    let stale = await r.json();
    if (stale && !Array.isArray(stale) && Array.isArray(stale.memories)) stale = stale.memories;
    if (!Array.isArray(stale)) stale = [];
    if (!stale.length) { el.innerHTML = '<div class="empty-state">No stale memories</div>'; return; }
    el.innerHTML = stale.map(m =>
      '<div class="dup-item" onclick="showMemoryDetail(\'' + m.id + '\')" style="display:flex;align-items:center;gap:8px;">' +
        catBadge(m.category) +
        '<span style="flex:1;">' + (m.content||'').substring(0,140) + '</span>' +
        '<span style="color:var(--muted);font-size:10px;">' + age(m.created_at) + '</span>' +
        '<span style="font-size:10px;color:#fca5a5;">' + ((m.quality||0)*100).toFixed(0) + '%</span>' +
      '</div>'
    ).join('');
  } catch(e) { el.innerHTML = '<div class="error-state">Error: ' + e.message + '</div>'; }
}

async function deleteAllStale() {
  const days = document.getElementById('stale-days').value;
  const quality = document.getElementById('stale-quality').value;
  try {
    const r = await fetch(API + '/stale?days=' + days + '&quality_max=' + quality);
    let stale = await r.json();
    if (stale && !Array.isArray(stale) && Array.isArray(stale.memories)) stale = stale.memories;
    if (!Array.isArray(stale)) stale = [];
    if (!stale.length) { alert('Nothing to delete'); return; }
    if (!confirm('Delete ' + stale.length + ' stale memories?')) return;
    await fetch(API + '/memories/bulk-delete', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({memory_ids: stale.map(m => m.id)}) });
    loadStale(); updateTopStats();
  } catch(e) { alert('Error: ' + e.message); }
}

updateTopStats();
loadDashboard();
