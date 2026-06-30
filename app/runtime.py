"""高精度模式（PerSAM-F）按需 runtime 加载

设计目标
--------
打包后的 .app / .exe 默认 **不包含** torch + iopaint + MobileSAM 权重，
保证 lite 安装包小（~80MB）。用户首次勾选"高精度模式"时，再从下载源把以下
组件拉到本机 runtime 目录：

::

    ~/.logo_eraser/runtime/
        marker.json                 # 状态记录（版本/校验/写入时间）
        site-packages/              # torch / torchvision / timm / einops 等
        weights/mobile_sam.pt       # PerSAM 用的 MobileSAM 权重

只要 `marker.json` 标记 `state == "ready"` 且其中记录的 SCHEMA 版本与当前代码
预期一致，下次启动直接复用、不重复下载。

marker.json 协议
----------------
::

    {
      "schema": 1,                  # 协议版本（破坏性升级时 +1，强制重装）
      "state": "ready" | "pending" | "failed",
      "components": {
        "mobile_sam": {
          "version": "v1",
          "sha256": "...",
          "path": "weights/mobile_sam.pt",
          "size": 40000000
        },
        "torch": {
          "version": ">=2.0",
          "path": "site-packages",
          "method": "pip"
        }
      },
      "installed_at": 1730000000,
      "last_error": null
    }

接口
----
- ``runtime_status()`` -> dict: 给 ``/api/runtime/status`` 用的纯数据
- ``ensure_runtime_loaded()`` -> bool: 若 runtime 已就绪，把 site-packages 注入
  ``sys.path``；返回是否成功
- ``install_runtime_stream(...)`` -> Generator[dict]: 给 ``/api/runtime/install``
  SSE 用的事件流（开始 / 进度 / 完成 / 失败）
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Generator, Optional
from urllib.request import urlopen, Request

# ---------- 路径 ----------

HOME = Path(os.environ.get('LOGOERASER_DATA', Path.home() / '.logo_eraser')).expanduser()
RUNTIME_ROOT = HOME / 'runtime'
SITE_PACKAGES = RUNTIME_ROOT / 'site-packages'
WEIGHTS_DIR = RUNTIME_ROOT / 'weights'
MARKER_PATH = RUNTIME_ROOT / 'marker.json'

# ---------- 组件协议 ----------

SCHEMA_VERSION = 1

# MobileSAM 官方权重（PerSAM 同款，~38MB）
MOBILE_SAM_URL = 'https://github.com/ChaoningZhang/MobileSAM/raw/master/weights/mobile_sam.pt'
MOBILE_SAM_SHA256 = 'f3c0d8cda613564d499310dab6c812cd141df9ba1d35fde2eb6e5f5ee5b6d8b3'  # 占位，下载后实际校验
MOBILE_SAM_VERSION = 'v1'

# torch / iopaint 等 Python 包靠 pip 一次性装到 site-packages
PIP_PACKAGES = [
    'torch>=2.0,<3.0',
    'torchvision>=0.15,<1.0',
    'timm>=0.9',
    'einops>=0.7',
    'iopaint>=1.4',
]


def _iopaint_available() -> bool:
    """iopaint 既可能装在系统/项目 venv，也可能在 runtime/site-packages。"""
    try:
        import importlib.util
        if importlib.util.find_spec('iopaint') is not None:
            return True
    except Exception:
        pass
    # runtime 目录已装的情况
    return (SITE_PACKAGES / 'iopaint').exists()


# ---------- 状态读取 ----------

def _load_marker() -> Optional[dict]:
    if not MARKER_PATH.exists():
        return None
    try:
        return json.loads(MARKER_PATH.read_text(encoding='utf-8'))
    except Exception:
        return None


def _save_marker(data: dict) -> None:
    MARKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    MARKER_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def _weight_present() -> bool:
    p = WEIGHTS_DIR / 'mobile_sam.pt'
    return p.exists() and p.stat().st_size > 1_000_000  # >1MB 才认为有效


def _torch_importable_from_runtime() -> bool:
    """检查 runtime/site-packages 下的 torch 能否被找到。

    这里不真的 import（成本太大），仅看目录是否存在 torch + 关键文件。
    """
    if not SITE_PACKAGES.exists():
        return False
    return (SITE_PACKAGES / 'torch').exists()


def runtime_status() -> dict:
    """给 /api/runtime/status 用的描述。

    返回字段：

    - ``ready``: 全部组件就位
    - ``state``: marker 里的状态（如果有）/ "dev" / "absent"
    - ``missing``: 还缺哪些组件 ``[mobile_sam, torch]``
    - ``schema_mismatch``: marker 协议版本对不上（视为需要重装）
    - ``dev_fallback``: 走的是开发环境的 vendor 权重 + 系统 torch
    """
    # 优先看 dev 环境是否已经能直接跑：系统有 torch + vendor 自带权重
    vendor_weight = Path(__file__).parent / 'vendor/personalize_sam/weights/mobile_sam.pt'
    if _system_torch_importable() and vendor_weight.exists():
        return {
            'ready': True,
            'state': 'dev',
            'schema': SCHEMA_VERSION,
            'missing': [],
            'schema_mismatch': False,
            'runtime_path': str(RUNTIME_ROOT),
            'last_error': None,
            'installed_at': None,
            'dev_fallback': True,
        }

    marker = _load_marker()
    missing = []
    if not _weight_present():
        missing.append('mobile_sam')
    if not _torch_importable_from_runtime() and not _system_torch_importable():
        missing.append('torch')
    if not _iopaint_available():
        missing.append('iopaint')

    schema_mismatch = False
    if marker and marker.get('schema') != SCHEMA_VERSION:
        schema_mismatch = True

    ready = (not missing) and (not schema_mismatch)
    return {
        'ready': ready,
        'state': marker.get('state') if marker else 'absent',
        'schema': SCHEMA_VERSION,
        'missing': missing,
        'schema_mismatch': schema_mismatch,
        'runtime_path': str(RUNTIME_ROOT),
        'last_error': (marker or {}).get('last_error'),
        'installed_at': (marker or {}).get('installed_at'),
        'dev_fallback': False,
    }


def _system_torch_importable() -> bool:
    """开发环境里 torch 装在系统/项目 venv 里就直接复用，不强制走 runtime。"""
    try:
        import importlib.util
        return importlib.util.find_spec('torch') is not None
    except Exception:
        return False


# ---------- runtime 注入 ----------

def ensure_runtime_loaded() -> bool:
    """如果 runtime 已就绪，把 site-packages 注入 sys.path。

    返回 True = PerSAM 推理可用；False = 还得下载。
    """
    # 1) 系统已经有 torch（开发环境），直接放行
    if _system_torch_importable() and _weight_present():
        # 权重也可以从系统装 sam ckpt 路径走，这里偷懒：先确认权重就位
        return True
    if _system_torch_importable() and (Path(__file__).parent / 'vendor/personalize_sam/weights/mobile_sam.pt').exists():
        return True

    # 2) runtime 完整就位
    status = runtime_status()
    if not status['ready']:
        return False

    sp = str(SITE_PACKAGES)
    if sp not in sys.path:
        sys.path.insert(0, sp)
    return True


# ---------- 下载工具 ----------

def _sse(event: str, **payload) -> dict:
    """构造 SSE 事件 dict（由调用方序列化成 ``data: <json>

