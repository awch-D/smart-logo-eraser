"""Logo Eraser Web Tool — 本地调参 + 批处理工具

接口（v2，支持模板库 + 按需 runtime + 示例素材）：
  页面
    GET  /                                  工具主页
    GET  /uploads/<path>                    上传文件
    GET  /sessions/<path>                   模板文件
  模板
    POST /api/upload-template               上传待框选样图
    POST /api/save-template                 保存一个新模板（image_id, box, name, color?）
    GET  /api/templates                     列出所有已保存模板
    DELETE /api/templates/<id>              删除模板
    POST /api/pick-color                    在图上指定点取色
  批处理
    POST /api/upload-batch                  上传一批待处理图
    POST /api/process                       批处理：template_ids 数组，每张图选最佳匹配
    GET  /api/result/<batch_id>/<filename>  下载结果
  高精度模式 runtime（按需下载）
    GET  /api/runtime/status                查询 torch + MobileSAM 权重是否就绪
    POST /api/runtime/install               触发下载安装，SSE 推送进度
  示例素材
    GET  /api/examples                      列出内置示例组
    POST /api/load-example                  把某组示例一键拷贝到当前会话
  单图速擦 / 手动修复
    POST /api/single-erase                  一次性擦除单张图（不写模板库）
    POST /api/manual-erase                  对 batch 内某张图手动定位 + 重擦
"""
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from threading import Lock

import cv2
import numpy as np
from flask import (
    Flask, Response, jsonify, request, send_file, send_from_directory,
    stream_with_context,
)
from PIL import Image

from runtime import (
    ensure_runtime_loaded,
    install_runtime_stream,
    runtime_status,
)

ROOT = Path(__file__).parent
DATA_ROOT = Path(os.environ.get('LOGOERASER_DATA', ROOT)).expanduser()
UPLOADS = DATA_ROOT / 'uploads'
OUTPUTS = DATA_ROOT / 'outputs'
SESSIONS = DATA_ROOT / 'sessions'
TEMPLATES_INDEX = DATA_ROOT / 'templates.json'
for d in (UPLOADS, OUTPUTS, SESSIONS):
    d.mkdir(parents=True, exist_ok=True)

_index_lock = Lock()


def _load_index() -> list:
    if not TEMPLATES_INDEX.exists():
        return []
    try:
        return json.loads(TEMPLATES_INDEX.read_text(encoding='utf-8'))
    except Exception:
        return []


def _save_index(items: list):
    TEMPLATES_INDEX.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')


def _find_iopaint_bin():
    env = os.environ.get('IOPAINT_BIN')
    if env and Path(env).exists():
        return Path(env)
    dev = ROOT.parent / '.venv/bin/iopaint'
    if dev.exists():
        return dev
    return None


IOPAINT_BIN = _find_iopaint_bin()
ALLOWED_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024


# ---------- 工具函数 ----------

def _save_upload(file_storage, dest_dir: Path) -> Path:
    name = Path(file_storage.filename).name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXT:
        raise ValueError(f'不支持的格式: {suffix}')
    uid = uuid.uuid4().hex[:10]
    out = dest_dir / f'{uid}{suffix}'
    file_storage.save(out)
    return out


