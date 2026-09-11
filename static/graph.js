// ═══════════════════════════════════════════════
// LanceDB Memory Graph — Néon Edition
// ═══════════════════════════════════════════════

// State
let nodes = new vis.DataSet();
let edges = new vis.DataSet();
let network = null;
let allData = { nodes: [], edges: [], typed_edges: [] };
let selectedNodeId = null;
let highlightedTypedEdges = new Set(); // node IDs highlighted for typed relations
let graphIntersectionVisible = false;
const MAX_EDITOR_FACTS = 12;
const MAX_EDITOR_RELATIONS = 20;

// Cat colors — neon cyberpunk, dark cores with blazing borders
const catColors = {
  user_pref:  { bg: '#1a0520', border: '#ff2d8d', label: '#ff6db3', hex: '#ec4899', glow: 'rgba(255,45,141,1.0)' },
  project:    { bg: '#001a0f', border: '#00ff88', label: '#5cffb0', hex: '#10b981', glow: 'rgba(0,255,136,1.0)' },
  tech:       { bg: '#1a0e00', border: '#ff9d00', label: '#ffc04d', hex: '#f59e0b', glow: 'rgba(255,157,0,1.0)' },
  correction: { bg: '#1a0005', border: '#ff2244', label: '#ff6682', hex: '#ef4444', glow: 'rgba(255,34,68,1.0)' },
  fact:       { bg: '#001a1f', border: '#00ddff', label: '#66eeff', hex: '#06b6d4', glow: 'rgba(0,221,255,1.0)' },
  decision:   { bg: '#0f0020', border: '#aa44ff', label: '#cc88ff', hex: '#8b5cf6', glow: 'rgba(170,68,255,1.0)' },
  insight:    { bg: '#001a05', border: '#44ff66', label: '#88ff99', hex: '#22c55e', glow: 'rgba(68,255,102,1.0)' },
  reference:  { bg: '#000d1a', border: '#3399ff', label: '#77bbff', hex: '#3b82f6', glow: 'rgba(51,153,255,1.0)' },
  pattern:    { bg: '#1a0800', border: '#ff6611', label: '#ff9944', hex: '#f97316', glow: 'rgba(255,102,17,1.0)' },
  question:   { bg: '#12001a', border: '#dd33ff', label: '#ee77ff', hex: '#a855f7', glow: 'rgba(221,51,255,1.0)' },
};
const defaultColor = { bg: '#12122a', border: '#6677ff', label: '#99aaff', hex: '#6366f1', glow: 'rgba(102,119,255,0.8)' };
const catLabels = {
  user_pref: 'Pref', project: 'Project', tech: 'Tech',
  correction: 'Correction', fact: 'Fact', decision: 'Decision',
  insight: 'Insight', reference: 'Reference', pattern: 'Pattern', question: 'Question',
};
// Hex-only colors for canvas/scatter (used by app.js)
const colorHex = Object.fromEntries(Object.entries(catColors).map(([k,v]) => [k, v.hex]));
const colorLabel = catLabels;
const colorHexDef = defaultColor.hex;

// ═══════════════════════════════════════════════
// Load + Render
// ═══════════════════════════════════════════════

async function loadGraph() {
  document.getElementById('loading').style.display = 'block';
  const threshold = document.getElementById('threshold-slider')?.value || 0.8;
  const relationType = document.getElementById('relation-filter')?.value || '';
  if (!selectedNodeId) {
    allData = {nodes: [], edges: [], typed_edges: [], selection_required: true, hidden_neighbor_count: 0};
    renderGraph();
    const loading = document.getElementById('loading');
    loading.style.display = 'block';
    loading.innerHTML = '<div>Select a search result or memory to load a bounded neighborhood.</div>';
    return;
  }
  try {
    const resp = await fetch('/api/graph?memory_id=' + encodeURIComponent(selectedNodeId) + '&threshold=' + threshold + '&relation_types=' + encodeURIComponent(relationType));
    allData = await resp.json();
    renderGraph();
    if (!allData.error && allData.nodes.some(node => node.id === selectedNodeId)) openSidebar(selectedNodeId);
  } catch (e) {
    console.error('Failed to load graph:', e);
    document.getElementById('loading').innerHTML = 'Load error. Check server.';
  }
}

