// Logo 擦除工具 — Canvas + 模板库 + 批处理
const state = {
  tool: 'box',
  image: null,
  imageId: null,
  imgSize: { w: 0, h: 0 },
  scale: 1,
  offset: { x: 0, y: 0 },
  drag: null,
  box: null,
  color: null,            // null = 留给后端自动采样
  pickRadius: 5,
  templates: [],          // 服务端列表
  selectedTpls: new Set(),
  batchId: null,
};

const $ = (id) => document.getElementById(id);
const cv = $('cv');
const ctx = cv.getContext('2d');

// ---------- DPI 自适应 ----------
function fitCanvas() {
  const wrap = cv.parentElement;
  const w = wrap.clientWidth, h = wrap.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  cv.width = w * dpr; cv.height = h * dpr;
  cv.style.width = w + 'px'; cv.style.height = h + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}
window.addEventListener('resize', fitCanvas);

function screenToImage(sx, sy) {
  return { x: (sx - state.offset.x) / state.scale, y: (sy - state.offset.y) / state.scale };
}
function imageToScreen(ix, iy) {
  return { x: ix * state.scale + state.offset.x, y: iy * state.scale + state.offset.y };
}

function draw() {
  ctx.clearRect(0, 0, cv.clientWidth, cv.clientHeight);
  if (!state.image) return;
  ctx.save();
  ctx.translate(state.offset.x, state.offset.y);
  ctx.scale(state.scale, state.scale);
  ctx.imageSmoothingEnabled = state.scale < 2;
  ctx.drawImage(state.image, 0, 0);
  ctx.restore();
  if (state.box) {
    const [x0, y0, x1, y1] = state.box;
    const a = imageToScreen(x0, y0);
    const b = imageToScreen(x1, y1);
    ctx.lineWidth = 2; ctx.strokeStyle = '#ff2d55'; ctx.setLineDash([6, 4]);
    ctx.strokeRect(a.x, a.y, b.x - a.x, b.y - a.y); ctx.setLineDash([]);
    ctx.fillStyle = 'rgba(255,45,85,.15)';
    ctx.fillRect(a.x, a.y, b.x - a.x, b.y - a.y);
  }
}

function setScale(newScale, anchor) {
  newScale = Math.max(0.05, Math.min(40, newScale));
  if (anchor) {
    const before = screenToImage(anchor.x, anchor.y);
    state.scale = newScale;
    const after = imageToScreen(before.x, before.y);
    state.offset.x += anchor.x - after.x;
    state.offset.y += anchor.y - after.y;
  } else { state.scale = newScale; }
  $('zoomLabel').textContent = Math.round(state.scale * 100) + '%';
  draw();
}
function fitImage() {
  if (!state.image) return;
  const wrap = cv.parentElement;
  const sx = wrap.clientWidth / state.imgSize.w;
  const sy = wrap.clientHeight / state.imgSize.h;
  const s = Math.min(sx, sy) * 0.95;
  state.scale = s;
  state.offset.x = (wrap.clientWidth - state.imgSize.w * s) / 2;
  state.offset.y = (wrap.clientHeight - state.imgSize.h * s) / 2;
  $('zoomLabel').textContent = Math.round(state.scale * 100) + '%';
  draw();
}

// ---------- 工具切换 ----------
function setTool(t) {
  state.tool = t;
  document.querySelectorAll('.tool').forEach((b) => b.classList.toggle('active', b.dataset.tool === t));
  cv.className = ''; cv.classList.add('tool-' + t);
}
document.querySelectorAll('.tool').forEach((b) => b.addEventListener('click', () => setTool(b.dataset.tool)));
setTool('box');

window.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'b' || e.key === 'B') setTool('box');
  else if (e.key === 'i' || e.key === 'I') setTool('pick');
  else if (e.code === 'Space') { e.preventDefault(); setTool('pan'); }
});
// === app.js part 2 (追加到 app.js) ===
// 上传样图 / 鼠标 / 缩放 / 吸管

