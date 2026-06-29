"""Logo Eraser Web Tool — 本地调参 + 批处理工具

接口（v2，支持模板库）：
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
from flask import Flask, jsonify, request, send_file, send_from_directory
from PIL import Image

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


def _fill_color(img_rgb, box, pad_x=4, pad_top=4, pad_bottom=-9,
                pct_x=0.0, pct_top=0.0, pct_bottom=0.0,
                color=(255, 255, 255)):
    H, W, _ = img_rgb.shape
    fb = _expand_box(box, W, H, pad_x, pad_top, pad_bottom,
                     pct_x, pct_top, pct_bottom)
    out = img_rgb.copy()
    fx0, fy0, fx1, fy1 = fb
    out[fy0:fy1, fx0:fx1] = color
    return out, fb


def _run_iopaint(img_path: Path, mask: np.ndarray, out_path: Path):
    if IOPAINT_BIN is None or not Path(IOPAINT_BIN).exists():
        raise RuntimeError('未安装 IOPaint（仅支持颜色填充模式）')
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / 'in').mkdir()
        (tdp / 'out').mkdir()
        shutil.copy(img_path, tdp / 'in' / img_path.name)
        cv2.imwrite(str(tdp / 'mask.png'), mask)
        cmd = [
            str(IOPAINT_BIN), 'run',
            '--model=lama', '--device=mps',
            f'--image={tdp / "in"}',
            f'--mask={tdp / "mask.png"}',
            f'--output={tdp / "out"}',
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-500:])
        png = next((tdp / 'out').glob('*.png'))
        Image.open(png).convert('RGB').save(out_path, 'JPEG', quality=95)


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
    pad_x = int(data.get('pad_x', 4))
    pad_y = int(data.get('pad_y', 4))
    pad_bottom = int(data.get('pad_bottom', -9))
    # 百分比 padding：按匹配到的 logo 实际尺寸自适应膨胀
    pct_x = float(data.get('pct_x', 0.0))
    pct_top = float(data.get('pct_top', 0.0))
    pct_bottom = float(data.get('pct_bottom', 0.0))
    min_score = float(data.get('min_score', 0.7))

    if not template_ids:
        return jsonify(error='至少选择一个模板'), 400

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
                item.update(status='no_match', error='没有任何模板匹配')
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
                    working, fb = _fill_color(
                        working, (x0, y0, x1, y1),
                        pad_x=pad_x, pad_top=pad_y, pad_bottom=pad_bottom,
                        pct_x=pct_x, pct_top=pct_top, pct_bottom=pct_bottom,
                        color=final_color,
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
                    mask[fy0:fy1, fx0:fx1] = 255
                    matches_meta.append({
                        'template_id': tpl_entry['id'],
                        'template_name': tpl_entry['name'],
                        'box': [x0, y0, x1, y1],
                        'score': round(score, 3),
                        'scale': round(scale, 2),
                        'fill_box': [fx0, fy0, fx1, fy1],
                    })
                _run_iopaint(img_path, mask, out_path)
                item['fill_box'] = matches_meta[0]['fill_box']

            item['matches'] = matches_meta
            item.update(status='ok', output=out_name,
                        url=f'/api/result/{batch_id}/{out_name}')
        except Exception as e:
            item.update(status='error', error=str(e))
        results.append(item)

    return jsonify(batch_id=batch_id, results=results)


@app.get('/api/result/<batch_id>/<filename>')
def result_file(batch_id, filename):
    return send_from_directory(OUTPUTS / f'batch_{batch_id}', filename)


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=7860)
    args = p.parse_args()
    print(f'Logo Eraser → http://{args.host}:{args.port}')
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