function renderGraph() {
  document.getElementById('loading').style.display = 'none';

  // Stats
  fetch('/api/stats').then(r => r.json()).then(stats => {
    if (stats.total_memories != null) setEl('mem-count', stats.total_memories);
    if (stats.total_entities != null) setEl('ent-count', stats.total_entities);
    if (stats.db_size_mb != null) setEl('db-size', stats.db_size_mb + ' MB');
  }).catch(() => {});

  setEl('mem-count', allData.nodes?.length || 0);
  setEl('ent-count', countEntities(allData.nodes));
  const totalEdges = (allData.edges?.length || 0);
  setEl('edge-count', totalEdges);
  setEl('hidden-neighbor-count', (allData.hidden_neighbor_count || 0) + ' hidden by budget · ' + (allData.hidden_by_relation_filter || 0) + ' hidden by relation filter');
  const fleg = document.getElementById('fresh-legend');
  if (fleg) fleg.style.display = 'flex';

  // Build vis.js nodes with neon glow
  const visNodes = (allData.nodes || []).map(n => {
    const isHub = n.node_type === 'hub';
    let c = isHub
      ? { bg: '#12122a', border: '#818cf8', label: '#a5b4fc', glow: 'rgba(99,102,241,0.5)' }
      : (catColors[n.category] || defaultColor);

    // Freshness — GLOW basé sur la couleur de la catégorie, plus c'est frais plus ça émet
    let ageBorder = c.border, ageGlow = c.glow, ageBW = 2, ageShadow = 10, ageLabel = '';
    if (!isHub && n.created_at) {
      const ageHours = (Date.now()/1000 - n.created_at) / 3600;
      if (ageHours < 1)        { ageBorder = '#ffffff'; ageGlow = c.glow;                    ageBW = 4; ageShadow = 30; ageLabel = ' ◉'; }
      else if (ageHours < 6)   { ageBorder = c.border;  ageGlow = c.glow.replace('1.0','0.9'); ageBW = 3; ageShadow = 22; }
      else if (ageHours < 24)  { ageBorder = c.border;  ageGlow = c.glow.replace('1.0','0.7'); ageBW = 3; ageShadow = 16; }
      else if (ageHours < 72)  { ageBorder = c.border;  ageGlow = c.glow.replace('1.0','0.5'); ageBW = 2; ageShadow = 11; }
      else if (ageHours < 168) { ageBorder = c.border;  ageGlow = c.glow.replace('1.0','0.35'); ageBW = 2; ageShadow = 8; }
      else if (ageHours < 720) { ageBorder = c.border;  ageGlow = c.glow.replace('1.0','0.2'); ageBW = 1; ageShadow = 5; }
      else                     { ageBorder = c.border;  ageGlow = c.glow.replace('1.0','0.08'); ageBW = 1; ageShadow = 3; }
    }

    const sz = isHub ? 32 : Math.max(22, (n.size || 20) + Math.min((n.access_count || 0) * 0.5, 8));
    const displayLabel = (n.label || '(empty)') + ageLabel;

    return {
      id: n.id, label: displayLabel, title: n.title || '',
      color: {
        background: c.bg,
        border: ageBorder,
        highlight: { background: c.bg, border: '#ffffff' },
        hover: { background: c.bg, border: '#ffffff' },
      },
      font: { color: c.label, size: isHub ? 13 : 11, face: 'Fira Code, monospace', strokeWidth: 0 },
      size: sz, shape: isHub ? 'box' : 'dot',
      borderWidth: ageBW, borderWidthSelected: 3,
      shadow: { enabled: true, color: ageGlow, size: ageShadow, x: 0, y: 0 },
      opacity: 1.0,
      _category: n.category, _tier: n.tier || 'none',
      _entities: (n.entities || []).join(' ').toLowerCase(),
      _content: (n.label || '').toLowerCase(), _created: n.created_at || 0,
    };
  });

  const visEdges = (allData.edges || []).map(e => {
    if (e.kind === 'declared') return {
      from: e.from, to: e.to, label: e.label || '', kind: 'declared',
      color: {color:'#f59e0b', highlight:'#fbbf24', hover:'#fbbf24'}, width:2.2,
      dashes:false, font:{color:'#fbbf24', size:9, strokeWidth:0},
      arrows:{to:{enabled:true, scaleFactor:0.55}}, smooth:{type:'curvedCW', roundness:0.12},
    };
    return {
      from: e.from, to: e.to, label: e.label || '', kind: 'semantic',
      color: {color:'#3a3a6a', highlight:'#818cf8', hover:'#a5b4fc'}, width:1.3,
      dashes:[4,5], font:{color:'#64748b', size:9, strokeWidth:0},
      arrows:{to:{enabled:false}}, smooth:{type:'curvedCW', roundness:0.15},
    };
  });

  if (network) {
    // Update in-place
    const oldIds = new Set(nodes.getIds());
    const newIds = new Set(visNodes.map(n => n.id));
    for (const id of oldIds) { if (!newIds.has(id)) nodes.remove(id); }
    for (const n of visNodes) { oldIds.has(n.id) ? nodes.update(n) : nodes.add(n); }
    edges.clear(); edges.add(visEdges);
  } else {
    nodes.clear(); edges.clear();
    nodes.add(visNodes); edges.add(visEdges);

    network = new vis.Network(document.getElementById('graph-canvas'), { nodes, edges }, {
      nodes: { scaling: { min: 10, max: 45 } },
      physics: {
        enabled: true,
        solver: 'forceAtlas2Based',
        forceAtlas2Based: {
          gravitationalConstant: -40,
          centralGravity: 0.005,
          springLength: 160,
          springConstant: 0.004,
          damping: 0.98,
        },
        stabilization: { iterations: 150, fit: true },
      },
      interaction: { hover: true, zoomView: true, dragView: true },
      layout: { improvedLayout: true },
    });

    network.on('click', function(params) {
      if (params.nodes.length) {
        loadNeighborhood(params.nodes[0]);
      } else {
        closeSidebar();
      }
    });
  }

  applyFilters();
  setGraphPhysicsActive(graphIntersectionVisible && !document.hidden);
}