// ---------- 上传样图 ----------
$('tplFile').addEventListener('change', async (e) => {
  const file = e.target.files[0]; if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  const r = await fetch('/api/upload-template', { method: 'POST', body: fd });
  const d = await r.json();
  if (!r.ok) { alert(d.error || '上传失败'); return; }
  state.imageId = d.image_id; state.imgSize = { w: d.width, h: d.height };
  const img = new Image();
  img.onload = () => {
    state.image = img; state.box = null;
    $('boxInfo').textContent = '未框选'; $('saveTplBtn').disabled = true;
    $('canvasEmpty').classList.add('hidden'); fitImage();
  };
  img.src = d.url;
});

function getMouse(e) { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }

cv.addEventListener('mousedown', (e) => {
  if (!state.image) return;
  const p = getMouse(e); const img = screenToImage(p.x, p.y);
  if (state.tool === 'pan' || e.button === 1) {
    state.drag = { type: 'pan', sx: p.x, sy: p.y, ox: state.offset.x, oy: state.offset.y };
    cv.classList.add('panning');
  } else if (state.tool === 'box') {
    state.drag = { type: 'box', startImg: img };
    state.box = [img.x, img.y, img.x, img.y]; draw();
  } else if (state.tool === 'pick') {
    pickColor(Math.round(img.x), Math.round(img.y));
  }
});

cv.addEventListener('mousemove', (e) => {
  const p = getMouse(e); const img = screenToImage(p.x, p.y);
  $('hud').textContent = state.image
    ? `图像 ${Math.round(img.x)},${Math.round(img.y)} · 缩放 ${(state.scale*100).toFixed(0)}%` : '';
  if (!state.drag) return;
  if (state.drag.type === 'pan') {
    state.offset.x = state.drag.ox + (p.x - state.drag.sx);
    state.offset.y = state.drag.oy + (p.y - state.drag.sy); draw();
  } else if (state.drag.type === 'box') {
    const s = state.drag.startImg;
    state.box = [
      Math.max(0, Math.min(s.x, img.x)), Math.max(0, Math.min(s.y, img.y)),
      Math.min(state.imgSize.w, Math.max(s.x, img.x)), Math.min(state.imgSize.h, Math.max(s.y, img.y)),
    ];
    draw();
  }
});

cv.addEventListener('mouseup', () => {
  if (state.drag && state.drag.type === 'box' && state.box) {
    const [x0, y0, x1, y1] = state.box.map(Math.round);
    if (x1 - x0 >= 5 && y1 - y0 >= 5) {
      state.box = [x0, y0, x1, y1];
      $('boxInfo').textContent = `(${x0},${y0})-(${x1},${y1}) · ${x1-x0}×${y1-y0}`;
      $('saveTplBtn').disabled = false;
    } else { state.box = null; $('boxInfo').textContent = '未框选'; }
    draw();
  }
  state.drag = null; cv.classList.remove('panning');
});

cv.addEventListener('wheel', (e) => {
  if (!state.image) return; e.preventDefault();
  const p = getMouse(e); const factor = e.deltaY < 0 ? 1.15 : 1/1.15;
  setScale(state.scale * factor, p);
}, { passive: false });

$('zoomIn').onclick = () => setScale(state.scale * 1.25, { x: cv.clientWidth/2, y: cv.clientHeight/2 });
$('zoomOut').onclick = () => setScale(state.scale / 1.25, { x: cv.clientWidth/2, y: cv.clientHeight/2 });
$('zoomFit').onclick = fitImage;
$('zoom100').onclick = () => setScale(1, { x: cv.clientWidth/2, y: cv.clientHeight/2 });