def _match_template(img_gray, tpl_gray, min_score=0.7):
    """多尺度匹配单个模板。返回 (x0,y0,x1,y1,score,scale) 或 None"""
    th, tw = tpl_gray.shape
    H, W = img_gray.shape
    best = None
    for scale in np.linspace(0.7, 1.5, 17):
        nw, nh = int(tw * scale), int(th * scale)
        if nw < 20 or nh < 20 or nw > W or nh > H:
            continue
        ts = cv2.resize(tpl_gray, (nw, nh), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(img_gray, ts, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if best is None or mx > best[0]:
            best = (float(mx), loc, nw, nh, float(scale))
    if best is None or best[0] < min_score:
        return None
    score, (x, y), w, h, scale = best
    return (int(x), int(y), int(x + w), int(y + h), score, scale)


def _pick_best_template(img_bgr, templates, min_score):
    """对一张图试所有模板，返回得分最高的 (template_entry, match_box)"""
    img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    best = None
    for t in templates:
        tpl_path = SESSIONS / t['file']
        tpl_bgr = cv2.imread(str(tpl_path))
        if tpl_bgr is None:
            continue
        tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
        m = _match_template(img_gray, tpl_gray, min_score)
        if m is None:
            continue
        if best is None or m[4] > best[1][4]:
            best = (t, m)
    return best  # (template_entry, (x0,y0,x1,y1,score,scale))  or None


def _match_all_templates(img_bgr, templates, min_score):
    """对一张图试所有模板，返回所有过阈值的命中 [(template_entry, match)]"""
    img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    hits = []
    for t in templates:
        tpl_path = SESSIONS / t['file']
        tpl_bgr = cv2.imread(str(tpl_path))
        if tpl_bgr is None:
            continue
        tpl_gray = cv2.cvtColor(tpl_bgr, cv2.COLOR_BGR2GRAY)
        m = _match_template(img_gray, tpl_gray, min_score)
        if m is not None:
            hits.append((t, m))
    # 按得分降序
    hits.sort(key=lambda h: -h[1][4])
    return hits


def _sample_surround_color(img_rgb, box, band=14):
    x0, y0, x1, y1 = box
    H, W, _ = img_rgb.shape
    sx0 = max(0, x0 - band)
    sy0 = max(0, y0 - band)
    sx1 = min(W, x1 + band)
    sy1 = min(H, y1 + band)
    region = img_rgb[sy0:sy1, sx0:sx1].copy()
    iy0, ix0 = y0 - sy0, x0 - sx0
    iy1, ix1 = y1 - sy0, x1 - sx0
    mask = np.ones(region.shape[:2], dtype=bool)
    mask[iy0:iy1, ix0:ix1] = False
    surround = region[mask]
    if len(surround) == 0:
        return None
    luma = surround.mean(axis=1)
    bright = surround[luma > luma.mean()]
    pool = bright if len(bright) > 50 else surround
    return tuple(int(c) for c in np.median(pool, axis=0))


def _expand_box(box, W, H, pad_x=0, pad_top=0, pad_bottom=0,
                pct_x=0.0, pct_top=0.0, pct_bottom=0.0):
    """按匹配框尺寸自适应膨胀。返回 (fx0, fy0, fx1, fy1)。

    pad_*  绝对像素扩展（可为负）
    pct_*  按匹配框宽/高的百分比扩展（0.10 = 10%）；和 pad_* 叠加。
    """
    x0, y0, x1, y1 = box
    bw = max(1, x1 - x0)
    bh = max(1, y1 - y0)
    ex = int(round(pad_x + bw * pct_x))
    et = int(round(pad_top + bh * pct_top))
    eb = int(round(pad_bottom + bh * pct_bottom))
    fx0 = max(0, x0 - ex)
    fy0 = max(0, y0 - et)
    fx1 = min(W, x1 + ex)
    fy1 = min(H, y1 + eb)
    if fx1 <= fx0:
        fx1 = min(W, fx0 + 1)
    if fy1 <= fy0:
        fy1 = min(H, fy0 + 1)
    return fx0, fy0, fx1, fy1


def _learn_logo_color_bgr(tpl_bgr):
    """从模板暗色像素聚类得 logo 主色 BGR（中位数）。"""
    pixels = tpl_bgr.reshape(-1, 3)
    luma = pixels.mean(axis=1)
    mask = luma < np.percentile(luma, 30)
    dark = pixels[mask]
    if len(dark) == 0:
        return None
    return tuple(int(c) for c in np.median(dark, axis=0))


def _build_logo_color_mask(roi_bgr, target_bgr, tol):
    """HSV 空间下，距离 target_bgr 小于 tol 的像素标 255。"""
    target = np.uint8([[list(target_bgr)]])
    target_hsv = cv2.cvtColor(target, cv2.COLOR_BGR2HSV)[0, 0].astype(np.int16)
    roi_hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV).astype(np.int16)
    dh = np.abs(roi_hsv[:, :, 0] - target_hsv[0])
    dh = np.minimum(dh, 180 - dh)  # 环形色相
    ds = np.abs(roi_hsv[:, :, 1] - target_hsv[1])
    dv = np.abs(roi_hsv[:, :, 2] - target_hsv[2])
    dist = np.sqrt((dh * 2.0) ** 2 + ds ** 2 + (dv * 0.5) ** 2)
    return (dist < tol).astype(np.uint8) * 255


def _fill_color(img_rgb, box, pad_x=6, pad_top=6, pad_bottom=0,
                pct_x=0.05, pct_top=0.05, pct_bottom=0.0,
                color=(255, 255, 255),
                tpl_bgr=None, color_tol=80, dilate=6):
    """擦除 logo。

    - tpl_bgr 提供：走自适应算法（学色 + HSV mask + 膨胀 + cv2.inpaint），
      对纯色背景接近完美。pad_*/pct_* 此时被忽略，直接用模板 box。
    - tpl_bgr 为 None：走旧的整块 fill 行为（box ± padding 整块刷 color）。
    """
    H, W, _ = img_rgb.shape

    # --- 旧路径：整块 fill（兼容性兜底） ---
    if tpl_bgr is None:
        fb = _expand_box(box, W, H, pad_x, pad_top, pad_bottom,
                         pct_x, pct_top, pct_bottom)
        out = img_rgb.copy()
        fx0, fy0, fx1, fy1 = fb
        out[fy0:fy1, fx0:fx1] = color
        return out, fb

    # --- 新路径：自学色 + cv2.inpaint ---
    logo_bgr = _learn_logo_color_bgr(tpl_bgr)
    if logo_bgr is None:
        # 模板拿不到主色，降级旧路径
        fb = _expand_box(box, W, H, pad_x, pad_top, pad_bottom,
                         pct_x, pct_top, pct_bottom)
        out = img_rgb.copy()
        fx0, fy0, fx1, fy1 = fb
        out[fy0:fy1, fx0:fx1] = color
        return out, fb

    x0, y0, x1, y1 = box
    pad_w = dilate + 2
    wx0 = max(0, x0 - pad_w)
    wy0 = max(0, y0 - pad_w)
    wx1 = min(W, x1 + pad_w)
    wy1 = min(H, y1 + pad_w)

    img_bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
    work_roi = img_bgr[wy0:wy1, wx0:wx1]
    sub_mask = _build_logo_color_mask(work_roi, logo_bgr, color_tol)

    k = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (dilate * 2 + 1, dilate * 2 + 1)
    )
    sub_mask = cv2.dilate(sub_mask, k)

    full_mask = np.zeros((H, W), dtype=np.uint8)
    full_mask[wy0:wy1, wx0:wx1] = sub_mask

    try:
        out_bgr = cv2.inpaint(img_bgr, full_mask, 3, cv2.INPAINT_TELEA)
    except cv2.error:
        # inpaint 异常时降级到旧路径
        fb = _expand_box(box, W, H, pad_x, pad_top, pad_bottom,
                         pct_x, pct_top, pct_bottom)
        out = img_rgb.copy()
        fx0, fy0, fx1, fy1 = fb
        out[fy0:fy1, fx0:fx1] = color
        return out, fb

    out_rgb = cv2.cvtColor(out_bgr, cv2.COLOR_BGR2RGB)
    return out_rgb, (wx0, wy0, wx1, wy1)