async function loadNeighborhood(nodeId) {
  selectedNodeId = nodeId;
  closeSearchPanel();
  await loadGraph();
}

function setGraphPhysicsActive(active) {
  if (!network) return;
  network.setOptions({physics: {enabled: Boolean(active)}});
  if (active) network.startSimulation();
  else network.stopSimulation();
}

function pauseGraphPhysics() { setGraphPhysicsActive(false); }
function resumeGraphPhysics() { setGraphPhysicsActive(graphIntersectionVisible && !document.hidden); }

const graphObserver = new IntersectionObserver(entries => {
  graphIntersectionVisible = entries.some(entry => entry.isIntersecting);
  setGraphPhysicsActive(graphIntersectionVisible && !document.hidden);
}, {threshold: 0.01});
graphObserver.observe(document.getElementById('graph-container'));
document.addEventListener('visibilitychange', () => setGraphPhysicsActive(graphIntersectionVisible && !document.hidden));

// ═══════════════════════════════════════════════
// Typed edge highlighting (shown only on selection)
// ═══════════════════════════════════════════════

function highlightTypedRelations(nodeId) {
  // Reset previous highlights
  resetTypedHighlights();

  // Find typed edges involving nodeId
  const relatedIds = new Set();
  (allData.typed_edges || []).forEach(e => {
    if (e.from === nodeId) relatedIds.add(e.to);
    if (e.to === nodeId) relatedIds.add(e.from);
  });

  highlightedTypedEdges = relatedIds;

  // Apply violet glow to connected nodes
  relatedIds.forEach(id => {
    const node = allData.nodes.find(n => n.id === id);
    if (!node) return;
    nodes.update({
      id: id,
      borderWidth: 3,
      borderWidthSelected: 4,
      shadow: { enabled: true, color: 'rgba(167,139,250,0.7)', size: 18, x: 0, y: 0 },
      color: {
        background: node.node_type === 'hub' ? '#12122a' : (catColors[node.category] || defaultColor).bg,
        border: '#a78bfa',
        highlight: { border: '#c4b5fd' },
        hover: { border: '#ddd6fe' },
      },
    });
  });
}

function resetTypedHighlights() {
  highlightedTypedEdges.forEach(id => {
    const n = allData.nodes.find(x => x.id === id);
    if (!n) return;
    const isHub = n.node_type === 'hub';
    let c = isHub
      ? { bg: '#12122a', border: '#818cf8', glow: 'rgba(99,102,241,0.5)' }
      : (catColors[n.category] || defaultColor);

    // Recompute age border
    let ageBorder = c.border, ageGlow = c.glow, ageBW = 2, ageShadow = 10;
    if (!isHub && n.created_at) {
      const ageHours = (Date.now()/1000 - n.created_at) / 3600;
      if (ageHours < 1)        { ageBorder = '#ffffff'; ageGlow = 'rgba(255,255,255,0.9)'; ageBW = 3; ageShadow = 30; }
      else if (ageHours < 24)  { ageBorder = '#e0e7ff'; ageGlow = 'rgba(199,210,254,0.7)'; ageShadow = 16; }
      else if (ageHours < 168) { ageBorder = '#a5b4fc'; ageGlow = 'rgba(165,180,252,0.5)'; ageShadow = 8; }
      else if (ageHours < 720) { ageBorder = '#6366f1'; ageGlow = 'rgba(99,102,241,0.35)'; ageShadow = 5; }
      else                     { ageBorder = '#4338ca'; ageGlow = 'rgba(67,56,202,0.2)'; ageShadow = 3; }
    }
    nodes.update({
      id: id,
      borderWidth: ageBW,
      borderWidthSelected: 3,
      shadow: { enabled: true, color: ageGlow, size: ageShadow, x: 0, y: 0 },
      color: {
        background: c.bg,
        border: ageBorder,
        highlight: { border: '#ffffff' },
        hover: { border: '#ffffff' },
      },
    });
  });
  highlightedTypedEdges.clear();
}

// ═══════════════════════════════════════════════
// Sidebar (inspiré memviz — détail enrichi)
// ═══════════════════════════════════════════════

