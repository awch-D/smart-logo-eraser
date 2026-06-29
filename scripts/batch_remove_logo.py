#!/usr/bin/env python3
"""
batch_remove_logo.py — 批量去 BOZZ logo 水印

定位策略：
  1. cv2 模板匹配（多尺度），自动找到每张图里 logo 的精确位置
  2. 周围采样真实白色（中位值），用该颜色覆盖 logo 区域
  3. 失败兜底：调用 IOPaint LaMa 用 mask 修复

用法：
  python batch_remove_logo.py <input>            # 单图，输出 <input>_clean.jpg
  python batch_remove_logo.py <input_dir> [output_dir]
  python batch_remove_logo.py ... --mode iopaint # 走 LaMa 而不是白色填充
  python batch_remove_logo.py ... --min-score 0.6 # 匹配阈值，默认 0.7
  python batch_remove_logo.py ... --pad-x 8 --pad-y 6 --pad-bottom 0
"""
import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

TEMPLATE_PATH = Path(__file__).parent / 'logo_template.png'


def find_logo(img_bgr: np.ndarray, template_bgr: np.ndarray, min_score: float = 0.7):
    """多尺度模板匹配，返回 (x0, y0, x1, y1, score) 或 None"""
    img_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    tpl_gray = cv2.cvtColor(template_bgr, cv2.COLOR_BGR2GRAY)
    th, tw = tpl_gray.shape

    best = None
    # 多尺度：0.7x ~ 1.5x，覆盖不同分辨率
    for scale in np.linspace(0.7, 1.5, 17):
        new_w, new_h = int(tw * scale), int(th * scale)
        if new_w < 20 or new_h < 20 or new_w > img_gray.shape[1] or new_h > img_gray.shape[0]:
            continue
        tpl_s = cv2.resize(tpl_gray, (new_w, new_h), interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(img_gray, tpl_s, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if best is None or max_val > best[0]:
            best = (max_val, max_loc, new_w, new_h, scale)

    if best is None or best[0] < min_score:
        return None
    score, (x, y), w, h, scale = best
    return (x, y, x + w, y + h, score, scale)


def sample_surround_color(img_rgb: np.ndarray, box, band: int = 14):
    """从 logo 边界外 band 像素的环带采样中位色"""
    x0, y0, x1, y1 = box
    H, W, _ = img_rgb.shape
    sx0 = max(0, x0 - band)
    sy0 = max(0, y0 - band)
    sx1 = min(W, x1 + band)
    sy1 = min(H, y1 + band)
    region = img_rgb[sy0:sy1, sx0:sx1].copy()
    # 把 logo 内部挖掉
    inner_y0 = y0 - sy0
    inner_y1 = y1 - sy0
    inner_x0 = x0 - sx0
    inner_x1 = x1 - sx0
    mask = np.ones(region.shape[:2], dtype=bool)
    mask[inner_y0:inner_y1, inner_x0:inner_x1] = False
    surround = region[mask]
    if len(surround) == 0:
        return None
    # 偏好高亮度像素（认为白底），避免把屏幕黑边或屏幕画面采进去
    luma = surround.mean(axis=1)
    bright = surround[luma > luma.mean()]
    pool = bright if len(bright) > 50 else surround
    return tuple(int(c) for c in np.median(pool, axis=0))


def fill_white(img_rgb: np.ndarray, box, pad_x=4, pad_top=4, pad_bottom=-9, color=None):
    """用纯色覆盖 box（含 padding）"""
    H, W, _ = img_rgb.shape
    x0, y0, x1, y1 = box
    fx0 = max(0, x0 - pad_x)
    fy0 = max(0, y0 - pad_top)
    fx1 = min(W, x1 + pad_x)
    fy1 = min(H, y1 + pad_bottom)  # 默认 -9: 底部收紧，避免触屏幕黑边
    if fy1 <= fy0:
        fy1 = y1
    if color is None:
        color = sample_surround_color(img_rgb, (fx0, fy0, fx1, fy1))
        if color is None:
            color = (255, 255, 255)
    out = img_rgb.copy()
    out[fy0:fy1, fx0:fx1] = color
    return out, (fx0, fy0, fx1, fy1), color


def make_mask(shape, fill_box):
    H, W = shape
    mask = np.zeros((H, W), dtype=np.uint8)
    fx0, fy0, fx1, fy1 = fill_box
    mask[fy0:fy1, fx0:fx1] = 255
    return mask


def process_one(path: Path, out_path: Path, template_bgr, args):
    img_bgr = cv2.imread(str(path))
    if img_bgr is None:
        print(f'  ✗ 读取失败: {path}')
        return False
    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

    found = find_logo(img_bgr, template_bgr, min_score=args.min_score)
    if not found:
        print(f'  ✗ 未匹配到 logo: {path.name}')
        return False
    x0, y0, x1, y1, score, scale = found
    print(f'  ✓ {path.name}: logo @ ({x0},{y0})-({x1},{y1}) score={score:.3f} scale={scale:.2f}')

    if args.mode == 'fill':
        result, fill_box, color = fill_white(
            img_rgb, (x0, y0, x1, y1),
            pad_x=args.pad_x, pad_top=args.pad_y, pad_bottom=args.pad_bottom,
        )
        Image.fromarray(result).save(out_path, 'JPEG', quality=95)
        print(f'    填充 RGB{color} 区域 {fill_box} → {out_path}')
    else:
        # iopaint 模式：生成 mask 并调用 LaMa
        import subprocess, tempfile, shutil
        H, W = img_rgb.shape[:2]
        fill_box = (
            max(0, x0 - args.pad_x),
            max(0, y0 - args.pad_y),
            min(W, x1 + args.pad_x),
            min(H, y1 + args.pad_bottom),
        )
        mask = make_mask((H, W), fill_box)

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / 'in').mkdir()
            (tdp / 'out').mkdir()
            shutil.copy(path, tdp / 'in' / path.name)
            cv2.imwrite(str(tdp / 'mask.png'), mask)
            iopaint_bin = Path(__file__).parent / '.venv/bin/iopaint'
            cmd = [
                str(iopaint_bin), 'run',
                '--model=lama', '--device=mps',
                f'--image={tdp / "in"}',
                f'--mask={tdp / "mask.png"}',
                f'--output={tdp / "out"}',
            ]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode != 0:
                print(f'    IOPaint 失败: {r.stderr[-200:]}')
                return False
            # 输出文件是 .png，转为目标格式
            png_out = next((tdp / 'out').glob('*.png'))
            Image.open(png_out).convert('RGB').save(out_path, 'JPEG', quality=95)
            print(f'    LaMa 修复 → {out_path}')
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input', help='单图路径或目录')
    ap.add_argument('output', nargs='?', help='输出目录（输入为目录时必需）')
    ap.add_argument('--mode', choices=['fill', 'iopaint'], default='fill',
                    help='fill = 周围色覆盖（快，纯白底场景最优）；iopaint = LaMa 修复（兜底）')
    ap.add_argument('--template', default=str(TEMPLATE_PATH), help='logo 模板图')
    ap.add_argument('--min-score', type=float, default=0.7, help='模板匹配阈值 0~1')
    ap.add_argument('--pad-x', type=int, default=4, help='左右 padding')
    ap.add_argument('--pad-y', type=int, default=4, help='上方 padding')
    ap.add_argument('--pad-bottom', type=int, default=-9, help='下方 padding（负数=收缩）')
    args = ap.parse_args()

    tpl = cv2.imread(args.template)
    if tpl is None:
        print(f'模板读取失败: {args.template}')
        sys.exit(1)

    inp = Path(args.input)
    if inp.is_file():
        out = Path(args.output) if args.output else inp.with_name(inp.stem + '_clean.jpg')
        if out.is_dir():
            out = out / (inp.stem + '_clean.jpg')
        ok = process_one(inp, out, tpl, args)
        sys.exit(0 if ok else 1)

    if not inp.is_dir():
        print(f'输入路径无效: {inp}')
        sys.exit(1)
    if not args.output:
        print('输入为目录时必须指定输出目录')
        sys.exit(1)
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    exts = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
    files = sorted([p for p in inp.iterdir() if p.suffix.lower() in exts])
    print(f'共找到 {len(files)} 张图片')
    ok_count = 0
    for p in files:
        out = out_dir / (p.stem + '_clean.jpg')
        if process_one(p, out, tpl, args):
            ok_count += 1
    print(f'\n完成: {ok_count}/{len(files)}')


if __name__ == '__main__':
    main()
