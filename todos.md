# Smart Logo Eraser — Todos

> 本清单从 [ROADMAP.md](./ROADMAP.md) + 一次完整代码审计拆解而来，每条都是可执行的最小任务。
> 完成一项就把 `- [ ]` 改成 `- [x]`，commit 时附上对应任务号。
>
> **元规则**：每完成 1–2 项后回头看清单是否还合理。需求会随迭代演化，不强求按序走完。
>
> **背景**：上一轮审计发现「todos 打勾 ≠ 真的能跑」——P0-1 按需下载、P0-2 示例素材、P0-5 手动修复在 packaged build 里实际是死的。所以新增 FIX 阶段，必须先清完才能继续 P0 验收。

---

## 🔥 FIX — 修复 Sprint（优先级最高，必须先全清）

### F1 阻塞级 bug（packaged build 不可用）

#### F1-1 PyInstaller 包内 runtime 下载失败（B1）

- **症状**：`app/runtime.py:294 _pip_install_stream` 用 `sys.executable -m pip`，frozen 后 sys.executable 是 .app/.exe，不能 -m pip
- [ ] 决策下载策略（A：随包附带 pip wheel + 嵌入式 python；B：直接下 platform wheel 用 zipfile 解压；C：随包附 portable python）
- [ ] 实现选定方案，写 `_install_runtime_v2()` 替换 `_pip_install_stream`
- [ ] dev 环境用 PyInstaller --onedir 打一版，模拟干净机器执行真实下载
- [ ] 失败重试 / 断点续传 / .part 清理 路径都覆盖
- [ ] 把 marker.json 写入移到下载完全成功后

#### F1-2 iopaint subprocess 看不到 runtime 里的 iopaint（B2）

- **症状**：`app/server.py:228 _run_iopaint` 子进程没收到 PYTHONPATH，runtime/site-packages 里的 iopaint 形同虚设
- [ ] `_run_iopaint` 调用 subprocess.run 时注入 `env={'PYTHONPATH': str(SITE_PACKAGES), **os.environ}`
- [ ] 或者落地 `runtime/bin/iopaint` 入口脚本（带 sys.path 修正）
- [ ] 验证：临时屏蔽 .venv/bin/iopaint，仅靠 runtime 能跑通

#### F1-3 手动定位按钮永远 disabled（B3）

- **症状**：`app/static/app.js openManualPanel()` 没调 `setMode('manual')`，`notifyBoxChanged` 的启用分支命不中 → P0-5 UI 入口完全无效
- [ ] `openManualPanel()` 内置 `state.mode = 'manual'`
- [ ] `closeManualPanel()` 恢复 `state.mode = 'batch'`
- [ ] `setMode()` 增加 'manual' 分支（不切 tab，仅隔离 Canvas 行为）
- [ ] 端到端测试：批量结果 → 手动定位 → 框选 → 按钮可点 → 擦除成功 → 卡片更新

#### F1-4 examples 没进 PyInstaller 包（B4）

- **症状**：`logo_eraser.spec:21 datas` 漏了 examples；`server.py:662 EXAMPLES_ROOT = ROOT.parent / 'examples'` 打包后指向不存在的 dist 路径
- [ ] spec datas 添加 `('../examples', 'examples')`
- [ ] `EXAMPLES_ROOT = Path(getattr(sys, '_MEIPASS', ROOT.parent)) / 'examples'`
- [ ] 重新打包，确认 dist/LogoEraser/_internal/examples 存在
- [ ] 安装包里点示例下拉，stock4u / photomark 都能加载

---

### F2 严重 bug（dev 环境也受影响）

#### F2-1 iopaint 设备硬编码 mps（B5）

- **症状**：`app/server.py:234` 写死 `--device=mps`，Win / Intel Mac / Linux 用户直接崩
- [ ] 抽 `_pick_iopaint_device()` helper：cuda → cuda；mps → mps；否则 cpu
- [ ] `_run_iopaint` 用 helper 结果
- [ ] cpu 路径加 `--no-half` / 等价参数

#### F2-2 dev_fallback 不检测 iopaint（B6）

- **症状**：dev 环境有 torch + vendor 权重但没装 iopaint，`runtime_status` 仍返回 ready=true，跑 iopaint 模式炸
- [ ] dev_fallback 分支加 `_iopaint_available()` 检查
- [ ] 缺 iopaint 时 state='dev-partial'，missing 含 'iopaint'，前端提示「dev 环境缺 iopaint，pip install iopaint」

#### F2-3 vendor 自带权重未被识别（B7）

