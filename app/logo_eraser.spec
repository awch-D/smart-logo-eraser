# Logo Eraser 打包配置 — 同时支持 macOS .app 和 Windows .exe
# 用法：pyinstaller logo_eraser.spec
# 产物：
#   - macOS: dist/LogoEraser.app   (默认 onedir，bundle 内部仍是文件夹)
#   - Windows: dist/LogoEraser/LogoEraser.exe
#
# 体积策略（P0-1 方案 A）：
#   - 安装包默认不带 torch / iopaint / MobileSAM 权重
#   - 用户首次勾选"高精度模式"时，由 app/runtime.py 按需下载到
#     ~/.logo_eraser/runtime/，结果缓存复用
#   - vendor/personalize_sam 这个 ~50MB 的源码仍然打进去（PerSAM 算法
#     依赖它），权重 mobile_sam.pt 走 runtime 下载（~38MB）
#   - 顶层使用 onedir 而非 onefile，便于 runtime 注入 site-packages

import sys
from pathlib import Path

block_cipher = None
HERE = Path('.').resolve()

# 把前端资源 + PerSAM 算法源码一起带进去
datas = [
    ('templates', 'templates'),
    ('static', 'static'),
    # PerSAM 源码（算法依赖，体积小）
    ('vendor/personalize_sam/per_segment_anything', 'vendor/personalize_sam/per_segment_anything'),
]

hiddenimports = [
    'PIL._tkinter_finder',
    # pywebview 后端
    'webview.platforms.cocoa' if sys.platform == 'darwin' else 'webview.platforms.edgechromium',
    # flask + werkzeug 子模块
    'werkzeug.middleware', 'werkzeug.middleware.dispatcher',
    # 我们自己的两个新模块
    'runtime',
    'persam_engine',
]

a = Analysis(
    ['desktop.py'],
    pathex=[str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # 砍掉用不到的大包，缩小体积
        'matplotlib', 'tkinter', 'pytest', 'IPython', 'jupyter',
        # 高精度模式依赖 —— 默认不打包，由 runtime 按需下载
        'torch', 'torchvision', 'transformers', 'diffusers',
        'iopaint',
        'timm', 'einops',  # PerSAM 运行时依赖，也走 runtime
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# ---- onedir 模式（关键：保留可写的 _internal/ 目录，便于 runtime 注入）----
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='LogoEraser',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name='LogoEraser',
)

# macOS .app bundle —— 内部仍然是 onedir 布局
if sys.platform == 'darwin':
    app_bundle = BUNDLE(
        coll,
        name='LogoEraser.app',
        icon=None,
        bundle_identifier='com.arno.logoeraser',
        info_plist={
            'CFBundleName': 'Logo Eraser',
            'CFBundleDisplayName': 'Logo 擦除工具',
            'CFBundleShortVersionString': '1.0.0',
            'NSHighResolutionCapable': True,
            'NSRequiresAquaSystemAppearance': False,  # 跟随系统主题
        },
    )
