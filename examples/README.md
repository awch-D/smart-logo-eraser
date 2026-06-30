# 内置示例素材

这两组示例都是**纯代码合成**（用 `scripts/make_examples.py` 生成），不依赖任何外部图像，发布到 GitHub 不存在版权风险。

如果新克隆仓库后这个目录是空的，可以重新生成：

```bash
python scripts/make_examples.py
```

## stock4u/ — 电商产品图 · STOCK4U

```
template.png         # 样图（一张白底产品图，右下角带 STOCK4U 水印）
template.box.json    # 该样图上水印的精确 box（程序生成时记录）
batch/01.png         # 批量目标图 1（不同主色）
batch/02.png         # 批量目标图 2（不同主色）
batch/03.png         # 批量目标图 3（不同主色）
```

水印位置都在右下角，体量大小一致，最适合演示「样图框一次 → 批量复用模板」的核心流程。

## photomark/ — 风光摄影 · © PHOTOMARK

```
template.png
template.box.json
batch/01.png  02.png  03.png    # 不同配色的风景图
```

水印在顶部居中，文本带 `©` 符号，适合演示「文字水印 + IOPaint 修复」配合「高精度模式 PerSAM-F」的效果。

## 在桌面应用里一键加载

启动应用 → ④ 批量处理卡片顶部的「示例素材」下拉里选一组 → 点「试试示例素材」即可：

- 自动把 `template.png` 框选好的 logo 加入模板库
- 自动把 `batch/` 三张图作为待处理批量上传

直接点「开始批量擦除」就能看到效果。
