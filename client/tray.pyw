# -*- coding: utf-8 -*-
r"""v3 客户端托盘程序。

双击 start_client.vbs（= runtime\pythonw.exe tray.pyw）后：
  - 系统托盘常驻一个图标
  - 自动拉起 voice_bridge（面板 + 轮询 + HA 看门狗都在里面）
  - 菜单：打开面板 / 启动 HA / 日志 / 开机自启 / 退出

设计原则：托盘只做「壳」。轮询、面板、HA 生命周期全部由 voice_bridge 负责，
托盘通过面板 HTTP API（127.0.0.1:8124）与之交互，避免两处状态不同步。
HA 是独立进程（BREAKAWAY），退出托盘不影响它继续服务口令。
"""
import ctypes
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent          # app/
RUNTIME_DIR = APP_DIR.parent / "runtime"            # runtime/
PYTHONW = RUNTIME_DIR / "pythonw.exe"
BRIDGE_SCRIPT = APP_DIR / "voice_bridge.py"
LOG_PATH = APP_DIR / "voice_bridge.log"
PANEL_URL = "http://127.0.0.1:8124"
MUTEX_NAME = "XiaoAiPetControlTray.Mutex"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "XiaoAiPetControl"

# ---------------------------------------------------------------------------
# 单实例：命名互斥量，第二个实例直接退出
# ---------------------------------------------------------------------------
_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
if ctypes.windll.kernel32.GetLastError() == 183:    # ERROR_ALREADY_EXISTS
    # 已在运行：直接把面板带到前台
    _open_panel()
    sys.exit(0)


def _open_panel() -> None:
    """用 Edge 应用模式开面板：独立窗口、无地址栏，看起来是桌面应用而非网页。"""
    edge = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) \
        / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    if not edge.is_file():
        edge = Path(r"C:\Program Files") / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    if edge.is_file():
        flags = 0x00000008 | 0x00000200          # DETACHED | NEW_PROCESS_GROUP
        subprocess.Popen([str(edge), f"--app={PANEL_URL}",
                          f"--user-data-dir={APP_DIR / '.edge-profile'}"],
                         creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    else:
        webbrowser.open(PANEL_URL)


def _bridge_alive() -> bool:
    try:
        with urllib.request.urlopen(PANEL_URL + "/api/status", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _start_bridge() -> str:
    if _bridge_alive():
        return "已在运行"
    if not PYTHONW.is_file():
        return f"找不到运行时: {PYTHONW}"
    flags = 0x00000008 | 0x00000200 | 0x01000000  # DETACHED | NEW_GROUP | BREAKAWAY
    try:
        subprocess.Popen(
            [str(PYTHONW), str(BRIDGE_SCRIPT), "--run"],   # 必须带 --run 才常驻
            cwd=str(APP_DIR), creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
    except OSError as e:
        return f"启动失败: {e}"     # 托盘不能因拉起失败而崩
    return "已启动"


def _post(path: str, payload: dict) -> str:
    try:
        req = urllib.request.Request(
            PANEL_URL + path, method="POST",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        return d.get("msg") or d.get("error") or "已发送"
    except Exception as e:
        return f"失败: {e}"


def _autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except OSError:
        return False


def _autostart_toggle() -> str:
    import winreg
    target = f'"{PYTHONW}" "{BRIDGE_SCRIPT}" --run'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if _autostart_enabled():
                winreg.DeleteValue(k, RUN_VALUE)
                return "已关闭开机自启"
            winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, target)
            return "已开启开机自启"
    except OSError as e:
        return f"失败: {e}"


def _make_icon():
    """用 Pillow 现画一个图标，避免带资源文件。橙色圆底 + 白色猫爪印。"""
    from PIL import Image, ImageDraw
    import pystray

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([2, 2, 62, 62], fill=(245, 130, 32, 255))          # 橙底
    # 猫爪：一个大肉垫 + 四个脚趾
    d.ellipse([22, 34, 42, 52], fill=(255, 255, 255, 255))
    for i, (x, y, r) in enumerate([(18, 26, 5), (27, 18, 5), (37, 18, 5), (46, 26, 5)]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, 255))

    def on_open(icon, item):
        _open_panel()

    def on_ha(icon, item):
        notify(_post("/api/ha/start", {}))

    def on_log(icon, item):
        if LOG_PATH.is_file():
            os.startfile(LOG_PATH)                               # noqa: S606
        else:
            notify("还没有日志文件")

    def on_autostart(icon, item):
        notify(_autostart_toggle())
        icon.update_menu()

    def on_quit(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("打开管理面板", on_open, default=True),
        pystray.MenuItem("启动 Home Assistant", on_ha),
        pystray.MenuItem("查看日志", on_log),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: ("✔ 开机自启" if _autostart_enabled()
                                       else "开机自启"), on_autostart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出（HA 继续运行）", on_quit),
    )
    return pystray.Icon("xiaoai_pet", img, "小爱宠物语音控制", menu)


_notify_at = [0.0]


def notify(msg: str) -> None:
    """托盘气泡通知，3 秒限流。"""
    if not msg or time.time() - _notify_at[0] < 3:
        return
    _notify_at[0] = time.time()
    try:
        _icon.notify(msg, "小爱宠物语音控制")
    except Exception:
        pass


_icon = _make_icon()


def _watch_bridge() -> None:
    """每 10 秒检查 voice_bridge，死了自动拉回（与 HA 看门狗同一思路）。"""
    import threading

    def loop():
        while _icon.visible:
            if not _bridge_alive():
                _start_bridge()
                time.sleep(20)          # 给冷启动留时间，避免反复拉
            time.sleep(10)

    threading.Thread(target=loop, daemon=True).start()


if __name__ == "__main__":
    msg = _start_bridge()
    notify({"已启动": "客户端已启动，正在连接小爱…"}.get(msg, msg))
    _watch_bridge()
    _icon.run()