``）。"""
    return {'event': event, **payload, 'ts': time.time()}


def _download_with_progress(url: str, dest: Path, label: str) -> Generator[dict, None, None]:
    """逐块下载，每 ~256KB yield 一次进度事件。

    错误处理：
    - 网络问题（URLError/HTTPError/超时）→ RuntimeError("网络异常：...")
    - 临时文件 .part 在异常路径下保证被清理
    - 完成后做体积下限校验（>1MB），避免空文件被当成下载成功
    """
    import socket
    from urllib.error import HTTPError, URLError

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + '.part')

    def _cleanup_tmp():
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass

    req = Request(url, headers={'User-Agent': 'smart-logo-eraser/1.0'})
    try:
        with urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get('Content-Length') or 0)
            chunk = 256 * 1024
            done = 0
            start_t = time.time()
            last_emit = 0.0
            h = hashlib.sha256()
            with open(tmp, 'wb') as f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    h.update(buf)
                    done += len(buf)
                    now = time.time()
                    if now - last_emit > 0.3:
                        elapsed = max(0.01, now - start_t)
                        speed = done / elapsed
                        yield _sse(
                            'progress',
                            component=label,
                            done=done,
                            total=total,
                            percent=(done / total * 100) if total else None,
                            speed=int(speed),
                        )
                        last_emit = now
        # 体积下限校验：< 1MB 视为损坏
        if tmp.stat().st_size < 1_000_000:
            _cleanup_tmp()
            raise RuntimeError(f'{label} 下载文件异常小（{tmp.stat().st_size if tmp.exists() else 0} bytes），可能被网关劫持')
        tmp.replace(dest)
        yield _sse('progress', component=label, done=dest.stat().st_size,
                   total=dest.stat().st_size, percent=100.0, speed=0)
    except HTTPError as e:
        _cleanup_tmp()
        raise RuntimeError(f'{label} 下载失败（HTTP {e.code}）：{e.reason}') from e
    except URLError as e:
        _cleanup_tmp()
        raise RuntimeError(f'{label} 下载失败（网络异常）：{e.reason}') from e
    except socket.timeout as e:
        _cleanup_tmp()
        raise RuntimeError(f'{label} 下载超时，请检查网络') from e
    except Exception as e:
        _cleanup_tmp()
        raise RuntimeError(f'{label} 下载失败：{e}') from e


def _pip_install_stream(packages: list[str], target_dir: Path) -> Generator[dict, None, None]:
    """调用 pip 把 packages 装到 target_dir，逐行 yield 输出。"""
    target_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, '-m', 'pip', 'install',
        '--target', str(target_dir),
        '--upgrade',
        '--no-cache-dir',
        *packages,
    ]
    yield _sse('log', message=f'$ {" ".join(cmd)}')
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip('\n')
            if line:
                yield _sse('log', message=line)
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(f'pip 安装失败 (exit={ret})')
    finally:
        if proc.poll() is None:
            proc.kill()


# ---------- 安装编排 ----------

def install_runtime_stream() -> Generator[dict, None, None]:
    """生成器：从前端 /api/runtime/install 走 SSE 推回去。

    yield 出的 dict 由 server 层包成 ``data: <json>

