# -*- coding: utf-8 -*-
"""桌面版 HTTP 面：前端静态托管 + 模型下载 + GPU 管理 + 系统小工具。

由 `app.py` 在创建 Flask 实例后 `import desktop_routes` 一行挂上
（见构建脚本对 app.py 的增量）。**不新增路由到 app.py 本体**，
将来上游更新 app.py 时这一行基本不会冲突。

路由清单（全部挂在现有 `app` 对象上）：

    GET  /                              → dist/index.html（SPA 入口）
    GET  /<path>                        → dist 静态文件；找不到回 index.html
    GET  /api/desktop/info              → 包路径 / 版本 / 计算模式
    POST /api/desktop/config            → 改 compute 等配置
    GET  /api/models/download/status    → 全部模型下载状态
    POST /api/models/download           → {key} 开始下载
    POST /api/models/download/cancel    → {key} 取消
    POST /api/models/remove             → {key} 删除已安装模型
    GET  /api/models/download/stream    → SSE 进度
    GET  /api/gpu/status                → GPU 包状态 + 当前后端
    GET  /api/gpu/probe                 → 探测本机 GPU 是否真的可用
    POST /api/gpu/install               → 开始安装 cu121 包
    POST /api/gpu/cancel                → 取消安装
    POST /api/gpu/uninstall             → 卸载
    GET  /api/gpu/stream                → SSE 安装进度
    POST /api/system/open-folder        → {path} 资源管理器打开（限包内目录）
    POST /api/desktop/restart-model-service → 切换 CPU/GPU 后重启模型服务
"""
from __future__ import annotations

import json
import os
import time

from flask import Response, jsonify, request, send_from_directory

import config_store
import gpu_manager
import model_download

#: 前端产物目录（构建时拷入 _internal/frontend）
FRONTEND_DIR = config_store.FRONTEND_DIR

#: SPA 入口缺失时的兜底（API-only 部署 / 构建漏拷前端）
_NO_FRONTEND_HINT = {
    "error": "前端产物缺失",
    "hint": f"expected at {FRONTEND_DIR}",
}


def _sse(gen) -> Response:
    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 反代别缓冲，否则 SSE 变批量
            "Connection": "keep-alive",
        },
    )