def _run_iopaint(img_path: Path, mask: np.ndarray, out_path: Path):
    """运行 IOPaint LaMa 修复。

    优先用 IOPAINT_BIN（venv 装的 console_script），不可用时回退到
    `python -m iopaint`，避免 console_script 的 shebang 因目录迁移而失效。
    """
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / 'in').mkdir()
        (tdp / 'out').mkdir()
        shutil.copy(img_path, tdp / 'in' / img_path.name)
        cv2.imwrite(str(tdp / 'mask.png'), mask)
        args = [
            'run', '--model=lama', '--device=mps',
            f'--image={tdp / "in"}',
            f'--mask={tdp / "mask.png"}',
            f'--output={tdp / "out"}',
        ]
        bin_ok = IOPAINT_BIN is not None and Path(IOPAINT_BIN).exists()
        attempts = []
        if bin_ok:
            attempts.append([str(IOPAINT_BIN)] + args)
        # 模块方式兜底（需要 iopaint 在 import 路径上）
        import sys as _sys
        attempts.append([_sys.executable, '-m', 'iopaint'] + args)

        last_err = None
        for cmd in attempts:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if r.returncode == 0:
                    png = next((tdp / 'out').glob('*.png'))
                    Image.open(png).convert('RGB').save(out_path, 'JPEG', quality=95)
                    return
                last_err = r.stderr[-500:] or r.stdout[-500:]
            except (FileNotFoundError, OSError) as e:
                last_err = str(e)
                continue
        raise RuntimeError(f'IOPaint 调用失败：{last_err or "未知错误"}')


# ---------- 页面 ----------

@app.route('/')
def index():
    return send_from_directory('templates', 'index.html')


@app.route('/uploads/<path:filename>')
def uploaded(filename):
    return send_from_directory(UPLOADS, filename)


@app.route('/sessions/<path:name>')
def session_file(name):
    return send_from_directory(SESSIONS, name)


# ---------- API：模板 ----------

@app.post('/api/upload-template')
def upload_template():
    f = request.files.get('file')
    if not f:
        return jsonify(error='缺少 file'), 400
    try:
        path = _save_upload(f, UPLOADS)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    img = Image.open(path)
    return jsonify(image_id=path.name, url=f'/uploads/{path.name}',
                   width=img.size[0], height=img.size[1])


@app.post('/api/save-template')
def save_template():
    data = request.get_json(force=True)
    image_id = data.get('image_id')
    box = data.get('box')
    name = (data.get('name') or '').strip() or '未命名模板'
    color = data.get('color')  # 可选：保存时附带默认填充色
    if not image_id or not box or len(box) != 4:
        return jsonify(error='参数错误'), 400
    src = UPLOADS / image_id
    if not src.exists():
        return jsonify(error='样图不存在'), 404
    x0, y0, x1, y1 = [int(v) for v in box]
    if x1 - x0 < 5 or y1 - y0 < 5:
        return jsonify(error='框选区域太小'), 400

    img = Image.open(src).convert('RGB')
    crop = img.crop((x0, y0, x1, y1))
    tpl_id = uuid.uuid4().hex[:10]
    tpl_file = f'tpl_{tpl_id}.png'
    crop.save(SESSIONS / tpl_file)

    # 同时再裁一份缩略图供 UI 列表用
    thumb = crop.copy()
    thumb.thumbnail((200, 200))
    thumb.save(SESSIONS / f'thumb_{tpl_id}.png')

    entry = {
        'id': tpl_id,
        'name': name,
        'file': tpl_file,
        'thumb': f'thumb_{tpl_id}.png',
        'width': crop.size[0],
        'height': crop.size[1],
        'box': [x0, y0, x1, y1],
        'color': color,  # 可能为 None
        'created': int(time.time()),
    }
    with _index_lock:
        items = _load_index()
        items.append(entry)
        _save_index(items)
    return jsonify(template=entry)


