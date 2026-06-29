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
  };
  try {
    const r = await fetch('/api/process', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || '处理失败');
    renderResults(d.results);
  } catch (err) { alert(err.message); }
  finally { $('prog').style.display = 'none'; updateRunBtn(); }
};

function renderResults(items) {
  const grid = $('results');
  const ok = items.filter((i) => i.status === 'ok').length;
  grid.innerHTML = `<h3>处理结果 ${ok}/${items.length}</h3>`;
  const wrap = document.createElement('div'); wrap.className = 'result-grid';
  items.forEach((it) => {
    const el = document.createElement('div');
    el.className = 'result-item' + (it.status === 'ok' ? '' : ' err');
    if (it.status === 'ok') {
      const n = (it.matches && it.matches.length) || 1;
      const names = (it.matches || []).map((m) => escapeHtml(m.template_name)).join(' + ');
      el.innerHTML = `
        <img src="${it.url}" data-full="${it.url}" />
        <div class="name">${escapeHtml(it.file)}</div>
        <div class="meta">📌 ${n > 1 ? `擦除 ${n} 处: ${names}` : escapeHtml(it.template_name)} · score ${it.score}</div>`;
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