async function pickColor(x, y) {
  if (!state.imageId) return;
  const r = parseInt($('pickRadius').value, 10);
  const res = await fetch('/api/pick-color', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ image_id: state.imageId, x, y, radius: r }),
  });
  const d = await res.json(); if (!res.ok) return;
  state.color = d.color;
  const [R, G, B] = d.color;
  const hex = '#' + [R, G, B].map((v) => v.toString(16).padStart(2, '0')).join('');
  $('swatch').style.background = hex;
  $('colorInfo').textContent = `${hex} (${R},${G},${B})`;
}
$('clearColor').onclick = () => {
  state.color = null;
  $('swatch').style.background = '#ffffff';
  $('colorInfo').textContent = '— 留空 (每图自动采样)';
};
$('pickRadius').addEventListener('input', (e) => { $('pickRadiusLabel').textContent = e.target.value + 'px'; });
// === app.js part 3 (追加到 app.js) ===

// ---------- 模板库 ----------
async function loadTemplates() {
  const r = await fetch('/api/templates');
  const d = await r.json();
  state.templates = d.templates || [];
  const ids = new Set(state.templates.map((t) => t.id));
  state.selectedTpls = new Set([...state.selectedTpls].filter((id) => ids.has(id)));
  renderTemplates();
  updateRunBtn();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
}

function renderTemplates() {
  const list = $('tplList');
  $('tplCount').textContent = state.templates.length;
  if (state.templates.length === 0) {
    list.innerHTML = '<div class="tpl-empty">尚未保存任何模板</div>';
    return;
  }
  list.innerHTML = '';
  state.templates.forEach((t) => {
    const el = document.createElement('div');
    el.className = 'tpl-item' + (state.selectedTpls.has(t.id) ? ' checked' : '');
    el.dataset.id = t.id;
    const color = t.color ? `rgb(${t.color.join(',')})` : 'transparent';
    el.innerHTML = `
      <input type="checkbox" ${state.selectedTpls.has(t.id) ? 'checked' : ''} />
      <img class="tpl-thumb" src="${t.thumb_url}" />
      <div class="tpl-info">
        <div class="tpl-name" title="双击重命名">${escapeHtml(t.name)}</div>
        <div class="tpl-meta">
          ${t.color ? `<span class="tpl-color" style="background:${color}"></span>` : ''}
          ${t.width}×${t.height}
        </div>
      </div>
      <button class="tpl-del" title="删除">×</button>`;
    list.appendChild(el);
  });
}

$('tplList').addEventListener('click', async (e) => {
  const item = e.target.closest('.tpl-item');
  if (!item) return;
  const id = item.dataset.id;
  if (e.target.classList.contains('tpl-del')) {
    if (!confirm('删除这个模板？')) return;
    const r = await fetch('/api/templates/' + id, { method: 'DELETE' });
    if (r.ok) { state.selectedTpls.delete(id); loadTemplates(); }
    return;
  }
  if (state.selectedTpls.has(id)) state.selectedTpls.delete(id);
  else state.selectedTpls.add(id);
  renderTemplates();
  updateRunBtn();
});

$('tplList').addEventListener('dblclick', async (e) => {
  if (!e.target.classList.contains('tpl-name')) return;
  const item = e.target.closest('.tpl-item');
  const id = item.dataset.id;
  const old = e.target.textContent;
  const next = prompt('模板名称：', old);
  if (!next || next.trim() === old) return;
  const r = await fetch('/api/rename-template', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, name: next.trim() }),
  });
  if (r.ok) loadTemplates();
});

$('tplSelectAll').onclick = () => { state.templates.forEach((t) => state.selectedTpls.add(t.id)); renderTemplates(); updateRunBtn(); };
$('tplSelectNone').onclick = () => { state.selectedTpls.clear(); renderTemplates(); updateRunBtn(); };
$('tplRefresh').onclick = loadTemplates;

$('saveTplBtn').onclick = async () => {
  const name = prompt('模板名称：', '');
  if (name === null) return;
  const r = await fetch('/api/save-template', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      image_id: state.imageId, box: state.box,
      name: name.trim(), color: state.color,
    }),
  });
  const d = await r.json();
  if (!r.ok) { alert(d.error || '保存失败'); return; }
  state.selectedTpls.add(d.template.id);
  document.querySelector('.step[data-step="2"]').classList.add('active');
  loadTemplates();
};

