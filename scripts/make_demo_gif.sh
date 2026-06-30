#!/usr/bin/env bash
# Demo 录制/转 GIF 工具集（macOS）— 服务于 P0-3
#
# 用法：
#   bash scripts/make_demo_gif.sh record       # 启动 30s 屏幕录制（用 ffmpeg 抓 macOS 桌面）
#   bash scripts/make_demo_gif.sh trim input.mov 5 30   # 从第 5s 起截取 30s 片段
#   bash scripts/make_demo_gif.sh gif demo.mov         # 用 gifski 转高质量 GIF（推荐）
#   bash scripts/make_demo_gif.sh gif-ffmpeg demo.mov  # 用 ffmpeg 转 GIF（无 gifski 时兜底）
#
# 建议流程：
#   1. 准备工具：brew install ffmpeg gifski
#   2. 启动桌面应用：cd app && python desktop.py
#   3. 按下面录制脚本里的 30s 节奏录屏（推荐手动按 Cmd+Shift+5 选窗口录）
#   4. 用 trim 截到节奏卡得最准的 30s
#   5. gif 转 800px / <5MB / 1.5x 加速
#
# 录制建议节奏（30s 总时长，参考 ROADMAP P0-3）：
#   0-3s   打开应用，主界面
#   3-7s   切到「单图速擦」Tab → 上传示例图
#   7-12s  在图上框选 logo → 点「一键擦除」→ 显示对比
#   12-15s 切回「批量擦除」
#   15-20s 点「试试示例素材」→ 加载完成
#   20-25s 点「开始批量擦除」→ 结果网格依次出现
#   25-30s 点开一张结果放大查看（lightbox）
#
# 提示：录屏前关闭 Dock + 顶部状态栏的多余图标（系统设置 → 控制中心），
# 录窗口模式避免抓到桌面壁纸。

set -e

ACTION="${1:-help}"
OUT_DIR="assets"
mkdir -p "$OUT_DIR"

case "$ACTION" in
  record)
    # 30s 桌面录屏（ffmpeg avfoundation）。1 = 主屏 (M1 / M2 Mac 默认)
    OUT="${2:-$OUT_DIR/demo_raw.mov}"
    echo "→ 录屏 30s 到 $OUT（按 q 提前结束）"
    ffmpeg -hide_banner -f avfoundation -framerate 30 -i "1:none" \
           -t 30 -c:v libx264 -pix_fmt yuv420p -y "$OUT"
    echo "✓ 录制完成：$OUT"
    ;;

  trim)
    SRC="$2"; START="${3:-0}"; DUR="${4:-30}"
    OUT="${5:-$OUT_DIR/demo_trim.mov}"
    [ -z "$SRC" ] && { echo "用法: $0 trim <src.mov> [start_sec] [duration] [out]"; exit 1; }
    ffmpeg -hide_banner -ss "$START" -i "$SRC" -t "$DUR" -c copy -y "$OUT"
    echo "✓ 截取完成：$OUT"
    ;;

  gif)
    # 推荐：gifski 抖动算法 + 自适应 palette，效果远好于 ffmpeg
    SRC="$2"; OUT="${3:-$OUT_DIR/demo.gif}"
    [ -z "$SRC" ] && { echo "用法: $0 gif <src.mov> [out.gif]"; exit 1; }
    if ! command -v gifski >/dev/null; then
      echo "需要 gifski：brew install gifski"; exit 1
    fi
    # 1.5x 加速 → 800px 宽 → 20fps → gifski
    TMP="$(mktemp -d)/scaled.mp4"
    ffmpeg -hide_banner -i "$SRC" \
           -filter:v "setpts=PTS/1.5,scale=800:-2,fps=20" \
           -c:v libx264 -pix_fmt yuv420p -y "$TMP"
    gifski --width 800 --fps 20 --quality 90 -o "$OUT" "$TMP"
    rm -f "$TMP"
    echo "✓ GIF 完成：$OUT"
    ls -lh "$OUT"
    echo "→ 如果体积 > 5MB，把 --quality 降到 80 或 fps 降到 15。"
    ;;

  gif-ffmpeg)
    # 无 gifski 时的兜底：两遍 palette
    SRC="$2"; OUT="${3:-$OUT_DIR/demo.gif}"
    [ -z "$SRC" ] && { echo "用法: $0 gif-ffmpeg <src.mov> [out.gif]"; exit 1; }
    PALETTE="$(mktemp -t demo_palette).png"
    ffmpeg -hide_banner -i "$SRC" \
           -filter:v "setpts=PTS/1.5,scale=800:-2,fps=15,palettegen=stats_mode=diff" \
           -y "$PALETTE"
    ffmpeg -hide_banner -i "$SRC" -i "$PALETTE" \
           -filter_complex "setpts=PTS/1.5,scale=800:-2,fps=15[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4" \
           -y "$OUT"
    rm -f "$PALETTE"
    echo "✓ GIF 完成：$OUT"
    ls -lh "$OUT"
    ;;

  *)
    head -n 35 "$0" | tail -n 34
    ;;
esac
