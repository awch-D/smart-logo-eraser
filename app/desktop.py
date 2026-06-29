"""桌面应用入口 — 跨平台 (macOS + Windows)

启动流程：
  1. 在后台线程里启动 Flask（绑定 127.0.0.1 + 随机端口，避免冲突）
  2. pywebview 打开原生窗口指向 Flask URL
  3. 窗口关闭时主进程退出

打包后（PyInstaller）也用同样的入口，cv2/numpy 静态依赖随包带走。
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path
from wsgiref.simple_server import WSGIServer, make_server

import webview

# 让被 PyInstaller 打包时也能找到资源
APP_DIR = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)).resolve()
os.chdir(APP_DIR)

# 数据目录写到用户主目录下（打包后包内是只读的）
HOME = Path.home() / '.logo_eraser'
HOME.mkdir(exist_ok=True)
for sub in ('uploads', 'outputs', 'sessions'):
    (HOME / sub).mkdir(exist_ok=True)
os.environ['LOGOERASER_DATA'] = str(HOME)

from server import app  # noqa: E402 — 调整 cwd 后才 import


def pick_free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


_server: WSGIServer | None = None


def run_server(host: str, port: int):
    global _server
    _server = make_server(host, port, app)
    _server.serve_forever()


def wait_ready(host: str, port: int, timeout: float = 8.0):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main():
    host = '127.0.0.1'
    port = pick_free_port()
    t = threading.Thread(target=run_server, args=(host, port), daemon=True)
    t.start()
    if not wait_ready(host, port):
        print('Flask 启动失败', file=sys.stderr)
        sys.exit(1)

    url = f'http://{host}:{port}/'
    print(f'Logo Eraser → {url}')

    win = webview.create_window(
        'Logo 擦除工具',
        url,
        width=1280, height=820,
        min_size=(960, 640),
        resizable=True,
        text_select=True,
    )

    def on_closed():
        if _server is not None:
            _server.shutdown()
    win.events.closed += on_closed

    # macOS 默认 cocoa；Windows 默认 edgechromium（需 WebView2 Runtime）
    gui = 'edgechromium' if sys.platform.startswith('win') else None
    webview.start(gui=gui)


if __name__ == '__main__':
    main()