// ---------- 批量处理 ----------
$('batchFiles').addEventListener('change', async (e) => {
  const files = Array.from(e.target.files); if (!files.length) return;
  const fd = new FormData(); files.forEach((f) => fd.append('files', f));
  const r = await fetch('/api/upload-batch', { method: 'POST', body: fd });
  const d = await r.json();
  if (!r.ok) { alert(d.error || '上传失败'); return; }
  state.batchId = d.batch_id;
  $('batchInfo').textContent = `${d.count} 张`;
  updateRunBtn();
});

function updateRunBtn() {
  $('runBtn').disabled = !(state.selectedTpls.size > 0 && state.batchId);
}

$('runBtn').onclick = async () => {
  const wantPersam = $('usePersam').checked;
  if (wantPersam) {
    const ok = await ensureRuntimeReady();
    if (!ok) return;  // 用户取消或下载失败
  }
  $('runBtn').disabled = true;
  $('prog').style.display = 'block'; $('prog').removeAttribute('value');
  const payload = {
    batch_id: state.batchId,
    template_ids: [...state.selectedTpls],
    mode: $('mode').value,
    color: state.color,
    pad_x: parseInt($('padX').value, 10),
    pad_y: parseInt($('padY').value, 10),
    pad_bottom: parseInt($('padBottom').value, 10),
    pct_x: (parseFloat($('pctX').value) || 0) / 100,
    pct_top: (parseFloat($('pctTop').value) || 0) / 100,
    pct_bottom: (parseFloat($('pctBottom').value) || 0) / 100,
    min_score: parseFloat($('minScore').value),
    use_persam: wantPersam,
  };
  try {
    const r = await fetch('/api/process', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '处理失败');
    renderResults(d.results, { use_persam: d.use_persam, fallback_reason: d.fallback_reason });
  } catch (err) { alert(err.message); }
  finally { $('prog').style.display = 'none'; updateRunBtn(); }
};

// renderResults 在文件末尾的 P0-5 模块中定义（带 manual-pending 状态卡片）

$('results').addEventListener('click', (e) => {
  if (e.target.tagName === 'IMG' && e.target.dataset.full) {
    $('lbImg').src = e.target.dataset.full;
    $('lightbox').classList.add('open');
  }
});
$('lbClose').onclick = () => $('lightbox').classList.remove('open');
$('lightbox').addEventListener('click', (e) => {
  if (e.target.id === 'lightbox') $('lightbox').classList.remove('open');
});

fitCanvas();
loadTemplates();

// ============ 高精度模式 runtime ============

let runtimeReadyCache = null;  // null=未知 / true=就绪 / false=缺失
async function refreshRuntimeStatus() {
  try {
    const r = await fetch('/api/runtime/status');
    const d = await r.json();
    runtimeReadyCache = !!d.ready;
    const badge = $('persamBadge');
    if (d.ready) {
      badge.hidden = false;
      badge.textContent = d.dev_fallback ? '开发模式' : '已就绪';
      badge.classList.add('ready');
    } else if ($('usePersam').checked) {
      badge.hidden = false;
      badge.textContent = '未就绪';
      badge.classList.remove('ready');
    } else {
      badge.hidden = true;
    }
    return d;
  } catch (e) {
    runtimeReadyCache = null;
    return null;
  }
}

$('usePersam').addEventListener('change', () => {
  refreshRuntimeStatus();
});