@app.get('/api/templates')
def list_templates():
    with _index_lock:
        items = _load_index()
    # 补 URL
    for t in items:
        t['url'] = f'/sessions/{t["file"]}'
        t['thumb_url'] = f'/sessions/{t["thumb"]}'
    return jsonify(templates=items)


@app.delete('/api/templates/<tpl_id>')
def delete_template(tpl_id):
    with _index_lock:
        items = _load_index()
        keep = []
        removed = None
        for t in items:
            if t['id'] == tpl_id:
                removed = t
            else:
                keep.append(t)
        if not removed:
            return jsonify(error='not found'), 404
        _save_index(keep)
    for f in (removed['file'], removed['thumb']):
        p = SESSIONS / f
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass
    return jsonify(ok=True)


@app.post('/api/rename-template')
def rename_template():
    data = request.get_json(force=True)
    tpl_id = data.get('id')
    new_name = (data.get('name') or '').strip()
    if not tpl_id or not new_name:
        return jsonify(error='参数错误'), 400
    with _index_lock:
        items = _load_index()
        for t in items:
            if t['id'] == tpl_id:
                t['name'] = new_name
                _save_index(items)
                return jsonify(ok=True, template=t)
    return jsonify(error='not found'), 404


@app.post('/api/pick-color')
def pick_color():
    data = request.get_json(force=True)
    image_id = data.get('image_id')
    x = int(data.get('x', 0))
    y = int(data.get('y', 0))
    radius = max(1, int(data.get('radius', 5)))
    src = UPLOADS / image_id
    if not src.exists():
        return jsonify(error='图不存在'), 404
    arr = np.array(Image.open(src).convert('RGB'))
    H, W, _ = arr.shape
    x0 = max(0, x - radius); y0 = max(0, y - radius)
    x1 = min(W, x + radius + 1); y1 = min(H, y + radius + 1)
    region = arr[y0:y1, x0:x1].reshape(-1, 3)
    med = np.median(region, axis=0).astype(int)
    return jsonify(color=[int(med[0]), int(med[1]), int(med[2])])


# ---------- API：批处理 ----------

@app.post('/api/upload-batch')
def upload_batch():
    batch_id = uuid.uuid4().hex[:10]
    bdir = UPLOADS / f'batch_{batch_id}'
    bdir.mkdir()
    saved = []
    for f in request.files.getlist('files'):
        try:
            p = _save_upload(f, bdir)
            saved.append({'name': p.name, 'orig': Path(f.filename).name})
        except ValueError:
            continue
    if not saved:
        return jsonify(error='没有有效图片'), 400
    return jsonify(batch_id=batch_id, files=saved, count=len(saved))