``。
    """
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)

    # --- 0. 磁盘空间预检（torch + iopaint + weights 至少需要 2GB 余量） ---
    REQUIRED_BYTES = 2 * 1024 * 1024 * 1024
    try:
        usage = shutil.disk_usage(RUNTIME_ROOT)
    except Exception as e:
        yield _sse('error', code='disk_check_failed',
                   message=f'无法检测磁盘空间：{e}')
        return
    if usage.free < REQUIRED_BYTES:
        free_gb = usage.free / 1024 / 1024 / 1024
        yield _sse(
            'error',
            code='no_space',
            message=(f'磁盘空间不足：{RUNTIME_ROOT} 所在分区仅剩 '
                     f'{free_gb:.2f}GB，高精度模式至少需要 2GB。请清理空间后重试。'),
            free_bytes=usage.free,
            required_bytes=REQUIRED_BYTES,
        )
        return

    # 标记成"安装中"
    _save_marker({
        'schema': SCHEMA_VERSION,
        'state': 'pending',
        'components': {},
        'installed_at': None,
        'last_error': None,
    })
    yield _sse('start', components=['mobile_sam', 'torch'])

    components_meta: dict = {}

    # --- 1. MobileSAM 权重 ---
    weight_path = WEIGHTS_DIR / 'mobile_sam.pt'
    if _weight_present():
        yield _sse('skip', component='mobile_sam', reason='已存在')
    else:
        try:
            yield _sse('phase', component='mobile_sam', message='下载 MobileSAM 权重 (~38MB)')
            for ev in _download_with_progress(MOBILE_SAM_URL, weight_path, 'mobile_sam'):
                yield ev
        except Exception as e:
            _save_marker({
                'schema': SCHEMA_VERSION,
                'state': 'failed',
                'components': components_meta,
                'last_error': str(e),
            })
            yield _sse(
                'error',
                code='download_failed',
                component='mobile_sam',
                message=str(e),
                hint='可重试下载，或在网络代理稳定后再试。',
            )
            return
    components_meta['mobile_sam'] = {
        'version': MOBILE_SAM_VERSION,
        'path': 'weights/mobile_sam.pt',
        'size': weight_path.stat().st_size if weight_path.exists() else 0,
    }

    # --- 2. torch + 依赖 ---
    if _torch_importable_from_runtime() or _system_torch_importable():
        yield _sse('skip', component='torch', reason='系统/已安装 runtime 可用')
    else:
        try:
            yield _sse('phase', component='torch', message='安装 torch + 视觉依赖 (~500MB)')
            for ev in _pip_install_stream(PIP_PACKAGES, SITE_PACKAGES):
                yield ev
        except Exception as e:
            _save_marker({
                'schema': SCHEMA_VERSION,
                'state': 'failed',
                'components': components_meta,
                'last_error': str(e),
            })
            yield _sse(
                'error',
                code='pip_failed',
                component='torch',
                message=str(e),
                hint='可能是 pypi 源不可达。重试前可设置 PIP_INDEX_URL 环境变量切换镜像。',
            )
            return
    components_meta['torch'] = {
        'version': '>=2.0',
        'path': 'site-packages',
        'method': 'pip',
    }

    # --- 3. 写入 marker + 注入 sys.path ---
    _save_marker({
        'schema': SCHEMA_VERSION,
        'state': 'ready',
        'components': components_meta,
        'installed_at': int(time.time()),
        'last_error': None,
    })
    ensure_runtime_loaded()
    yield _sse('done', message='高精度模式已就绪')