function formatBytes(n) {
  if (n == null) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

let rtAbortCtrl = null;
let rtResolver = null;

function openRuntimeModal(autoStart = false) {
  $('runtimeModal').hidden = false;
  $('rtLog').textContent = '';
  $('rtPhase').textContent = '等待开始';
  $('rtSpeed').textContent = '';
  $('rtBytes').textContent = '';
  $('rtPercent').textContent = '';
  $('rtProg').value = 0;
  $('rtStart').hidden = false;
  $('rtStart').disabled = false;
  $('rtStart').textContent = '开始下载';
  $('rtCancel').hidden = true;
  $('rtCancel').textContent = '取消下载';
  if (autoStart) startRuntimeDownload();
}

function closeRuntimeModal(success) {
  $('runtimeModal').hidden = true;
  if (rtAbortCtrl) { try { rtAbortCtrl.abort(); } catch {} rtAbortCtrl = null; }
  if (rtResolver) { rtResolver(success); rtResolver = null; }
}

$('rtClose').onclick = () => closeRuntimeModal(false);
$('rtCancel').onclick = () => closeRuntimeModal(false);
$('rtStart').onclick = () => startRuntimeDownload();

async function startRuntimeDownload() {
  $('rtStart').hidden = true;
  $('rtCancel').hidden = false;
  $('rtPhase').textContent = '连接下载源…';
  rtAbortCtrl = new AbortController();
  try {
    const resp = await fetch('/api/runtime/install', {
      method: 'POST',
      signal: rtAbortCtrl.signal,
      headers: { 'Accept': 'text/event-stream' },
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop() || '';
      for (const chunk of parts) {
        const line = chunk.split('\n').find((l) => l.startsWith('data:'));
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
        handleRuntimeEvent(ev);
        if (ev.event === 'done') {
          await refreshRuntimeStatus();
          closeRuntimeModal(true);
          return;
        }
        if (ev.event === 'error') {
          $('rtPhase').textContent = '✗ ' + (ev.message || '下载失败');
          let msg = ev.message || ev.component || '下载失败';
          if (ev.hint) msg += '\n提示：' + ev.hint;
          if (ev.code) msg = '[' + ev.code + '] ' + msg;
          const log = $('rtLog');
          log.textContent += '\n' + msg + '\n';
          log.scrollTop = log.scrollHeight;
          $('rtStart').hidden = false;
          $('rtStart').disabled = false;
          $('rtStart').textContent = '重试下载';
          $('rtCancel').hidden = false;
          $('rtCancel').textContent = '关闭';
          return;
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') alert('下载中断：' + e.message);
  } finally {
    $('rtStart').hidden = false;
    $('rtStart').disabled = false;
    $('rtCancel').hidden = true;
  }
}

function handleRuntimeEvent(ev) {
  if (ev.event === 'start') {
    $('rtPhase').textContent = '开始下载 ' + (ev.components || []).join(' + ');
  } else if (ev.event === 'phase') {
    $('rtPhase').textContent = ev.message || ev.component;
  } else if (ev.event === 'progress') {
    if (ev.total) {
      $('rtProg').max = ev.total;
      $('rtProg').value = ev.done;
      $('rtPercent').textContent = (ev.percent || 0).toFixed(1) + '%';
    } else {
      $('rtProg').removeAttribute('value');
      $('rtPercent').textContent = '';
    }
    $('rtBytes').textContent = `${formatBytes(ev.done)} / ${ev.total ? formatBytes(ev.total) : '?'}`;
    $('rtSpeed').textContent = ev.speed ? `${formatBytes(ev.speed)}/s` : '';
  } else if (ev.event === 'log') {
    const log = $('rtLog');
    log.textContent += ev.message + '\n';
    log.scrollTop = log.scrollHeight;
  } else if (ev.event === 'skip') {
    $('rtPhase').textContent = `跳过 ${ev.component}: ${ev.reason}`;
  } else if (ev.event === 'done') {
    $('rtPhase').textContent = '✓ 安装完成';
    $('rtProg').value = $('rtProg').max;
    $('rtPercent').textContent = '100%';
  }
}

async function ensureRuntimeReady() {
  const status = await refreshRuntimeStatus();
  if (status && status.ready) return true;
  // 弹窗确认下载
  return await new Promise((resolve) => {
    rtResolver = resolve;
    openRuntimeModal(false);
  });
}

// ============ 示例素材 ============

async function loadExamplesList() {
  try {
    const r = await fetch('/api/examples');
    const d = await r.json();
    const list = d.examples || [];
    if (!list.length) { $('exampleRow').hidden = true; return; }
    const sel = $('exampleSel');
    sel.innerHTML = list.map((e) =>
      `<option value="${e.id}">${escapeHtml(e.name)} · ${e.batch_count} 张</option>`
    ).join('');
    $('exampleRow').hidden = false;
  } catch (e) {
    $('exampleRow').hidden = true;
  }
}

$('loadExampleBtn').onclick = async () => {
  const id = $('exampleSel').value;
  if (!id) return;
  $('loadExampleBtn').disabled = true;
  try {
    const r = await fetch('/api/load-example', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '加载失败');
    state.batchId = d.batch_id;
    $('batchInfo').textContent = `${d.count} 张（示例）`;
    state.selectedTpls.add(d.template.id);
    await loadTemplates();
    updateRunBtn();
    alert('示例已加载：1 个模板 + ' + d.count + ' 张批量图，点「开始批量擦除」即可。');
  } catch (e) {
    alert(e.message);
  } finally {
    $('loadExampleBtn').disabled = false;
  }
};

// 启动时拉一次状态
refreshRuntimeStatus();
loadExamplesList();

// ============ P0-4 单图速擦 + P0-5 手动修复 ============

state.mode = 'batch';  // 'batch' | 'single' | 'manual'
state.singleResultUrl = null;
state.manualContext = null;   // { batch_id, file, original_status, card_el }
const $$ = (s) => document.querySelectorAll(s);

function setMode(m) {
  state.mode = m;
  $$('.mode-tab').forEach((t) => t.classList.toggle('active', t.dataset.mode === m));
  $$('.batch-only').forEach((p) => { p.hidden = m !== 'batch'; });
  $$('.single-only').forEach((p) => { p.hidden = m !== 'single'; });
  $('batchSteps').hidden = m !== 'batch';
  $('singleSteps').hidden = m !== 'single';
  // 切到单图模式：清掉左侧 canvas 的当前图（避免和样图混淆）
  if (m === 'single') {
    if (state.image && !state.singleImageId) {
      // 用户先在批量模式上传了样图：保留即可，单图模式也能直接用
    }
    $('results').innerHTML = '';
  } else if (m === 'batch') {
    $('singleCompare').hidden = true;
  }
}

$$('.mode-tab').forEach((t) => t.addEventListener('click', () => setMode(t.dataset.mode)));

// ---- 单图速擦：上传 ----
$('singleFile').addEventListener('change', async (e) => {
  const file = e.target.files[0]; if (!file) return;
  const fd = new FormData(); fd.append('file', file);
  const r = await fetch('/api/upload-template', { method: 'POST', body: fd });
  const d = await r.json();
  if (!r.ok) { alert(d.error || '上传失败'); return; }
  state.imageId = d.image_id;
  state.singleImageId = d.image_id;
  state.imgSize = { w: d.width, h: d.height };
  $('singleImgInfo').textContent = `${d.width}×${d.height}`;
  const img = new Image();
  img.onload = () => {
    state.image = img; state.box = null;
    $('singleBoxInfo').textContent = '未框选';
    $('singleRunBtn').disabled = true;
    $('canvasEmpty').classList.add('hidden');
    fitImage();
    document.querySelector('#singleSteps .step[data-step="2"]')?.classList.add('active');
  };
  img.src = d.url;
});

// ---- 单图模式：框选后更新状态 ----
function notifyBoxChanged() {
  if (state.mode === 'single' && state.box) {
    $('singleBoxInfo').textContent =
      `(${state.box[0]},${state.box[1]})-(${state.box[2]},${state.box[3]})`;
    $('singleRunBtn').disabled = false;
    document.querySelector('#singleSteps .step[data-step="3"]')?.classList.add('active');
  } else if (state.mode === 'manual' && state.box && state.manualContext) {
    $('manualRunBtn') && ($('manualRunBtn').disabled = false);
  }
}

// 注入到现有的 mouseup 后回调（轻量 patch）
const _origUp = cv.onmouseup;
cv.addEventListener('mouseup', () => { setTimeout(notifyBoxChanged, 0); });

// ---- 一键擦除（单图） ----
$('singleRunBtn').onclick = async () => {
  if (!state.singleImageId || !state.box) return;
  const wantPersam = $('singleUsePersam').checked;
  if (wantPersam) {
    const ok = await ensureRuntimeReady();
    if (!ok) return;
  }
  $('singleRunBtn').disabled = true;
  $('singleProg').style.display = 'block'; $('singleProg').removeAttribute('value');
  try {
    const payload = {
      image_id: state.singleImageId,
      box: state.box,
      mode: $('singleMode').value,
      use_persam: wantPersam,
      pct_x: (parseFloat($('singlePctX').value) || 0) / 100,
      pct_top: (parseFloat($('singlePctTop').value) || 0) / 100,
      pct_bottom: (parseFloat($('singlePctBottom').value) || 0) / 100,
    };
    const r = await fetch('/api/single-erase', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '擦除失败');
    showSingleResult(state.image.src, d.url, d.meta);
  } catch (e) { alert(e.message); }
  finally { $('singleProg').style.display = 'none'; $('singleRunBtn').disabled = false; }
};

function showSingleResult(beforeUrl, afterUrl, meta) {
  $('singleBeforeImg').src = beforeUrl;
  $('singleAfterImg').src = afterUrl;
  $('singleDownload').href = afterUrl;
  const persamHint = meta && meta.persam ? ' · PerSAM-F 精修' : '';
  $('singleMeta').textContent = `${meta.box[0]},${meta.box[1]} → ${meta.box[2]},${meta.box[3]}${persamHint}`;
  $('singleCompare').hidden = false;
}

$('singleAfterImg').onclick = $('singleBeforeImg').onclick = (e) => {
  if (e.target.src) {
    $('lbImg').src = e.target.src;
    $('lightbox').classList.add('open');
  }
};

// ============ P0-5 手动修复（结果项的 manual-pending 状态）============

// 改写 renderResults 的结果卡片渲染：状态 = manual-pending 时显示"手动定位"按钮
function renderResults(items, meta) {
  const grid = $('results');
  const ok = items.filter((i) => i.status === 'ok').length;
  const pending = items.filter((i) => i.status === 'manual-pending').length;
  let header = `<h3>处理结果 ${ok}/${items.length}` +
               (pending ? ` <span class="badge">${pending} 张待手动</span>` : '') + '</h3>';
  if (meta && meta.fallback_reason) {
    header += `<div class="hint" style="color:#c0392b;margin-bottom:6px">⚠ ${escapeHtml(meta.fallback_reason)}</div>`;
  } else if (meta && meta.use_persam) {
    header += `<div class="hint" style="color:#2c8a3c;margin-bottom:6px">✓ 已启用高精度模式 (PerSAM-F)</div>`;
  }
  grid.innerHTML = header;

  const wrap = document.createElement('div');
  wrap.className = 'result-grid';
  items.forEach((it) => {
    const el = document.createElement('div');
    const klass = it.status === 'ok' ? '' :
                  (it.status === 'manual-pending' ? ' pending' : ' err');
    el.className = 'result-item' + klass;
    el.dataset.file = it.file;
    if (it.status === 'ok') {
      const n = (it.matches && it.matches.length) || 1;
      const names = (it.matches || []).map((m) => escapeHtml(m.template_name)).join(' + ');
      el.innerHTML = `
        <img src="${it.url}" data-full="${it.url}" />
        <div class="name">${escapeHtml(it.file)}</div>
        <div class="meta">📌 ${n > 1 ? `擦除 ${n} 处: ${names}` : escapeHtml(it.template_name || '')} · score ${it.score || ''}</div>`;
    } else if (it.status === 'manual-pending') {
      el.innerHTML = `
        <div class="name">${escapeHtml(it.file)}</div>
        <div class="meta">⚠ ${escapeHtml(it.error || '未匹配')}</div>
        <button class="btn-mini manual-btn">✋ 手动定位</button>`;
    } else {
      el.innerHTML = `
        <div class="name">${escapeHtml(it.file)}</div>
        <div class="meta">✗ ${escapeHtml(it.status)} ${escapeHtml(it.error || '')}</div>`;
    }
    wrap.appendChild(el);
  });
  grid.appendChild(wrap);
  grid.classList.remove('empty');
}

// 委托：点 "手动定位" → 切到手动修复 mini-flow
$('results').addEventListener('click', async (e) => {
  if (!e.target.classList.contains('manual-btn')) return;
  const card = e.target.closest('.result-item');
  const file = card.dataset.file;
  if (!state.batchId) { alert('批次信息丢失'); return; }
  // 加载源图到 canvas
  const url = `/uploads/batch_${state.batchId}/${file}`;
  const img = new Image();
  img.onload = () => {
    state.image = img;
    state.imgSize = { w: img.naturalWidth, h: img.naturalHeight };
    state.box = null;
    state.imageId = null;  // 不污染模板上传图
    $('canvasEmpty').classList.add('hidden');
    fitImage();
    setTool('box');
    state.manualContext = {
      batch_id: state.batchId, file, card_el: card, image_url: url,
    };
    openManualPanel();
  };
  img.src = url;
});

function openManualPanel() {
  // 在 batch 面板顶部插一行临时 toolbar
  let bar = $('manualBar');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'manualBar';
    bar.className = 'manual-bar';
    bar.innerHTML = `
      <span>✋ 手动修复：在左侧 Canvas 上框出 logo 区域</span>
      <button class="primary" id="manualRunBtn" disabled>修复这张</button>
      <button class="btn-mini" id="manualCancelBtn">取消</button>
    `;
    document.querySelector('main').before(bar);
    $('manualRunBtn').onclick = manualRunErase;
    $('manualCancelBtn').onclick = closeManualPanel;
  }
  bar.hidden = false;
}

function closeManualPanel() {
  const bar = $('manualBar');
  if (bar) bar.hidden = true;
  state.manualContext = null;
  state.box = null;
}

async function manualRunErase() {
  const ctx = state.manualContext;
  if (!ctx || !state.box) return;
  const wantPersam = $('usePersam').checked;
  if (wantPersam) {
    const ok = await ensureRuntimeReady();
    if (!ok) return;
  }
  $('manualRunBtn').disabled = true;
  $('manualRunBtn').textContent = '修复中…';
  try {
    const payload = {
      batch_id: ctx.batch_id,
      file: ctx.file,
      box: state.box,
      mode: $('mode').value,
      use_persam: wantPersam,
      pad_x: parseInt($('padX').value, 10),
      pad_y: parseInt($('padY').value, 10),
      pad_bottom: parseInt($('padBottom').value, 10),
      pct_x: (parseFloat($('pctX').value) || 0) / 100,
      pct_top: (parseFloat($('pctTop').value) || 0) / 100,
      pct_bottom: (parseFloat($('pctBottom').value) || 0) / 100,
    };
    const r = await fetch('/api/manual-erase', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '修复失败');
    // 更新结果卡片：把 manual-pending 改为 ok
    const card = ctx.card_el;
    card.classList.remove('pending');
    card.innerHTML = `
      <img src="${d.url}?t=${Date.now()}" data-full="${d.url}" />
      <div class="name">${escapeHtml(d.file)}</div>
      <div class="meta">✋ 手动修复 · 已完成</div>`;
    closeManualPanel();
  } catch (e) {
    alert(e.message);
    $('manualRunBtn').disabled = false;
    $('manualRunBtn').textContent = '修复这张';
  }
}