function openSidebar(nodeId) {
  selectedNodeId = nodeId;
  const sidebar = document.getElementById('sidebar');
  const content = document.getElementById('sidebar-content');
  sidebar.classList.add('open');

  const n = allData.nodes.find(x => x.id === nodeId);
  if (!n) return;

  // Highlight typed relations on graph
  highlightTypedRelations(nodeId);

  if (n.node_type === 'hub') { renderHubSidebar(content, nodeId, n); return; }

  const cat = catLabels[n.category] || n.category || 'Fact';
  const dateStr = n.created_at ? new Date(+n.created_at * 1000).toLocaleString('en-US') : '--';

  // Fetch full detail from API
  fetch('/api/memory?id=' + nodeId)
    .then(r => r.json())
    .then(data => {
      if (data.error) { content.innerHTML = '<div class="detail-meta" style="color:red;">' + escapeHtml(data.error) + '</div>'; return; }
      const fullContent = data.content || n.title || n.label || '';
      const category = data.category || n.category || 'fact';
      const catLabel = catLabels[category] || category || 'Fact';
      const fullEntities = data.entities || n.entities || [];
      const linkedMemories = data.linked_memories || [];
      const quality = typeof data.quality === 'number' ? data.quality : null;
      const accessCount = data.access_count || 0;

      // Read current threshold slider value
      const currentThreshold = parseFloat(document.getElementById('threshold-slider')?.value || 0.8);

      // Build typed edges for this node
      const nodeTypedEdges = (allData.typed_edges || []).filter(e => e.from === nodeId || e.to === nodeId);
      // Build vector edges, filtered by threshold
      const nodeVecEdges = (allData.edges || []).filter(e => {
        if (e.from !== nodeId && e.to !== nodeId) return false;
        const score = parseFloat(e.label) || 0;
        return score >= currentThreshold;
      });

      const typedEdgesCount = nodeTypedEdges.length;
      const nEdges = nodeVecEdges.length;

      renderSidebarContent(content, nodeId, fullContent, category, catLabel, dateStr, fullEntities, linkedMemories, nodeTypedEdges, nodeVecEdges, quality, accessCount, typedEdgesCount, nEdges, data.structured || null, data.updated_at);
    })
    .catch(() => {
      renderSidebarContent(content, nodeId, n.title || n.label || '', n.category || 'fact', cat, dateStr, n.entities || [], [], [], []);
    });
}

