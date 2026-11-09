# -*- coding: utf-8 -*-
"""v3 客户端组装脚本：产出可分发的便携版目录。

用法（在仓库根目录）:
    python client/build_runtime.py            # 组装 build/XiaoAiPetControl/
    python client/build_runtime.py --zip      # 额外产出单文件 zip

产出结构:
    build/XiaoAiPetControl/
    ├─ start_client.vbs      # 双击无窗启动托盘
    ├─ app/                  # voice_bridge.py + tray.pyw
    ├─ runtime/              # python-3.12-embed + site-packages（HA/miservice/pystray）
    └─ config/homeassistant/ # HA 配置骨架（首启自动补全）

之后用 client/installer.iss（Inno Setup）把该目录打成单个 setup.exe。
脚本可重复执行：runtime 已存在则跳过下载与 pip 安装。
"""
import io
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUILD = REPO / "build" / "XiaoAiPetControl"
RUNTIME = BUILD / "runtime"
APP = BUILD / "app"
CONFIG = BUILD / "config" / "homeassistant"

PY_EMBED_URL = "https://www.python.org/ftp/python/3.12.8/python-3.12.8-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
# 与当前实测环境完全一致的版本组合
PIP_PKGS = [
    "miservice==3.0.1",
    "pystray==0.19.5",
    "pillow==11.0.0",      # HA 2025.1.4 钉死 Pillow==11.0.0，托盘共用一份
    "homeassistant==2025.1.4",
]


def _download(url: str, dest: Path) -> Path:
    print(f"下载 {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    return dest


def stage_runtime() -> None:
    """组装内嵌 Python 运行时（已存在则跳过）。"""
    done_marker = RUNTIME / ".deps-installed"
    if done_marker.is_file():
        print("runtime 已存在，跳过")
        return
    RUNTIME.mkdir(parents=True, exist_ok=True)
    py = RUNTIME / "python.exe"

    zip_path = BUILD / "_python-embed.zip"
    if not zip_path.is_file():
        _download(PY_EMBED_URL, zip_path)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(RUNTIME)

    # embed 版默认不含 site-packages：解开 ._pth 的限制
    pth = RUNTIME / "python312._pth"
    pth.write_text("python312.zip\n.\nLib\\site-packages\nimport site\n",
                   encoding="ascii")

    # 装 pip
    get_pip = BUILD / "_get-pip.py"
    if not get_pip.is_file():
        _download(GET_PIP_URL, get_pip)
    subprocess.run([str(py), str(get_pip), "--no-warn-script-location"],
                   check=True)

    # fnvhash 0.1.0（HA 依赖链要求 <0.2）只有 sdist，先单独放行装好
    subprocess.run([str(py), "-m", "pip", "install", "fnvhash==0.1.0",
                    "--no-warn-script-location"], check=True)
    # HA 的依赖树太大，直接解析会 resolution-too-deep。改为确定性安装：
    # ha_pins.txt 是从 HA 2025.1.4 METADATA 提取的官方钉死清单（按 win/cp312
    # 环境标记过滤），--no-deps 一次装齐，零回溯
    pins = REPO / "client" / "ha_pins.txt"
    subprocess.run(
        [str(py), "-m", "pip", "install", "-r", str(pins),
         "--no-deps", "--no-warn-script-location", "--only-binary=:all:"],
        check=True,
    )
    # HA 本体 + 托盘/小米库（依赖已被上面钉死清单覆盖，同样 --no-deps）
    subprocess.run(
        [str(py), "-m", "pip", "install",
         "homeassistant==2025.1.4", "miservice==3.0.1",
         "pystray==0.19.5", "six==1.17.0",
         "--no-deps", "--no-warn-script-location", "--only-binary=:all:"],
        check=True,
    )
    # 修复遍：钉死包已全部就位（版本精确匹配，解析器无事可做），
    # 此时带依赖重装一遍只会补齐二级依赖（如 python-slugify→text_unidecode），
    # 不会再触发 resolution-too-deep
    subprocess.run(
        [str(py), "-m", "pip", "install", "-r", str(pins),
         "--no-warn-script-location", "--only-binary=:all:"],
        check=True,
    )
    # pip 自身 60MB+ 用不到，删掉减包体
    subprocess.run([str(py), "-m", "pip", "uninstall", "-y", "pip", "setuptools",
                    "wheel"], check=False)
    done_marker.touch()
    print("runtime 组装完成")


def stage_app() -> None:
    APP.mkdir(parents=True, exist_ok=True)
    shutil.copy2(REPO / "voice_bridge.py", APP / "voice_bridge.py")
    shutil.copy2(REPO / "client" / "tray.pyw", APP / "tray.pyw")
    icon = REPO / "client" / "icon.ico"
    if icon.is_file():
        shutil.copy2(icon, APP / "icon.ico")
    print("app 文件已复制")


def stage_config() -> None:
    """HA 配置骨架：最小 configuration.yaml，首启由 HA 自动补全其余。"""
    (CONFIG / "custom_components").mkdir(parents=True, exist_ok=True)
    yaml = CONFIG / "configuration.yaml"
    if not yaml.is_file():
        yaml.write_text(
            "# v3 客户端默认配置（客户端会自动追加 logger 等）\n"
            "default_config:\n"
            "\n"
            "logger:\n"
            "  default: warning\n"
            "  logs:\n"
            "    homeassistant.config_entries: critical\n"
            "    homeassistant: error\n",
            encoding="utf-8")
    print("config 骨架已就绪")


def stage_launcher() -> None:
    # 纯 ASCII 的 vbs：引号一律用 Chr(34) 拼，避开 VBScript 的 "" 转义陷阱
    # （双引号紧邻会被解析成「关串再开串」，导致"未结束的字符串常量"）
    vbs = BUILD / "start_client.vbs"
    vbs.write_text(
        'Set fso = CreateObject("Scripting.FileSystemObject")\n'
        'Set sh = CreateObject("Wscript.Shell")\n'
        "here = fso.GetParentFolderName(WScript.ScriptFullName)\n"
        "q = Chr(34)\n"
        'sh.Run q & here & "\\runtime\\pythonw.exe" & q & " " & _\n'
        "        q & here & \"\\app\\tray.pyw\" & q, 0, False\n",
        encoding="ascii")
    readme = BUILD / "README.txt"
    readme.write_text(
        "【小爱宠物语音控制 · 客户端】\n\n"
        "1. 双击 start_client.vbs 启动（推荐发送到桌面快捷方式）\n"
        "2. 托盘出现橙色猫爪图标，右键：打开管理面板 / 启动 HA / 日志 / 开机自启\n"
        "3. 第一次使用：托盘 → 打开管理面板 → 「账号」页登录小米账号\n"
        "4. 集成（小佩/德业等）在面板「集成中心」搜索安装\n\n"
        "数据都在本目录：app/.mi.token（登录令牌）、app/voice_bridge.json（口令）、\n"
        "config/homeassistant（HA 配置）。删除目录即完全卸载。\n",
        encoding="gbk")
    print("启动器与说明已生成")


def main() -> None:
    BUILD.mkdir(parents=True, exist_ok=True)
    stage_runtime()
    stage_app()
    stage_config()
    stage_launcher()
    if "--zip" in sys.argv:
        out = BUILD.with_suffix(".zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for f in BUILD.rglob("*"):
                if f.is_file() and "_python-embed" not in f.name and "_get-pip" not in f.name:
                    z.write(f, f.relative_to(BUILD.parent))
        print(f"zip 已生成: {out}")
    print("完成:", BUILD)


if __name__ == "__main__":
    main()
