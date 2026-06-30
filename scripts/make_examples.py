"""生成内置示例素材：纯 PIL 合成的"带水印图"。

用法：
    python scripts/make_examples.py            # 输出到仓库根目录 examples/
    python scripts/make_examples.py --out /tmp/examples

所有图都由代码合成（几何渐变背景 + 程序化纹理 + 半透明文字水印），
不依赖任何外部素材，发布到 GitHub 不存在版权风险。

输出布局：
    examples/
        stock4u/
            meta.json
            template.png              # 含 STOCK4U 水印的样图
            template.box.json         # 该样图上水印的精确 box
            batch/
                01.png  02.png  03.png
        photomark/
            meta.json
            template.png
            template.box.json
            batch/
                01.png  02.png  03.png
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont


def find_font(preferred: list[str], size: int) -> ImageFont.FreeTypeFont:
    """按顺序尝试常见系统字体；都找不到就用 default。"""
    candidates = preferred + [
        '/System/Library/Fonts/Supplemental/Arial Bold.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
        '/System/Library/Fonts/SFNS.ttf',
        'C:/Windows/Fonts/arialbd.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


# ---------- 背景生成 ----------

def gradient_bg(size: tuple[int, int], top: tuple[int, int, int],
                bottom: tuple[int, int, int]) -> Image.Image:
    w, h = size
    img = Image.new('RGB', size, top)
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] * (1 - t) + bottom[0] * t)
        g = int(top[1] * (1 - t) + bottom[1] * t)
        b = int(top[2] * (1 - t) + bottom[2] * t)
        ImageDraw.Draw(img).line([(0, y), (w, y)], fill=(r, g, b))
    return img


def speckle(img: Image.Image, density: float = 0.005, alpha: int = 30) -> Image.Image:
    """加一层细小噪点，让图看起来不那么"AI 平面"。"""
    rng = random.Random(0xC0FFEE)
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    count = int(img.size[0] * img.size[1] * density)
    for _ in range(count):
        x = rng.randrange(img.size[0])
        y = rng.randrange(img.size[1])
        v = rng.randint(180, 240)
        d.point((x, y), fill=(v, v, v, alpha))
    return Image.alpha_composite(img.convert('RGBA'), overlay).convert('RGB')


# ---------- 场景 1：电商产品图 ----------

def product_card(size: tuple[int, int], hue: tuple[int, int, int],
                 label: str, seed: int) -> Image.Image:
    """模拟电商产品白底图：渐变背景 + 居中"产品"形状。"""
    rng = random.Random(seed)
    img = gradient_bg(size, (250, 250, 252), (228, 232, 240))
    img = speckle(img, density=0.003, alpha=20)
    d = ImageDraw.Draw(img, 'RGBA')

    w, h = size
    # 主体阴影
    cx, cy = w // 2, int(h * 0.55)
    rx, ry = int(w * 0.32), int(h * 0.32)
    d.ellipse((cx - rx, cy + int(ry * 0.7), cx + rx, cy + int(ry * 0.85)),
              fill=(0, 0, 0, 60))

    # 主体（圆角矩形）
    bw, bh = int(w * 0.42), int(h * 0.58)
    bx0 = cx - bw // 2
    by0 = cy - int(bh * 0.55)
    bx1 = bx0 + bw
    by1 = by0 + bh
    d.rounded_rectangle((bx0, by0, bx1, by1), radius=int(bw * 0.08),
                        fill=hue, outline=(0, 0, 0, 40), width=2)

    # 顶部高光
    d.rounded_rectangle((bx0 + 6, by0 + 6, bx1 - 6, by0 + int(bh * 0.25)),
                        radius=int(bw * 0.06), fill=(255, 255, 255, 50))

    # 标签
    font = find_font([], int(min(w, h) * 0.05))
    bbox = d.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    d.text(((w - tw) // 2, by1 - th - int(bh * 0.15)), label, font=font,
           fill=(255, 255, 255, 220))

    # 角标
    font_sm = find_font([], int(min(w, h) * 0.025))
    d.text((24, 24), 'NEW ARRIVAL', font=font_sm, fill=(80, 90, 110, 180))
    d.text((w - 100, 24), f'SKU-{rng.randint(1000, 9999)}', font=font_sm,
           fill=(80, 90, 110, 180))
    return img


# ---------- 场景 2：风景/摄影 ----------

def landscape_card(size: tuple[int, int], palette: list[tuple[int, int, int]],
                   seed: int) -> Image.Image:
    """模拟风光摄影：分段渐变天空 + 山形剪影 + 颗粒。"""
    rng = random.Random(seed)
    w, h = size
    # 天空（上半）
    sky = gradient_bg((w, int(h * 0.6)), palette[0], palette[1])
    img = Image.new('RGB', size, palette[2])
    img.paste(sky, (0, 0))
    img = speckle(img, density=0.004, alpha=28)
    d = ImageDraw.Draw(img, 'RGBA')

    # 远山 + 近山
    far_color = (palette[3][0], palette[3][1], palette[3][2], 200)
    near_color = (palette[4][0], palette[4][1], palette[4][2], 235)
    # 远山线
    pts_far = [(0, int(h * 0.55))]
    x = 0
    while x < w:
        x += rng.randint(40, 100)
        y = int(h * 0.55) + rng.randint(-int(h * 0.05), int(h * 0.05))
        pts_far.append((x, y))
    pts_far.append((w, int(h * 0.55)))
    pts_far += [(w, h), (0, h)]
    d.polygon(pts_far, fill=far_color)
    # 近山线
    pts_near = [(0, int(h * 0.7))]
    x = 0
    while x < w:
        x += rng.randint(60, 140)
        y = int(h * 0.7) + rng.randint(-int(h * 0.04), int(h * 0.08))
        pts_near.append((x, y))
    pts_near.append((w, int(h * 0.7)))
    pts_near += [(w, h), (0, h)]
    d.polygon(pts_near, fill=near_color)

    # 太阳
    sx, sy = int(w * 0.78), int(h * 0.22)
    sr = int(min(w, h) * 0.07)
    d.ellipse((sx - sr, sy - sr, sx + sr, sy + sr),
              fill=(255, 240, 200, 220))
    return img.filter(ImageFilter.GaussianBlur(radius=0.6))


# ---------- 水印贴图 ----------

def add_watermark(
    img: Image.Image, text: str,
    position: str = 'bottom-right',  # bottom-right / center / top-center
    color: tuple[int, int, int, int] = (255, 255, 255, 200),
    size_pct: float = 0.06,
    margin_pct: float = 0.05,
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """在 img 上叠加文字水印，返回新图 + 水印 bbox（图像坐标）。"""
    w, h = img.size
    font = find_font([], int(min(w, h) * size_pct))
    txt_layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(txt_layer)
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    mx = int(w * margin_pct)
    my = int(h * margin_pct)
    if position == 'bottom-right':
        x0 = w - tw - mx
        y0 = h - th - my
    elif position == 'top-center':
        x0 = (w - tw) // 2
        y0 = my
    else:  # center
        x0 = (w - tw) // 2
        y0 = (h - th) // 2
    # 加柔和阴影
    d.text((x0 + 2, y0 + 2), text, font=font, fill=(0, 0, 0, 160))
    d.text((x0, y0), text, font=font, fill=color)
    merged = Image.alpha_composite(img.convert('RGBA'), txt_layer).convert('RGB')
    # 校正实际 bbox（textbbox 返回的 bbox y 不是从 0 开始）
    real = (x0 + bbox[0], y0 + bbox[1], x0 + bbox[2], y0 + bbox[3])
    return merged, real


# ---------- 单组示例编排 ----------

def build_stock4u(out_dir: Path):
    """场景 1：电商产品图 + STOCK4U 水印（右下角）。"""
    group = out_dir / 'stock4u'
    (group / 'batch').mkdir(parents=True, exist_ok=True)
    # 样图：放在右下角
    base = product_card((900, 700), (96, 130, 220), 'BUNDLE', seed=11)
    tpl, box = add_watermark(base, 'STOCK4U', position='bottom-right',
                              size_pct=0.07)
    tpl.save(group / 'template.png', 'PNG')
    (group / 'template.box.json').write_text(
        json.dumps({'box': list(box)}, indent=2), encoding='utf-8'
    )
    # 批量 3 张：不同主色 + 不同标签
    products = [
        ((220, 96, 110), 'PREMIUM', 21),
        ((100, 170, 130), 'CLASSIC', 22),
        ((230, 168, 80), 'LIMITED', 23),
    ]
    for i, (hue, label, seed) in enumerate(products, 1):
        img = product_card((900, 700), hue, label, seed=seed)
        img, _ = add_watermark(img, 'STOCK4U', position='bottom-right',
                               size_pct=0.07)
        img.save(group / 'batch' / f'{i:02d}.png', 'PNG')
    (group / 'meta.json').write_text(json.dumps({
        'name': '电商产品图 · STOCK4U',
        'watermark_text': 'STOCK4U',
        'template': 'template.png',
        'description': '4 张白底产品图，右下角带 STOCK4U 水印，演示模板复用 + 自动采样色。',
    }, ensure_ascii=False, indent=2), encoding='utf-8')


def build_photomark(out_dir: Path):
    """场景 2：风光摄影 + PHOTOMARK 水印（顶部居中）。"""
    group = out_dir / 'photomark'
    (group / 'batch').mkdir(parents=True, exist_ok=True)
    # 三套天空配色
    palettes = [
        [(255, 200, 140), (255, 130, 90), (240, 100, 70),  (90, 60, 90),  (50, 30, 60)],
        [(160, 200, 250), (90, 130, 200), (70, 90, 160),  (60, 80, 120), (30, 40, 70)],
        [(255, 180, 220), (180, 130, 220), (110, 80, 180), (80, 60, 130), (40, 30, 80)],
        [(255, 230, 180), (240, 180, 130), (200, 140, 100),(120, 90, 80), (60, 50, 50)],
    ]
    # 样图
    base = landscape_card((1024, 640), palettes[0], seed=51)
    tpl, box = add_watermark(base, '© PHOTOMARK', position='top-center',
                              size_pct=0.05)
    tpl.save(group / 'template.png', 'PNG')
    (group / 'template.box.json').write_text(
        json.dumps({'box': list(box)}, indent=2), encoding='utf-8'
    )
    # 批量
    for i, pal in enumerate(palettes[1:], 1):
        img = landscape_card((1024, 640), pal, seed=50 + i * 3)
        img, _ = add_watermark(img, '© PHOTOMARK', position='top-center',
                               size_pct=0.05)
        img.save(group / 'batch' / f'{i:02d}.png', 'PNG')
    (group / 'meta.json').write_text(json.dumps({
        'name': '风光摄影 · PHOTOMARK',
        'watermark_text': '© PHOTOMARK',
        'template': 'template.png',
        'description': '4 张程序化风景图，顶部居中带 © PHOTOMARK 水印，演示文字水印 + IOPaint 修复。',
    }, ensure_ascii=False, indent=2), encoding='utf-8')


# ---------- 主入口 ----------

def main():
    ap = argparse.ArgumentParser(description='生成 Smart Logo Eraser 示例素材')
    ap.add_argument('--out', default=None,
                    help='输出目录（默认：仓库根 examples/）')
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    out = Path(args.out) if args.out else (here.parent / 'examples')
    out.mkdir(parents=True, exist_ok=True)

    print(f'→ stock4u  ({out / "stock4u"})')
    build_stock4u(out)
    print(f'→ photomark ({out / "photomark"})')
    build_photomark(out)
    print('✓ done')


if __name__ == '__main__':
    main()