function renderSidebarContent(content, nodeId, fullContent, category, catLabel, dateStr, entities, linkedMemories, typedEdges, vectorEdges, quality, accessCount, typedEdgesCount, nEdges, structured, updatedAt) {
  // ─── Stats bar ───
  const n = allData.nodes.find(x => x.id === nodeId);
  const ageHours = n && n.created_at ? (Date.now()/1000 - n.created_at) / 3600 : null;
  let freshnessLabel = 'Old', freshnessColor = '#555';
  if (ageHours !== null) {
    if (ageHours < 1)       { freshnessLabel = '< 1h'; freshnessColor = '#ffffff'; }
    else if (ageHours < 6)  { freshnessLabel = Math.floor(ageHours) + 'h'; freshnessColor = '#c7d2fe'; }
    else if (ageHours < 24) { freshnessLabel = Math.floor(ageHours) + 'h'; freshnessColor = '#a5b4fc'; }
    else if (ageHours < 72) { freshnessLabel = Math.floor(ageHours/24) + 'd'; freshnessColor = '#818cf8'; }
    else                    { freshnessLabel = Math.floor(ageHours/24) + 'd'; freshnessColor = '#6366f1'; }
  }

  const q = quality !== null ? quality : 0;
  const qColor = q >= 0.8 ? '#4ade80' : q >= 0.5 ? '#fbbf24' : '#f87171';
  const relCount = typedEdgesCount + nEdges;

  let statsHtml = '<div class="detail-stats">' +
    '<div class="stat-item"><span class="stat-dot" style="background:' + freshnessColor + ';box-shadow:0 0 8px ' + freshnessColor + ';"></span><span class="stat-val">' + freshnessLabel + '</span><span class="stat-lbl">Freshness</span></div>' +
    '<div class="stat-item"><span class="stat-val" style="color:' + qColor + '">' + Math.round(q * 100) + '%</span><span class="stat-lbl">Quality</span></div>' +
    '<div class="stat-item"><span class="stat-val">' + accessCount + '</span><span class="stat-lbl">Views</span></div>' +
    '<div class="stat-item"><span class="stat-val">' + relCount + '</span><span class="stat-lbl">Links</span></div>' +
    '</div>';

  // Entities
  let entityChips = '';
  if (entities.length) {
    entityChips = '<div class="detail-meta" style="margin-top:8px;">Entities (' + entities.length + ')</div><div class="detail-ents">' +
      entities.map(e => '<span onclick="searchEntity(\'' + escapeJsString(e) + '\')">' + escapeHtml(e) + '</span>').join('') +
      '</div>';
  }

  // ─── Relations: fusionner typedEdges + linkedMemories ───
  // Map typed edges by target ID → relation type
  const typedMap = {};
  typedEdges.forEach(e => {
    const otherId = e.from === nodeId ? e.to : e.from;
    typedMap[otherId] = { type: e.relation_type || e.type || 'linked', dir: e.from === nodeId ? '→' : '←' };
  });

  // Merge linked memories with their type info
  let relatedHtml = '';
  const seenRelated = new Set(); // avoid duplicates
  const relatedItems = [];

  // First: typed edges (have explicit type)
  typedEdges.forEach(e => {
    const otherId = e.from === nodeId ? e.to : e.from;
    if (seenRelated.has(otherId)) return;
    seenRelated.add(otherId);
    const otherNode = allData.nodes.find(x => x.id === otherId);
    const otherLabel = otherNode ? (otherNode.label || otherNode.title || otherId).substring(0, 50) : otherId;
    const relType = e.relation_type || e.type || 'linked';
    const dir = e.from === nodeId ? '→' : '←';
    relatedItems.push({
      id: otherId, label: otherLabel, type: relType, dir: dir,
      cat: otherNode?.category || 'fact', simScore: null,
    });
  });

  // Then: linked memories NOT already in typed
  (linkedMemories || []).forEach(lm => {
    if (seenRelated.has(lm.id)) return;
    seenRelated.add(lm.id);
    // Find common entities
    const nodeEntities = new Set(entities.map(e => e.toLowerCase()));
    const lmEntities = (lm.entities || []).map(e => e.toLowerCase());
    const common = lmEntities.filter(e => nodeEntities.has(e));
    let relType, dir;
    if (common.length > 0) {
      relType = 'shared';
      dir = common.slice(0, 2).join(', ');
    } else {
      relType = 'linked';
      dir = '';
    }
    relatedItems.push({
      id: lm.id, label: (lm.content || '').substring(0, 80),
      type: relType, dir: dir,
      cat: lm.category || 'fact', simScore: null,
    });
  });

  if (relatedItems.length) {
    relatedHtml = '<div class="detail-links-list"><div class="detail-meta" style="color:#a78bfa;">Related (' + relatedItems.length + ')</div>';
    relatedItems.forEach(item => {
      const typeClass = item.type === 'shared' ? 'typed-tag-shared' : 'typed-tag';
      relatedHtml += '<div class="detail-link-item typed-link" onclick="openSidebar(\'' + escapeJsString(item.id) + '\')">' +
        '<span class="cat-badge cat-' + safeCategory(item.cat) + '" style="font-size:9px;padding:1px 6px;margin-bottom:0">' + escapeHtml(catLabels[safeCategory(item.cat)] || '?') + '</span>' +
        '<span class="' + typeClass + '">' + escapeHtml(item.type) + '</span>' +
        (item.dir ? ' <span style="color:#a78bfa;font-size:10px;">' + escapeHtml(item.dir) + '</span>' : '') +
        ' ' + escapeHtml(item.label) +
        '</div>';
    });
    relatedHtml += '</div>';
  }

  // ─── Similar (vector cosine) — filtered by threshold ───
  let vecHtml = '';
  if (vectorEdges.length) {
    vecHtml = '<div class="detail-links-list"><div class="detail-meta" style="color:#818cf8;">Similar by embedding (' + vectorEdges.length + ')</div>';
    vectorEdges.sort((a,b) => {
      const sa = parseFloat(a.label) || 0;
      const sb = parseFloat(b.label) || 0;
      return sb - sa;
    });
    vectorEdges.slice(0, 10).forEach(e => {
      const otherId = e.from === nodeId ? e.to : e.from;
      const otherNode = allData.nodes.find(x => x.id === otherId);
      const otherLabel = otherNode ? (otherNode.label || otherNode.title || otherId).substring(0, 50) : otherId;
      const simScore = parseFloat(e.label);
      const simColor = simScore > 0.9 ? '#6ee7b7' : simScore > 0.8 ? '#a5b4fc' : '#64748b';
      vecHtml += '<div class="detail-link-item" onclick="openSidebar(\'' + escapeJsString(otherId) + '\')">' +
        '<span class="cat-badge cat-' + safeCategory(otherNode?.category) + '" style="font-size:9px;padding:1px 6px;margin-bottom:0">' + escapeHtml(catLabels[safeCategory(otherNode?.category)] || '?') + '</span>' +
        escapeHtml(otherLabel) +
        '<span style="float:right;color:' + simColor + ';font-size:10px;font-weight:600;">' + (simScore*100).toFixed(0) + '%</span>' +
        '</div>';
    });
    vecHtml += '</div>';
  }

  content.innerHTML =
    '<div class="cat-badge cat-' + safeCategory(category) + '">' + escapeHtml(catLabel) + '</div>' +
    statsHtml +
    '<div class="detail-content" id="detail-text">' + escapeHtml(fullContent) + '</div>' +
    '<div class="detail-meta" style="margin-top:4px;">' + escapeHtml(dateStr) + '</div>' +
    entityChips +
    vecHtml +
    relatedHtml +
    '<div class="detail-actions">' +
      '<button class="btn-focus" onclick="focusNode(\'' + escapeJsString(nodeId) + '\')">Center</button>' +
      '<button class="btn-focus" onclick="toggleEdit()">Edit</button>' +
      '<button class="btn-focus btn-copy" onclick="copyMemoryJSON(\'' + escapeJsString(nodeId) + '\')">Copy JSON</button>' +
      '<button class="btn-delete" onclick="deleteNode(\'' + escapeJsString(nodeId) + '\')">Delete</button>' +
    '</div>';

  content.dataset.nodeId = nodeId;
  content.dataset.fullContent = fullContent;
  content.dataset.category = category;
  content.dataset.entities = JSON.stringify(entities);
  content.dataset.structured = JSON.stringify(structured || {});
  content.dataset.updatedAt = String(updatedAt || 0);
}