@app.post('/api/process')
def process_batch():
    """批处理: 从多个模板中为每张图选最佳匹配

    参数:
      batch_id: str
      template_ids: [str]  要尝试的模板 id 列表 (必填)
      color: [R,G,B] | null  全局填充色; null 表示对每张图自动采样周围色
      mode: 'fill' | 'iopaint'
      pad_x, pad_y, pad_bottom: int
      min_score: float (默认 0.7)
    """
    data = request.get_json(force=True)
    batch_id = data.get('batch_id')
    template_ids = data.get('template_ids') or []
    mode = data.get('mode', 'fill')
    color = data.get('color')  # None = 自动采样
    pad_x = int(data.get('pad_x', 6))
    pad_y = int(data.get('pad_y', 6))
    pad_bottom = int(data.get('pad_bottom', 0))
    # 百分比 padding：按匹配到的 logo 实际尺寸自适应膨胀
    pct_x = float(data.get('pct_x', 0.05))
    pct_top = float(data.get('pct_top', 0.05))
    pct_bottom = float(data.get('pct_bottom', 0.0))
    min_score = float(data.get('min_score', 0.7))
    # 高精度模式：用 PerSAM-F 生成精修 mask（只在 mode=iopaint 时生效）
    use_persam = bool(data.get('use_persam', False))
    hint_margin = int(data.get('hint_margin', 6))
    hint_margin_pct = float(data.get('hint_margin_pct', 0.5))

    if not template_ids:
        return jsonify(error='至少选择一个模板'), 400

    # ---- 高精度模式预检 ----
    persam = None
    fallback_reason = None
    if use_persam:
        if mode != 'iopaint':
            fallback_reason = '高精度模式仅在 iopaint 模式下生效，已忽略'
            use_persam = False
        elif not ensure_runtime_loaded():
            fallback_reason = '高精度模式 runtime 未就绪，已自动降级为矩形 mask'
            use_persam = False
        else:
            try:
                from persam_engine import PerSamEngine
                persam = PerSamEngine.instance()
            except Exception as e:
                fallback_reason = f'PerSAM 加载失败：{e}，已自动降级为矩形 mask'
                use_persam = False
                persam = None

    bdir = UPLOADS / f'batch_{batch_id}'
    if not bdir.is_dir():
        return jsonify(error='batch 不存在'), 404

    # 解析选中的模板
    with _index_lock:
        all_items = _load_index()
    chosen = [t for t in all_items if t['id'] in template_ids]
    if not chosen:
        return jsonify(error='选中模板都不存在'), 404

    odir = OUTPUTS / f'batch_{batch_id}'
    odir.mkdir(exist_ok=True)

    results = []
    for img_path in sorted(bdir.iterdir()):
        if img_path.suffix.lower() not in ALLOWED_EXT:
            continue
        item = {'file': img_path.name}
        try:
            img_bgr = cv2.imread(str(img_path))
            if img_bgr is None:
                raise RuntimeError('读取失败')

            hits = _match_all_templates(img_bgr, chosen, min_score)
            if not hits:
                item.update(status='manual-pending', error='模板未匹配，可手动定位')
                results.append(item)
                continue

            # 顶层字段仍按分数最高的命中（兼容旧 UI 字段）
            top_tpl, (tx0, ty0, tx1, ty1, top_score, top_scale) = hits[0]
            item.update(
                template_id=top_tpl['id'],
                template_name=top_tpl['name'],
                box=[tx0, ty0, tx1, ty1],
                score=round(top_score, 3),
                scale=round(top_scale, 2),
            )

            out_name = img_path.stem + '_clean.jpg'
            out_path = odir / out_name
            matches_meta = []  # 每次擦除的详情

            if mode == 'fill':
                img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
                working = img_rgb.copy()
                for tpl_entry, (x0, y0, x1, y1, score, scale) in hits:
                    final_color = color or tpl_entry.get('color')
                    if not final_color:
                        final_color = _sample_surround_color(working, (x0, y0, x1, y1))
                    if not final_color:
                        final_color = (255, 255, 255)
                    final_color = tuple(int(c) for c in final_color)
                    # 加载模板 BGR 图供新算法学色
                    tpl_path = SESSIONS / tpl_entry['file']
                    tpl_bgr_img = cv2.imread(str(tpl_path))
                    working, fb = _fill_color(
                        working, (x0, y0, x1, y1),
                        pad_x=pad_x, pad_top=pad_y, pad_bottom=pad_bottom,
                        pct_x=pct_x, pct_top=pct_top, pct_bottom=pct_bottom,
                        color=final_color,
                        tpl_bgr=tpl_bgr_img,
                    )
                    matches_meta.append({
                        'template_id': tpl_entry['id'],
                        'template_name': tpl_entry['name'],
                        'box': [x0, y0, x1, y1],
                        'score': round(score, 3),
                        'scale': round(scale, 2),
                        'fill_box': list(fb),
                        'color': list(final_color),
                    })
                Image.fromarray(working).save(out_path, 'JPEG', quality=95)
                # 顶层 fill_box / color 取首个命中的
                item['fill_box'] = matches_meta[0]['fill_box']
                item['color'] = matches_meta[0]['color']
            else:
                H, W = img_bgr.shape[:2]
                mask = np.zeros((H, W), dtype=np.uint8)
                for tpl_entry, (x0, y0, x1, y1, score, scale) in hits:
                    fx0, fy0, fx1, fy1 = _expand_box(
                        (x0, y0, x1, y1), W, H,
                        pad_x=pad_x, pad_top=pad_y, pad_bottom=pad_bottom,
                        pct_x=pct_x, pct_top=pct_top, pct_bottom=pct_bottom,
                    )
                    match_meta = {
                        'template_id': tpl_entry['id'],
                        'template_name': tpl_entry['name'],
                        'box': [x0, y0, x1, y1],
                        'score': round(score, 3),
                        'scale': round(scale, 2),
                        'fill_box': [fx0, fy0, fx1, fy1],
                    }
                    persam_done = False
                    if use_persam and persam is not None:
                        try:
                            ref_path = SESSIONS / tpl_entry['file']
                            ref_bgr = cv2.imread(str(ref_path))
                            if ref_bgr is None:
                                raise RuntimeError(f'参考图读取失败: {tpl_entry["file"]}')
                            # 模板裁切图的 box 就是整张参考图
                            rh, rw = ref_bgr.shape[:2]
                            persam.encode_reference(tpl_entry['id'], ref_bgr, (0, 0, rw, rh))
                            pmask = persam.segment(
                                tpl_entry['id'],
                                img_bgr,
                                hint_box=(fx0, fy0, fx1, fy1),
                                hint_margin=hint_margin,
                                hint_margin_pct=hint_margin_pct,
                            )
                            if pmask.any():
                                mask[pmask] = 255
                                ys, xs = np.nonzero(pmask)
                                match_meta['persam_box'] = [
                                    int(xs.min()), int(ys.min()),
                                    int(xs.max()) + 1, int(ys.max()) + 1,
                                ]
                                match_meta['persam'] = True
                                persam_done = True
                            else:
                                match_meta['persam'] = False
                                match_meta['persam_error'] = '空 mask，降级为矩形'
                        except Exception as e:
                            match_meta['persam'] = False
                            match_meta['persam_error'] = str(e)

                    if not persam_done:
                        mask[fy0:fy1, fx0:fx1] = 255
                    matches_meta.append(match_meta)
                _run_iopaint(img_path, mask, out_path)
                item['fill_box'] = matches_meta[0]['fill_box']

            item['matches'] = matches_meta
            item.update(status='ok', output=out_name,
                        url=f'/api/result/{batch_id}/{out_name}')
        except Exception as e:
            item.update(status='error', error=str(e))
        results.append(item)

    return jsonify(
        batch_id=batch_id,
        results=results,
        use_persam=use_persam,
        fallback_reason=fallback_reason,
    )


