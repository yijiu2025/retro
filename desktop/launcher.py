# -*- coding: utf-8 -*-
"""RetroWeb.exe —— 打包版启动器（PyInstaller --onefile --windowed 冻结）。

它自己只做「看门」：真正的重活（Flask + pywebview + 模型服务）全部由
`python39\\python.exe _internal\\desktop_app.py` 承担。之所以不让启动器
直接干那些活：torch / PyG / RDKit 这一摞科学栈**不过 PyInstaller 的
import hook**（hidden imports、DLL、数据文件全是坑），而它们对
「便携 Python + 真实 site-packages」完全无感 —— 拆开是最稳的形态。

职责：
  1. 单实例（命名 Mutex，重复双击直接唤起已有窗口而非再起一份）
  2. 找到便携 Python 与 desktop_app.py，缺失时弹窗说明
  3. 以 **无控制台** 方式拉起桌面进程并看守
  4. 桌面进程异常退出 → 弹窗展示日志尾部（否则用户只看到「闪退」）
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time

MUTEX_NAME = "RetroWeb.SingleInstance.v1"
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000

PKG_ROOT = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                                            else __file__))
PYTHON_EXE = os.path.join(PKG_ROOT, "python39", "python.exe")
APP_SCRIPT = os.path.join(PKG_ROOT, "_internal", "desktop_app.py")
LOG_DIR = os.path.join(PKG_ROOT, "data", "logs")
LAUNCHER_LOG = os.path.join(LOG_DIR, "launcher.log")


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LAUNCHER_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _dialog(title: str, text: str) -> None:
    """无控制台环境下唯一能给用户反馈的方式。"""
    try:
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)  # MB_ICONERROR
    except Exception:  # noqa: BLE001
        _log(f"[dialog] {title}: {text}")


def _log_tail(n: int = 25) -> str:
    try:
        with open(os.path.join(LOG_DIR, "desktop.log"), encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()[-n:]
        return "".join(lines)
    except OSError:
        return "（没有找到 desktop.log）"


def _already_running() -> bool:
    if os.name != "nt":
        return False
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)  # noqa: F841  # 句柄须保活：具名互斥体随句柄关闭即释放
    return ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS


def main() -> None:
    _log(f"启动器就绪，包根: {PKG_ROOT}")

    if _already_running():
        _log("已有实例在运行（Mutex 已存在），本次双击直接退出")
        return

    missing = [p for p in (PYTHON_EXE, APP_SCRIPT) if not os.path.isfile(p)]
    if missing:
        _dialog(
            "RetroWeb 启动失败",
            "缺少必要文件：\n  " + "\n  ".join(missing)
            + "\n\n请确认压缩包已完整解压（不要只拷 exe）。",
        )
        _log(f"缺少文件: {missing}")
        return

    _log(f"拉起桌面进程: {PYTHON_EXE} {APP_SCRIPT}")
    try:
        proc = subprocess.Popen(
            [PYTHON_EXE, APP_SCRIPT],
            cwd=PKG_ROOT,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )
    except Exception as e:  # noqa: BLE001
        _dialog("RetroWeb 启动失败", f"无法启动桌面进程：\n{type(e).__name__}: {e}")
        _log(f"Popen 失败: {e}")
        return

    rc = proc.wait()
    _log(f"桌面进程退出，返回码 {rc}")
    if rc != 0:
        _dialog(
            "RetroWeb 异常退出",
            f"桌面进程返回码 {rc}。\n\n日志尾部：\n{_log_tail()}\n完整日志：{LOG_DIR}",
        )


if __name__ == "__main__":
    main()