function renderHubSidebar(content, hubId, hubNode) {
  const children = [];
  [...(allData.edges || [])].forEach(e => {
    if (e.from === hubId) children.push(e.to);
    if (e.to === hubId) children.push(e.from);
  });
  const seen = new Set();
  const childNodes = [];
  children.forEach(cid => {
    if (seen.has(cid)) return;
    seen.add(cid);
    const cn = allData.nodes.find(x => x.id === cid);
    if (cn && cn.node_type !== 'hub') childNodes.push(cn);
  });
  childNodes.sort((a,b) => ((a.category || '').localeCompare(b.category || '') || (a.label || '').localeCompare(b.label || '')));

  let itemsHtml = childNodes.length === 0
    ? '<div class="detail-meta" style="color:#475569;">No linked nodes.</div>'
    : childNodes.map(cn => {
        const lcat = catLabels[cn.category] || cn.category || 'Fact';
        const disp = (cn.title || cn.label || '')[0] === '?' ? (cn.title || cn.label || '').substring(1).trim() : (cn.title || cn.label || '');
        return '<div class="detail-link-item" onclick="openSidebar(\'' + escapeJsString(cn.id) + '\')">' +
          '<span class="cat-badge cat-' + safeCategory(cn.category) + '" style="font-size:10px;padding:1px 6px;margin-bottom:0">' + escapeHtml(lcat) + '</span>' +
          escapeHtml(disp) + '</div>';
      }).join('');

  content.innerHTML =
    '<div class="cat-badge cat-' + safeCategory(hubNode.category) + '">Hub</div>' +
    '<div style="font-family:\'Fira Code\',monospace;font-size:13px;color:#a5b4fc;margin-bottom:6px;">' + escapeHtml(hubNode.label || '') + '</div>' +
    '<div class="detail-meta" style="margin-bottom:12px;">' + escapeHtml(hubNode.title || hubNode.label || '') + '</div>' +
    '<div class="detail-meta">' + childNodes.length + ' linked memories:</div>' +
    '<div class="detail-links-list" style="margin-top:8px;">' + itemsHtml + '</div>' +
    '<div class="detail-actions" style="margin-top:14px;"><button class="btn-focus" onclick="focusNode(\'' + escapeJsString(hubId) + '\')">Center</button></div>';
}

// ═══════════════════════════════════════════════
// Edit / Delete
// ═══════════════════════════════════════════════

function toggleEdit() {
  const content = document.getElementById('sidebar-content');
  const nodeId = content.dataset.nodeId;
  let current = {};
  try { current = JSON.parse(content.dataset.structured || '{}'); } catch(e) {}
  if (!current.domain) {
    content.innerHTML = '<div class="version-conflict">This legacy row cannot be edited structurally until its format is reviewed.</div>' +
      '<div class="detail-actions"><button class="btn-focus" onclick="openSidebar(\'' + escapeJsString(nodeId) + '\')">Back</button></div>';
    return;
  }
  const relations = JSON.stringify(current.relations || [], null, 2);
  const categories = ['fact','user_pref','project','tech','correction','decision','insight','reference','pattern','question'];

  content.innerHTML =
    '<div class="detail-meta editor-heading">Structured memory · preview required</div>' +
    '<div class="structured-editor" oninput="invalidateEditPreview()">' +
      '<label>Domain<input id="edit-domain" value="' + escapeHtmlAttr(current.domain) + '"></label>' +
      '<label>Subject<input id="edit-subject" value="' + escapeHtmlAttr(current.subject) + '"></label>' +
      '<label>Facts · one per line (max ' + MAX_EDITOR_FACTS + ')<textarea id="edit-facts">' + escapeHtml((current.facts || []).join('\n')) + '</textarea></label>' +
      '<div class="editor-row"><label>Tier<select id="edit-tier">' + [1,2,3].map(t => '<option value="' + t + '" ' + (current.tier === t ? 'selected' : '') + '>' + t + '</option>').join('') + '</select></label>' +
      '<label>Category<select id="edit-category">' + categories.map(c => '<option value="' + c + '" ' + (current.category === c ? 'selected' : '') + '>' + escapeHtml(catLabels[c] || c) + '</option>').join('') + '</select></label></div>' +
      '<label>Relations JSON (max ' + MAX_EDITOR_RELATIONS + ')<textarea id="edit-relations">' + escapeHtml(relations) + '</textarea></label>' +
    '</div>' +
    '<div id="version-conflict" class="version-conflict" hidden></div>' +
    '<div id="edit-diff" class="edit-diff" hidden><div><span>Old</span><pre id="previous-preview"></pre></div><div><span>Canonical new</span><pre id="canonical-preview"></pre></div></div>' +
    '<div class="detail-actions" style="margin-top:12px;">' +
      '<button class="btn-focus" onclick="previewEdit(\'' + escapeJsString(nodeId) + '\')">Preview</button>' +
      '<button id="save-edit" class="btn-save" onclick="saveEdit(\'' + escapeJsString(nodeId) + '\')" disabled>Save canonical</button>' +
      '<button class="btn-focus" onclick="openSidebar(\'' + escapeJsString(nodeId) + '\')">Cancel</button>' +
    '</div>';
  content.dataset.previewReady = '0';
}

function invalidateEditPreview() {
  const content = document.getElementById('sidebar-content');
  content.dataset.previewReady = '0';
  const save = document.getElementById('save-edit');
  if (save) save.disabled = true;
}