@app.get('/api/result/<batch_id>/<filename>')
def result_file(batch_id, filename):
    if batch_id == 'single':
        return send_from_directory(OUTPUTS / 'single', filename)
    return send_from_directory(OUTPUTS / f'batch_{batch_id}', filename)


# ---------- API：高精度模式 runtime ----------

@app.get('/api/runtime/status')
def api_runtime_status():
    return jsonify(runtime_status())


@app.post('/api/runtime/install')
def api_runtime_install():
    def gen():
        try:
            for ev in install_runtime_stream():
                yield f'data: {json.dumps(ev, ensure_ascii=False)}\n\n'
        except Exception as e:
            payload = {'event': 'error', 'message': str(e), 'ts': time.time()}
            yield f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'

    return Response(
        stream_with_context(gen()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


# ---------- API：示例素材 ----------

EXAMPLES_ROOT = ROOT.parent / 'examples'


def _scan_examples() -> list[dict]:
    """examples/ 目录约定结构::

        examples/
            stock4u/
                meta.json          # {"name": "...", "watermark_text": "STOCK4U", "template": "template.png"}
                template.png       # 样图（含水印），用来框选保存为模板
                template.box.json  # {"box": [x0,y0,x1,y1]} 该样图上水印的精确框
                batch/
                    img1.png
                    img2.png
                    ...
    """
    if not EXAMPLES_ROOT.exists():
        return []
    out = []
    for group_dir in sorted(EXAMPLES_ROOT.iterdir()):
        meta_p = group_dir / 'meta.json'
        if not group_dir.is_dir() or not meta_p.exists():
            continue
        try:
            meta = json.loads(meta_p.read_text(encoding='utf-8'))
        except Exception:
            continue
        batch_dir = group_dir / 'batch'
        batch_files = (
            sorted(p.name for p in batch_dir.iterdir() if p.suffix.lower() in ALLOWED_EXT)
            if batch_dir.is_dir() else []
        )
        out.append({
            'id': group_dir.name,
            'name': meta.get('name', group_dir.name),
            'watermark': meta.get('watermark_text'),
            'template': meta.get('template', 'template.png'),
            'batch_count': len(batch_files),
        })
    return out


@app.get('/api/examples')
def api_examples():
    return jsonify(examples=_scan_examples())


@app.post('/api/load-example')
def api_load_example():
    """把指定示例组加载到当前会话：模板入库 + batch 拷贝。

    返回 {template_id, batch_id, count} 给前端直接跑 process。
    """
    data = request.get_json(force=True)
    group_id = data.get('id')
    if not group_id:
        return jsonify(error='缺少 id'), 400
    group_dir = EXAMPLES_ROOT / group_id
    meta_p = group_dir / 'meta.json'
    if not group_dir.is_dir() or not meta_p.exists():
        return jsonify(error='示例不存在'), 404
    try:
        meta = json.loads(meta_p.read_text(encoding='utf-8'))
    except Exception as e:
        return jsonify(error=f'meta.json 解析失败: {e}'), 500

    tpl_src = group_dir / meta.get('template', 'template.png')
    box_p = group_dir / 'template.box.json'
    if not tpl_src.exists() or not box_p.exists():
        return jsonify(error='示例缺少 template 或 template.box.json'), 500
    try:
        box = json.loads(box_p.read_text(encoding='utf-8')).get('box')
        if not box or len(box) != 4:
            raise ValueError('box 字段无效')
    except Exception as e:
        return jsonify(error=f'box 读取失败: {e}'), 500

    # 1) 复用 save-template 的核心逻辑：裁剪 + 缩略图 + 落库
    img = Image.open(tpl_src).convert('RGB')
    x0, y0, x1, y1 = [int(v) for v in box]
    if x1 - x0 < 5 or y1 - y0 < 5:
        return jsonify(error='示例 box 太小'), 500
    crop = img.crop((x0, y0, x1, y1))
    tpl_id = uuid.uuid4().hex[:10]
    tpl_file = f'tpl_{tpl_id}.png'
    crop.save(SESSIONS / tpl_file)
    thumb = crop.copy()
    thumb.thumbnail((200, 200))
    thumb.save(SESSIONS / f'thumb_{tpl_id}.png')
    entry = {
        'id': tpl_id,
        'name': meta.get('name', group_id) + ' (示例)',
        'file': tpl_file,
        'thumb': f'thumb_{tpl_id}.png',
        'width': crop.size[0],
        'height': crop.size[1],
        'box': [x0, y0, x1, y1],
        'color': meta.get('color'),
        'created': int(time.time()),
    }
    with _index_lock:
        items = _load_index()
        items.append(entry)
        _save_index(items)

    # 2) 复制 batch 图到 uploads/batch_<id>
    batch_id = uuid.uuid4().hex[:10]
    bdir = UPLOADS / f'batch_{batch_id}'
    bdir.mkdir()
    saved = []
    src_batch = group_dir / 'batch'
    if src_batch.is_dir():
        for p in sorted(src_batch.iterdir()):
            if p.suffix.lower() not in ALLOWED_EXT:
                continue
            uid = uuid.uuid4().hex[:10]
            dst = bdir / f'{uid}{p.suffix.lower()}'
            shutil.copy(p, dst)
            saved.append({'name': dst.name, 'orig': p.name})

    return jsonify(
        template_id=tpl_id,
        template=entry,
        batch_id=batch_id,
        files=saved,
        count=len(saved),
    )


# ---------- API：单图速擦 (P0-4) + 手动修复 (P0-5) ----------

def _erase_one(
    img_bgr: np.ndarray,
    box: tuple[int, int, int, int],
    *,
    mode: str = 'fill',
    use_persam: bool = False,
    color=None,
    pad_x: int = 6, pad_y: int = 6, pad_bottom: int = 0,
    pct_x: float = 0.05, pct_top: float = 0.05, pct_bottom: float = 0.0,
    persam_ref_image: np.ndarray = None,
    persam_ref_box=None,
    persam_key: str = None,
) -> tuple[np.ndarray, dict]:
    """对单张图擦除指定 box 区域，返回 (输出 BGR, meta)。

    - persam_ref_image / persam_ref_box / persam_key 为 PerSAM 参考；
      若全部为 None 则默认用本图自身作为参考（单图速擦的常见用法）。
    """
    H, W = img_bgr.shape[:2]
    x0, y0, x1, y1 = [int(v) for v in box]
    if x1 - x0 < 5 or y1 - y0 < 5:
        raise RuntimeError('框选区域太小')

    fx0, fy0, fx1, fy1 = _expand_box(
        (x0, y0, x1, y1), W, H,
        pad_x=pad_x, pad_top=pad_y, pad_bottom=pad_bottom,
        pct_x=pct_x, pct_top=pct_top, pct_bottom=pct_bottom,
    )

    persam_done = False
    pmask = None
    meta = {'box': [x0, y0, x1, y1], 'fill_box': [fx0, fy0, fx1, fy1]}

    if use_persam and mode == 'iopaint':
        if not ensure_runtime_loaded():
            meta['persam'] = False
            meta['persam_error'] = 'runtime 未就绪，已降级矩形 mask'
        else:
            try:
                from persam_engine import PerSamEngine
                persam = PerSamEngine.instance()
                ref_img = persam_ref_image if persam_ref_image is not None else img_bgr
                ref_box = persam_ref_box if persam_ref_box is not None else (x0, y0, x1, y1)
                key = persam_key or f'inline_{uuid.uuid4().hex[:8]}'
                persam.encode_reference(key, ref_img, ref_box)
                pmask = persam.segment(
                    key, img_bgr,
                    hint_box=(fx0, fy0, fx1, fy1),
                    hint_margin=6, hint_margin_pct=0.5,
                )
                if pmask is not None and pmask.any():
                    persam_done = True
                    meta['persam'] = True
                    ys, xs = np.nonzero(pmask)
                    meta['persam_box'] = [int(xs.min()), int(ys.min()),
                                          int(xs.max()) + 1, int(ys.max()) + 1]
                else:
                    meta['persam'] = False
                    meta['persam_error'] = '空 mask，降级矩形'
            except Exception as e:
                meta['persam'] = False
                meta['persam_error'] = str(e)

    if mode == 'fill':
        img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        final_color = color
        if not final_color:
            final_color = _sample_surround_color(img_rgb, (x0, y0, x1, y1))
        if not final_color:
            final_color = (255, 255, 255)
        final_color = tuple(int(c) for c in final_color)
        out_rgb, _ = _fill_color(
            img_rgb, (x0, y0, x1, y1),
            pad_x=pad_x, pad_top=pad_y, pad_bottom=pad_bottom,
            pct_x=pct_x, pct_top=pct_top, pct_bottom=pct_bottom,
            color=final_color,
        )
        out_bgr = cv2.cvtColor(out_rgb, cv2.COLOR_RGB2BGR)
        meta['color'] = list(final_color)
        return out_bgr, meta

    # mode = iopaint
    mask = np.zeros((H, W), dtype=np.uint8)
    if persam_done and pmask is not None:
        mask[pmask] = 255
    else:
        mask[fy0:fy1, fx0:fx1] = 255

    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as ftmp:
        tmp_in = Path(ftmp.name)
    try:
        cv2.imwrite(str(tmp_in), img_bgr)
        with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as fout:
            tmp_out = Path(fout.name)
        _run_iopaint(tmp_in, mask, tmp_out)
        out_bgr = cv2.imread(str(tmp_out))
        return out_bgr, meta
    finally:
        try: tmp_in.unlink(missing_ok=True)
        except Exception: pass
        try: tmp_out.unlink(missing_ok=True)
        except Exception: pass


@app.post('/api/single-erase')
def single_erase():
    """单图速擦：用户在上传的图上框一次 → 直接擦除返回。

    参数:
      image_id: str    已通过 /api/upload-template 上传的图 id
      box: [x0,y0,x1,y1]
      mode: 'fill' | 'iopaint'
      use_persam: bool
      pad_x, pad_y, pad_bottom, pct_x, pct_top, pct_bottom: 同 process
    """
    data = request.get_json(force=True)
    image_id = data.get('image_id')
    box = data.get('box')
    if not image_id or not box or len(box) != 4:
        return jsonify(error='参数错误'), 400
    src = UPLOADS / image_id
    if not src.exists():
        return jsonify(error='图不存在'), 404

    img_bgr = cv2.imread(str(src))
    if img_bgr is None:
        return jsonify(error='图读取失败'), 500

    try:
        out_bgr, meta = _erase_one(
            img_bgr, box,
            mode=data.get('mode', 'fill'),
            use_persam=bool(data.get('use_persam', False)),
            pad_x=int(data.get('pad_x', 6)),
            pad_y=int(data.get('pad_y', 6)),
            pad_bottom=int(data.get('pad_bottom', 0)),
            pct_x=float(data.get('pct_x', 0.05)),
            pct_top=float(data.get('pct_top', 0.05)),
            pct_bottom=float(data.get('pct_bottom', 0.0)),
        )
    except Exception as e:
        return jsonify(error=str(e)), 500

    # 单图结果落到 OUTPUTS/single/<uid>.jpg
    out_dir = OUTPUTS / 'single'
    out_dir.mkdir(exist_ok=True)
    out_name = f'{uuid.uuid4().hex[:10]}.jpg'
    Image.fromarray(cv2.cvtColor(out_bgr, cv2.COLOR_BGR2RGB)).save(
        out_dir / out_name, 'JPEG', quality=95
    )

    return jsonify(
        url=f'/api/result/single/{out_name}',
        meta=meta,
    )


@app.post('/api/manual-erase')
def manual_erase():
    """手动修复：对一个 batch 里某张未匹配的图重新跑擦除。

    参数:
      batch_id: str
      file: str        batch 目录下的文件名（来自 process 返回的 it.file）
      box: [x0,y0,x1,y1]
      mode, use_persam, pad_* 同上
    """
    data = request.get_json(force=True)
    batch_id = data.get('batch_id')
    filename = data.get('file')
    box = data.get('box')
    if not batch_id or not filename or not box or len(box) != 4:
        return jsonify(error='参数错误'), 400
    bdir = UPLOADS / f'batch_{batch_id}'
    src = bdir / filename
    if not src.exists():
        return jsonify(error='源图不存在'), 404

    img_bgr = cv2.imread(str(src))
    if img_bgr is None:
        return jsonify(error='图读取失败'), 500

    try:
        out_bgr, meta = _erase_one(
            img_bgr, box,
            mode=data.get('mode', 'fill'),
            use_persam=bool(data.get('use_persam', False)),
            pad_x=int(data.get('pad_x', 6)),
            pad_y=int(data.get('pad_y', 6)),
            pad_bottom=int(data.get('pad_bottom', 0)),
            pct_x=float(data.get('pct_x', 0.05)),
            pct_top=float(data.get('pct_top', 0.05)),
            pct_bottom=float(data.get('pct_bottom', 0.0)),
        )
    except Exception as e:
        return jsonify(error=str(e)), 500

    odir = OUTPUTS / f'batch_{batch_id}'
    odir.mkdir(exist_ok=True)
    stem = Path(filename).stem
    out_name = f'{stem}_clean.jpg'
    Image.fromarray(cv2.cvtColor(out_bgr, cv2.COLOR_BGR2RGB)).save(
        odir / out_name, 'JPEG', quality=95
    )
    return jsonify(
        status='ok',
        file=filename,
        output=out_name,
        url=f'/api/result/{batch_id}/{out_name}',
        meta=meta,
    )


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=7860)
    args = p.parse_args()
    print(f'Logo Eraser → http://{args.host}:{args.port}')
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