def _sse_loop(snapshot, interval: float = 0.7):
    """每 interval 秒推一次快照；15 秒无变化也发一次心跳注释行保活。"""
    last = ""
    while True:
        try:
            payload = json.dumps(snapshot(), ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            payload = json.dumps({"error": str(e)}, ensure_ascii=False)
        if payload != last:
            yield f"data: {payload}\n\n"
            last = payload
        else:
            yield ": ping\n\n"
        time.sleep(interval)


# --------------------------------------------------------------------------
# 静态托管（同源：免 CORS、免 Node 中转、免登录 iframe）
# --------------------------------------------------------------------------
def _send_index():
    if not os.path.isfile(os.path.join(FRONTEND_DIR, "index.html")):
        return jsonify(_NO_FRONTEND_HINT), 503
    return send_from_directory(FRONTEND_DIR, "index.html")


def _send_spa(path: str):
    """dist 里有的文件（assets/ favicon/ 顶层 js）直接发；其余回 index.html。

    🔴 `/api/*` 绝不回 index.html：那会把 API 404 变成 200 + HTML，
        前端的错误处理全瞎。也不回 404 页 —— 交给前端路由（history 模式）。
    """
    if path.startswith("api/"):
        return jsonify({"error": f"not found: /{path}"}), 404
    safe = os.path.normpath(path).replace("\\", "/").lstrip("/")
    if safe.startswith(".."):
        return jsonify({"error": "invalid path"}), 400
    full = os.path.join(FRONTEND_DIR, safe)
    if os.path.isfile(full):
        return send_from_directory(FRONTEND_DIR, safe)
    return _send_index()


def register(app) -> None:
    """把本模块的全部路由挂到 Flask app 上。"""

    @app.route("/")
    def _desktop_index():
        return _send_index()

    @app.route("/<path:subpath>")
    def _desktop_spa(subpath: str):
        return _send_spa(subpath)

    # ------------------------------------------------------------------
    # 桌面信息 / 配置
    # ------------------------------------------------------------------
    @app.route("/api/desktop/info")
    def _desktop_info():
        cfg = config_store.load_config()
        return jsonify({
            "app": "RetroWeb",
            "version": "1.0.0",
            "pkg_root": config_store.PKG_ROOT,
            "models_dir": config_store.models_dir(),
            "frontend_dir": FRONTEND_DIR,
            "frontend_ok": os.path.isfile(os.path.join(FRONTEND_DIR, "index.html")),
            "python": config_store.PYTHON_EXE,
            "compute": cfg.get("compute", "auto"),
            "rule_workers": cfg.get("rule_workers", 2),
        })

    @app.route("/api/desktop/config", methods=["POST"])
    def _desktop_config_save():
        data = request.get_json(force=True, silent=True) or {}
        patch = {}
        if "compute" in data:
            compute = str(data["compute"]).lower()
            if compute not in ("auto", "cpu", "gpu"):
                return jsonify({"ok": False, "error": f"未知计算模式: {compute}"}), 400
            patch["compute"] = compute
        if "rule_workers" in data:
            try:
                patch["rule_workers"] = max(1, min(16, int(data["rule_workers"])))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "rule_workers 必须是整数"}), 400
        if not patch:
            return jsonify({"ok": False, "error": "没有可保存的字段"}), 400
        cfg = config_store.save_config(**patch)
        return jsonify({"ok": True, "config": cfg})

    @app.route("/api/desktop/restart-model-service", methods=["POST"])
    def _desktop_restart_model_service():
        """切完 CPU/GPU 用：模型服务在跑就重启（新进程才按新配置起解释器路径）。"""
        from app import _model_service  # 延迟导入避免循环

        st = _model_service.status()
        if not st.get("running"):
            return jsonify({"ok": True, "restarted": False,
                            "reason": "模型服务未在运行，下次加载时自动按新配置生效"})
        key = st.get("model") or ""
        res = _model_service.load(key, force=True) if key else _model_service.stop()
        return jsonify({"ok": True, "restarted": True, "service": res})

    # ------------------------------------------------------------------
    # 模型下载
    # ------------------------------------------------------------------
    @app.route("/api/models/download/status")
    def _models_download_status():
        return jsonify({"models": model_download.status(),
                        "models_dir": config_store.models_dir()})

    @app.route("/api/models/download", methods=["POST"])
    def _models_download_start():
        data = request.get_json(force=True, silent=True) or {}
        key = str(data.get("key") or "").strip()
        try:
            st = model_download.start(key)
        except KeyError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)}), 409
        return jsonify({"ok": True, "state": st})

    @app.route("/api/models/download/cancel", methods=["POST"])
    def _models_download_cancel():
        data = request.get_json(force=True, silent=True) or {}
        model_download.cancel(str(data.get("key") or "").strip())
        return jsonify({"ok": True})

    @app.route("/api/models/remove", methods=["POST"])
    def _models_remove():
        data = request.get_json(force=True, silent=True) or {}
        key = str(data.get("key") or "").strip()
        try:
            model_download.remove(key)
        except KeyError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)}), 409
        return jsonify({"ok": True})

    @app.route("/api/models/download/stream")
    def _models_download_stream():
        return _sse(_sse_loop(lambda: {"models": model_download.status()}))

    # ------------------------------------------------------------------
    # GPU 加速包
    # ------------------------------------------------------------------
    @app.route("/api/gpu/status")
    def _gpu_status():
        st = gpu_manager.status()
        st["backend"] = gpu_manager.backend_info()
        return jsonify(st)

    @app.route("/api/gpu/probe")
    def _gpu_probe():
        return jsonify(gpu_manager.probe())

    @app.route("/api/gpu/install", methods=["POST"])
    def _gpu_install():
        try:
            gpu_manager.install()
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)}), 409
        return jsonify({"ok": True})

    @app.route("/api/gpu/cancel", methods=["POST"])
    def _gpu_cancel():
        gpu_manager.cancel()
        return jsonify({"ok": True})

    @app.route("/api/gpu/uninstall", methods=["POST"])
    def _gpu_uninstall():
        try:
            gpu_manager.uninstall()
        except RuntimeError as e:
            return jsonify({"ok": False, "error": str(e)}), 409
        return jsonify({"ok": True})

    @app.route("/api/gpu/stream")
    def _gpu_stream():
        return _sse(_sse_loop(gpu_manager.status))

    # ------------------------------------------------------------------
    # 系统小工具
    # ------------------------------------------------------------------
    @app.route("/api/system/open-folder", methods=["POST"])
    def _system_open_folder():
        data = request.get_json(force=True, silent=True) or {}
        path = os.path.abspath(str(data.get("path") or ""))
        # 🔴 只允许开包内目录：这个口子能执行资源管理器，路径必须收紧
        allowed_roots = [config_store.PKG_ROOT]
        if not any(path == r or path.startswith(r + os.sep) for r in allowed_roots):
            return jsonify({"ok": False, "error": "只允许打开包内目录"}), 403
        if not os.path.isdir(path):
            return jsonify({"ok": False, "error": f"目录不存在: {path}"}), 400
        try:
            os.startfile(path)  # noqa: S606  Windows only，已限定包内路径
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"ok": True})