function editorPayload() {
  const facts = document.getElementById('edit-facts').value.split('\n').map(v => v.trim()).filter(Boolean);
  const relations = JSON.parse(document.getElementById('edit-relations').value || '[]');
  if (facts.length > MAX_EDITOR_FACTS) throw new Error('At most ' + MAX_EDITOR_FACTS + ' facts are allowed.');
  if (!Array.isArray(relations) || relations.length > MAX_EDITOR_RELATIONS) throw new Error('Relations must be an array of at most ' + MAX_EDITOR_RELATIONS + ' items.');
  return {
    domain: document.getElementById('edit-domain').value,
    subject: document.getElementById('edit-subject').value,
    facts: facts,
    tier: Number(document.getElementById('edit-tier').value),
    category: document.getElementById('edit-category').value,
    relations: relations,
    base_updated_at: Number(document.getElementById('sidebar-content').dataset.updatedAt || 0),
  };
}

function showEditError(data) {
  const error = document.getElementById('version-conflict');
  error.hidden = false;
  error.textContent = data.code === 'version_conflict'
    ? 'Version conflict: the memory changed. Reopen it before editing.'
    : (data.error || 'Invalid structured memory.');
}

async function previewEdit(nodeId) {
  try {
    const response = await fetch('/api/memories/' + nodeId + '/preview', {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(editorPayload())});
    const data = await response.json();
    if (data.error) { showEditError(data); return; }
    document.getElementById('version-conflict').hidden = true;
    document.getElementById('previous-preview').textContent = data.previous_content || '';
    document.getElementById('canonical-preview').textContent = data.canonical_content || '';
    document.getElementById('edit-diff').hidden = false;
    document.getElementById('sidebar-content').dataset.previewReady = '1';
    document.getElementById('save-edit').disabled = false;
  } catch(e) { showEditError({error: e.message}); }
}

async function saveEdit(nodeId) {
  if (document.getElementById('sidebar-content').dataset.previewReady !== '1') return;
  try {
    const response = await fetch('/api/memories/' + nodeId, {method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(editorPayload())});
    const data = await response.json();
    if (data.error) { showEditError(data); invalidateEditPreview(); return; }
    loadGraph(); openSidebar(nodeId);
  } catch(e) { showEditError({error: e.message}); }
}

function closeSidebar() {
  resetTypedHighlights();
  document.getElementById('sidebar').classList.remove('open');
  if (network) network.unselectAll();
}

