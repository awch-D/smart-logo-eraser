# Logo Eraser 打包配置 — 同时支持 macOS .app 和 Windows .exe
# 用法：pyinstaller logo_eraser.spec
# 产物：dist/LogoEraser.app (macOS) 或 dist/LogoEraser/LogoEraser.exe (Windows)

import sys
from pathlib import Path

block_cipher = None
HERE = Path('.').resolve()

# 把前端资源和默认模板一起带进去
datas = [
    ('templates', 'templates'),
    ('static', 'static'),
]
if (HERE / 'logo_template.png').exists():
    datas.append(('../logo_template.png', '.'))


hiddenimports = [
    'PIL._tkinter_finder',
    # pywebview 后端
    'webview.platforms.cocoa' if sys.platform == 'darwin' else 'webview.platforms.edgechromium',
    # flask + werkzeug 子模块
    'werkzeug.middleware', 'werkzeug.middleware.dispatcher',
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
        'torch', 'torchvision', 'transformers', 'diffusers', 'iopaint',  # 默认不打包 iopaint，作为可选项
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

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

# macOS .app bundle
if sys.platform == 'darwin':
    app = BUNDLE(
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