- **症状**：vendor/personalize_sam/weights/mobile_sam.pt 已在仓库里，但 `_weight_present()` 只看 `~/.logo_eraser/runtime/weights/`，missing 仍含 mobile_sam → 前端会触发 38MB 重复下载
- [ ] `_weight_present()` 增加 vendor 路径兜底（与 dev_fallback 路径一致）
- [ ] 或者 dev_fallback 命中时跳过 mobile_sam 的 missing 判定

#### F2-4 SHA256 校验是占位（B8）

- **症状**：`runtime.py:78 MOBILE_SAM_SHA256` 是占位字符串，`_download_with_progress` 累加 hash 但从不比对，完整性只靠 `size > 1MB`
- [ ] dev 环境跑一次完整下载，记录真实 sha256 写回常量
- [ ] `_download_with_progress` 启用 sha256 校验
- [ ] 校验失败 → 删 .part → 错误码 `hash_mismatch` + 重试入口

---

### F3 中等 bug（影响体验或安全）

#### F3-1 路径 traversal 风险（B9）

- **症状**：`server.py:967 bdir / filename`、`server.py:719 EXAMPLES_ROOT/group_id` 没做防护，传 `../../sessions/templates.json` 能越界
- [ ] result_file：`filename = Path(filename).name` 砍 dir 部分
- [ ] load-example：group_id 强制白名单 `^[a-z0-9_-]+$`
- [ ] manual-erase / single-erase 的 file 字段同样处理

#### F3-2 并发手动修复互相覆盖（B10, B13）

- **症状**：连续点两张「手动定位」状态相互覆盖；同一图并发 manual-erase 互踩 `{stem}_clean.jpg`
- [ ] openManualPanel 前判断 state.manualContext 是否存在，提示「先完成或取消上一张」
- [ ] manual-erase 输出文件名加 timestamp 后缀，前端 url 用最新结果

#### F3-3 PerSAM 冷启动阻塞（B11）

- **症状**：`persam_engine.py:87` 单例首次加载 1-3s 持 _inst_lock，并发请求全 hang
- [ ] desktop.py 启动后 / runtime ready 时后台 warm-up（线程跑 PerSamEngine.instance()）
- [ ] 加载期间 status API 返回 'warming'

#### F3-4 双击下载并发（B12）

- **症状**：用户双击「开始下载」/前端 retry 没等响应，两条 SSE 同时写 marker.json + pip --target 互踩
- [ ] `install_runtime_stream` 开头 acquire `RUNTIME_ROOT/.install.lock`（文件锁）
- [ ] 已有锁时直接返回「另一个安装进行中」事件
- [ ] 前端 rtStart 点击后立即 disabled，避免双击

#### F3-5 模式切换 state 残留（B14）

- **症状**：单图 ⇄ 批量切换时 state.image 保留，但 imageId 可能指向旧上传，box 落到错误图
- [ ] `setMode()` 在模式真正切换时清 state.image / state.imageId / state.box / canvas 回显
- [ ] 切回时不复用旧图，要求重新上传

#### F3-6 ensure_runtime_loaded 返回 True 但权重缺失（B15）

- **症状**：系统有 torch 但 vendor 权重缺失，`ensure_runtime_loaded()` 仍返回 True，persam_engine 加载时炸
- [ ] 把权重存在性合并到 ensure_runtime_loaded 判定
- [ ] 或允许加载 runtime/weights/mobile_sam.pt 作为 fallback

---

### F4 轻微 bug（清理类，可批量改）

- [ ] **B20**：desktop.py 用 `werkzeug.serving.make_server(..., threaded=True)` 替 `wsgiref.simple_server`，让 SSE 和普通请求能并发
- [ ] **B21**：scripts/batch_remove_logo.py:144 iopaint 路径修正（项目重命名后已错）
- [ ] **B22**：前端 fetch 包一层 helper，区分 4xx/5xx/网络断开，统一错误展示
- [ ] **B23**：persam `_ref_cache` 加 LRU 上限（默认 16 个 ref）
- [ ] **B17**：server.py 清理未使用 import (`io`, `send_file`)
- [ ] **B19**：runtime `_weight_present` 顺手对 .part 残留做清理

---

### FIX 阶段验收

- [ ] 干净 macOS 机器（无系统 torch）：装 dmg → 启动 → 勾高精度 → 完整下载 → 擦除成功
- [ ] 干净 Windows 机器：装 exe → 同样流程
- [ ] dev 环境：手动定位流程能完整跑通（B3）
- [ ] 安装包内能加载 examples 下拉（B4）
- [ ] iopaint 模式在 Intel Mac / Windows 不再硬崩（B5）
- [ ] 双击下载不会写坏 marker（F3-4）

> 📍 **Checkpoint**：FIX 阶段全部完成后再继续 P0-3 录屏 + P0 验收。

---

## P0 — 第 1–2 周：让用户能装上、试上、看见结果