function focusNode(nodeId) {
  if (network) network.focus(nodeId, { scale: 1.5, animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
}

function searchEntity(entity) {
  document.getElementById('search').value = entity;
  document.getElementById('cat-filter').value = '';
  applyFilters();
}

async function deleteNode(nodeId) {
  if (!confirm('Supprimer cette mémoire ?')) return;
  try {
    const resp = await fetch('/api/delete', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({memory_id: nodeId}) });
    if ((await resp.json()).error) return;
    closeSidebar(); selectedNodeId = null; loadGraph();
  } catch(e) { console.error('Delete error:', e); }
}

// ═══════════════════════════════════════════════
// Filters — with tier ghost mode (opacity 30%)
// ═══════════════════════════════════════════════

function applyFilters() {
  if (!network) return;
  const searchText = document.getElementById('search').value.toLowerCase().trim();
  const catFilter = document.getElementById('cat-filter').value;
  const t1 = document.getElementById('tier1')?.checked ?? true;
  const t2 = document.getElementById('tier2')?.checked ?? true;
  const t3 = document.getElementById('tier3')?.checked ?? true;

  // Nodes that match tier filter
  const tierMatch = new Set();
  (allData.nodes || []).forEach(n => {
    const tier = n.tier || 'none';
    if ((t1 && tier === '1') || (t2 && tier === '2') || (t3 && tier === '3') || tier === 'none') tierMatch.add(n.id);
  });

  // Primary visible: match tier + search + category
  const primaryIds = new Set();
  (allData.nodes || []).forEach(n => {
    if (!tierMatch.has(n.id)) return;
    const content = ((n.label || '') + ' ' + (n.title || '')).toLowerCase();
    const entities = (n.entities || []).join(' ').toLowerCase();
    const matchesSearch = !searchText || content.includes(searchText) || entities.includes(searchText);
    const matchesCat = !catFilter || n.category === catFilter;
    if (matchesSearch && matchesCat) primaryIds.add(n.id);
  });

  // Ghost: nodes connected to primary but NOT matching tier filter
  const ghostIds = new Set();
  (allData.edges || []).forEach(e => {
    if (primaryIds.has(e.from) && !primaryIds.has(e.to) && !tierMatch.has(e.to)) ghostIds.add(e.to);
    if (primaryIds.has(e.to) && !primaryIds.has(e.from) && !tierMatch.has(e.from)) ghostIds.add(e.from);
  });

  const allVisible = new Set([...primaryIds, ...ghostIds]);

  nodes.forEach(node => {
    if (primaryIds.has(node.id)) {
      nodes.update({ id: node.id, hidden: false, opacity: 1.0 });
    } else if (ghostIds.has(node.id)) {
      nodes.update({ id: node.id, hidden: false, opacity: 0.3 });
    } else {
      nodes.update({ id: node.id, hidden: true });
    }
  });
}

function countEntities(nodesArr) {
  if (!nodesArr) return '—';
  const ents = new Set();
  nodesArr.forEach(n => (n.entities || []).forEach(e => ents.add(e)));
  return ents.size;
}

// ═══════════════════════════════════════════════
// Semantic search
// ═══════════════════════════════════════════════

async function doSemanticSearch() {
  const query = document.getElementById('search').value.trim();
  if (!query) return;
  const panel = document.getElementById('search-panel');
  panel.classList.add('visible');
  panel.innerHTML = '<div class="search-empty">Searching...</div>';
  try {
    const resp = await fetch('/api/search?q=' + encodeURIComponent(query) + '&top_k=15');
    const data = await resp.json();
    if (data.error) { panel.innerHTML = '<div class="search-empty">Error: ' + escapeHtml(data.error) + '</div>'; return; }
    if (!data.results.length) { panel.innerHTML = '<div class="search-empty">No results.</div>'; return; }
    panel.innerHTML = data.results.map(r => {
      const cat = catLabels[r.category] || r.category || 'Fact';
      return '<div class="search-item" onclick="loadNeighborhood(\'' + escapeJsString(r.id) + '\')">' +
        '<span class="s-cat cat-' + safeCategory(r.category) + '">' + escapeHtml(cat) + '</span>' +
        '<span class="s-meta">score: ' + (r._distance ? r._distance.toFixed(2) : '0.00') + '</span>' +
        '<div class="s-content">' + escapeHtml((r.content || '').substring(0, 140)) + '</div></div>';
    }).join('');
  } catch(e) { panel.innerHTML = '<div class="search-empty">Search failed.</div>'; }
}

function closeSearchPanel() {
  document.getElementById('search-panel').classList.remove('visible');
}

// ═══════════════════════════════════════════════
// Export / Import / Copy
// ═══════════════════════════════════════════════

function exportMemories() {
  fetch('/api/export').then(r => r.json()).then(data => {
    if (data.error) { alert('Export failed: ' + data.error); return; }
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'lancedb-memories.json'; a.click();
    URL.revokeObjectURL(a.href);
  }).catch(e => alert('Export failed: ' + e));
}

function importMemories() { document.getElementById('import-file').click(); }

async function handleImportFile(input) {
  if (!input.files.length) return;
  try {
    const data = JSON.parse(await input.files[0].text());
    if (!data.memories) { alert('Invalid format: expected {"memories": [...]}'); return; }
    const resp = await fetch('/api/import', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) });
    const result = await resp.json();
    if (result.error) alert('Import error: ' + result.error);
    else { alert('Imported: ' + result.imported + ', skipped: ' + result.skipped); loadGraph(); }
  } catch(e) { alert('Import failed: ' + e.message); }
  input.value = '';
}

async function copyMemoryJSON(nodeId) {
  const n = allData.nodes.find(x => x.id === nodeId);
  if (!n) return;
  try {
    const data = await (await fetch('/api/memory?id=' + nodeId)).json();
    await navigator.clipboard.writeText(JSON.stringify({ id: data.id || nodeId, content: data.content || n.title || '', category: data.category || n.category || 'fact', entities: data.entities || [] }, null, 2));
    const btn = document.querySelector('.btn-copy');
    if (btn) { const orig = btn.textContent; btn.textContent = 'Copied!'; setTimeout(() => { btn.textContent = orig; }, 1200); }
  } catch(e) { alert('Copy failed: ' + e.message); }
}

// ═══════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════

document.getElementById('search').addEventListener('input', applyFilters);
document.getElementById('search').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') { e.preventDefault(); closeSearchPanel(); doSemanticSearch(); }
});
document.addEventListener('click', function(e) {
  const panel = document.getElementById('search-panel');
  if (panel.classList.contains('visible') && e.target !== document.getElementById('search') && !panel.contains(e.target)) closeSearchPanel();
});
document.getElementById('cat-filter').addEventListener('change', applyFilters);
document.getElementById('relation-filter').addEventListener('change', () => { closeSidebar(); loadGraph(); });
document.getElementById('refresh-btn').addEventListener('click', () => { closeSearchPanel(); closeSidebar(); loadGraph(); });
document.getElementById('sidebar-close').addEventListener('click', closeSidebar);

// Helpers
function escapeHtml(str) { const d = document.createElement('div'); d.textContent = str == null ? '' : String(str); return d.innerHTML; }
function escapeHtmlAttr(str) { return String(str == null ? '' : str).replace(/&/g,'&amp;').replace(/'/g,'&#39;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function escapeJsString(str) { const value = String(str == null ? '' : str); let out = ''; for (let i = 0; i < value.length; i++) out += '\\u' + value.charCodeAt(i).toString(16).padStart(4, '0'); return out; }
function safeCategory(value) { return Object.prototype.hasOwnProperty.call(catLabels, value) ? value : 'fact'; }
function setEl(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }
