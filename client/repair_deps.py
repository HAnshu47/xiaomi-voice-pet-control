# -*- coding: utf-8 -*-
"""元数据级依赖修复：不走 pip 解析器，直接补齐缺失的传递依赖。

原理：读已安装发行版的 METADATA Requires-Dist，按当前环境标记过滤，
找出未安装的，用 pip --no-deps 精确安装；每轮在全新子进程里扫描
（importlib.metadata 有目录缓存，同进程连续扫描会漏掉刚装的包）。
用法: runtime/python.exe client/repair_deps.py
"""
import re
import subprocess
import sys


def scan_once() -> list[str]:
    """在当前进程扫描缺失依赖名（归一化为 PEP 503 风格）。"""
    import importlib.metadata as im
    from packaging.requirements import Requirement
    from packaging.markers import default_environment

    def norm(s: str) -> str:
        # PEP 503：[-_.]+ 统一成 -（winrt-Windows.Foundation 这类带点名的包）
        return re.sub(r"[-_.]+", "-", s.lower())

    env = default_environment()
    have = {norm(d.metadata["Name"]) for d in im.distributions() if d.metadata.get("Name")}
    todo = []
    for dist in im.distributions():
        for raw in (dist.requires or []):
            try:
                req = Requirement(raw)
            except Exception:
                continue
            if req.marker and not req.marker.evaluate(env):
                continue
            if norm(req.name) not in have:
                todo.append(req.name)
    return sorted(set(todo))


def main() -> None:
    py = sys.executable
    for round_no in range(1, 9):
        # 每轮新起子进程扫描，避开 importlib.metadata 的目录缓存
        r = subprocess.run([py, __file__, "--scan"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        names = [n for n in (r.stdout or "").splitlines() if n.strip()]
        if not names:
            print(f"第 {round_no} 轮：依赖闭环，无缺失")
            return
        print(f"第 {round_no} 轮缺失 {len(names)} 个: {', '.join(names)}", flush=True)
        subprocess.run(
            [py, "-m", "pip", "install", *names,
             "--no-deps", "--no-warn-script-location"],
            check=False,
        )
    print("警告：8 轮未闭环，请人工检查")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--scan":
        print("\n".join(scan_once()))
    else:
        main()