### P0-1 修复打包后高精度模式失效（方案 A：首次启动按需下载）

- [x] 设计 runtime 目录结构（`~/.logo_eraser/runtime/torch/`、`mobile_sam.pt`、`marker.json`）
- [x] 定义 marker.json 协议（版本、下载源、校验值、写入时间）
- [x] 在 `server.py` 启动逻辑加 `ensure_runtime()` 检查
- [x] 后端实现 `/api/runtime/status`
- [x] 后端实现 `/api/runtime/install`（SSE 推送进度）
- [⚠️] runtime 安装依赖清单覆盖完整 —— 装完后 subprocess 无法找到，见 **F1-2**
- [x] 下载完成后写 marker.json，并把 runtime 目录加入 `sys.path`
- [x] 前端：勾"高精度模式"时先 GET 状态；未就绪则弹窗
- [x] 前端：下载弹窗带进度条 / 当前文件 / 速度 / 取消按钮
- [x] 调整 PyInstaller spec：仍 exclude torch + iopaint；改 `--onedir`
- [ ] 在干净 Mac（无 Python torch）上做端到端走查（依赖 **F1-1 / F1-2**）
- [ ] 在干净 Windows 上做同样走查（依赖 **F1-1 / F1-2**）
- [x] 失败兜底：下载中断 / 校验失败 / 磁盘不足
- [ ] 在 GitHub Release 上传 .dmg 和 .exe 产物

**验收**
- [ ] 全新 Mac 安装包 → 不勾用 padding；勾选触发下载 → 擦除成功
- [ ] 二次启动不会重复下载

---

### P0-2 内置 examples/ 示例素材

- [x] 写 `scripts/make_examples.py`
- [x] 场景 1（电商产品图 + STOCK4U 水印）
- [x] 场景 2（摄影作品 + PHOTOMARK 水印）
- [x] 提交 8 张图到 `examples/`
- [⚠️] 桌面应用 UI 加"试试示例素材"按钮（dev 环境 OK，安装包待 **F1-4**）
- [x] `README.md` 加 "5 秒上手" 章节

**验收**
- [ ] 新用户 clone 仓库 → 3 分钟内跑出第一张擦除结果（依赖 **F1-4**）

---

### P0-3 录制 30s Demo GIF / MP4

- [x] 准备录制脚本 → `scripts/make_demo_gif.sh`
- [ ] macOS 屏幕录制 → 看节奏 → 重录优化
- [x] 用 ffmpeg / gifski 转 GIF
- [ ] 提交 `assets/demo.gif`（如有 mp4 同步提交 `assets/demo.mp4`）
- [ ] README 顶部 hero 换成 GIF

**验收**
- [ ] GitHub README 顶部 GIF 自动播放且流畅，30s 内说清

---

### P0-4 单图速擦入口

- [x] HTML：主界面顶部加 Tab 切换组件
- [x] 单图模式复用现有 Canvas
- [x] 后端新增 `/api/single-erase`
- [x] 单图模式内部用 PerSAM-F
- [x] 前端：上传 → 框选 → 擦除 → 对比组件
- [x] 单图模式不写入 templates.json

**验收**
- [ ] 不读 README，3 步内擦出第一张图（依赖 **F3-5**）

---

### P0-5 失败兜底（手动修复入口）

- [x] 后端：no_match → `manual-pending`
- [x] 前端：manual-pending 显示"手动定位"按钮
- [⚠️] 点击"手动定位" → 框选 logo —— Canvas 能开但按钮永禁用，见 **F1-3**
- [x] 后端新增 `/api/manual-erase`
- [⚠️] 修复完成后结果合并回主网格（依赖 **F1-3**）
- [x] 手动修复仍失败时给出明确错误信息

**验收**
- [ ] 10 张图、5 张匹配失败 → 依次手动修复 → 全部 OK（依赖 **F1-3**）

---

### P0 阶段验收

- [ ] GitHub Release 提供 .dmg / .exe，新用户 5 分钟内试跑成功
- [ ] README 顶部有 GIF / 视频
- [ ] 仓库 clone 即跑，无须自带素材
- [ ] 首次使用挫败到放弃的概率 < 10%

> 📍 **Checkpoint**：P0 全部完成后停下来，看 P1 优先级是否还合理。

---

## P1 — 第 3–6 周：核心交互闭环 + 国际化基础

### P1-1 mask 微调画笔

- [ ] 设计 mask 编辑数据模型（base mask + delta strokes 数组）
- [ ] 前端：结果项点击放大后进入"微调"模式
- [ ] 前端：mask 编辑层独立于原图层，支持「+」涂笔 和「−」涂笔
- [ ] 前端：画笔大小可调、撤销 / 重做、确认 / 取消
- [ ] 后端：新增 `/api/refine-mask`
- [ ] 性能调优：refine 接口响应必须 < 200ms
- [ ] 微调完成后保存覆盖原结果

**验收**
- [ ] 用户能在 30 秒内修好一个 mask 错误
- [ ] 画笔体验流畅、无卡顿

---

### P1-2 进度反馈 / per-file 状态

- [ ] 把 `process_batch` 改成 generator，每张图 yield 一个进度事件
- [ ] 新增 `/api/process-stream`（SSE）；保留旧 `/api/process` 兼容
- [ ] 前端 EventSource 接入，结果网格先渲染"占位卡片"
- [ ] 状态机：start → matched → segmenting → erasing → done / failed
- [ ] 每张完成立即更新对应卡片
- [ ] 估算剩余时间：用前 3 张平均耗时外推

**验收**
- [ ] 50 张图批量处理时，每张完成都能立即看到结果

---

### P1-3 结果 mask 叠加预览

- [ ] server.py 擦除时同步保存 mask PNG 到 outputs 目录
- [ ] 结果 API 返回 mask URL
- [ ] 前端：结果项加 toggle，开关切换叠加渲染半透明红色 mask
- [ ] 默认关闭，用户主动开启

**验收**
- [ ] 用户能在结果上看到 AI 实际处理的区域

---

### P1-4 双语 README + GitHub topics 完善

- [ ] 翻译 README 主体为英文，保存为 `README.en.md`
- [ ] README 顶部加 `English | 简体中文` 切换链接
- [ ] GitHub About 设置：填好英文 Description
- [ ] 配置 Topics（watermark-removal / segment-anything / persam / opencv / lama-inpainting 等）
- [ ] 准备 Hacker News / r/MachineLearning 投递文案（备用）

**验收**
- [ ] 英文用户能完整读懂并跑通工具
- [ ] Topics 覆盖完全

---

### P1-5 拖拽 / 粘贴 / URL 输入

- [ ] 前端 Canvas 绑定 dragover / drop 事件
- [ ] 全局 paste 监听，转 file 上传
- [ ] 后端新增 `/api/upload-by-url`
- [ ] 大小 / 类型校验 + 错误提示

**验收**
- [ ] 拖拽、Cmd+V 截图、贴 URL 三种方式都能正常处理

---

### P1-6 模板库管理增强

- [ ] templates.json 增加 `group` 字段（向后兼容）
- [ ] 前端：按 group 折叠分组展示
- [ ] 前端：支持把模板拖拽到另一个分组
- [ ] 新增 `/api/export-templates`：打包 zip
- [ ] 新增 `/api/import-templates`：解 zip 合并到当前库
- [ ] 缩略图点击放大 lightbox

**验收**
- [ ] 模板数 > 20 时仍能高效定位
- [ ] 通过 zip 一键迁移整个模板库

---

### P1 阶段验收

- [ ] 单次任务完成率（含失败兜底后）> 90%
- [ ] 用户挫败恢复率 > 70%
- [ ] 国际用户可用、Topics 完整覆盖

> 📍 **Checkpoint**：P1 全部完成后停下来，决定下一步是 P2 视频支持还是 P3 商业化 / 社区文案。

---

## P2 — 第 7–12 周：延展场景（方向，不预先拆 todo）

- **视频水印移除**：按帧 + SAM 跟踪 + ffmpeg 合成
- **旋转 / 弯曲水印支持**：模板匹配阶段加旋转搜索 或 LoFTR feature matching
- **自动水印检测**：集成 watermark 检测模型
- **自托管服务模式**：纯 Web + Docker，团队部署
- **CLI 批处理升级**：让 `scripts/batch_remove_logo.py` 也支持 PerSAM 模式

---

## P3 — 长期：商业化 / 社区策略

- README 加竞品对照表（vs Cleanup.pictures / Photoshop 等）
- 价值主张文案重写：从工程视角改成用户视角
- 加 Sponsor 按钮（GitHub Sponsors / Buy me a coffee）
- 写技术原理 blog post，投递 Hacker News / dev.to / 掘金
- 远景：Cloud 版本支持视频处理（差异化商业入口）

---

## 衡量指标（每个里程碑后自检）

- **首次使用成功率**：新用户从下载到看到第一张擦除结果的转化率。目标 P0 后 > 80%。
- **单次任务完成率**：用户开一个会话能跑完一整批图的比例。目标 P1 后 > 90%。
- **用户从挫败中恢复率**：SAM / 模板匹配出问题时是放弃还是修补。目标 P1 后 > 70%。
- **GitHub Star / Fork 增长**：P0-3 GIF + P1-4 双语后跟踪 7 天增长曲线。

