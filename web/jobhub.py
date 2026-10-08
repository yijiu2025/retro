# -*- coding: utf-8 -*-
"""任务中心 —— 让「正在分析」变成「分析到哪一步了」。

背景
----
/api/predict 是一个同步长任务：加载 4.4 GB 模型（首次可达十几分钟）+ 推理，
前端只能干等，用户看到的就是一个转圈。这个模块把它改造成**可查询的后台任务**：

    POST /api/jobs                  → 立即返回 job_id
    GET  /api/jobs/<id>?since=N     → 状态快照（阶段时间线 / 增量日志 / 进度）
    POST /api/jobs/<id>/cancel      → 请求取消

顺带把模型内部的子步骤暴露出来。RetroChimera 的执行结构（retrochimera 1.3.0）：

    RetroChimeraModel.__init__      → 逐个构造子模型（models.json 里 2 个）
        └─ 每个子模型 __init__
             ├─ start_server()      → RuleApplicationServer：spawn N 个子进程加载规则库（最慢）
             └─ load_from_checkpoint(...)
    RetroChimeraModel.__call__      → 逐个调子模型推理 → combine_results 融合排序
        └─ ReactionModel.__call__ → self._get_reactions(...)   ← syntheseus 的必经之路

所以能观测到「加载子模型 1/2」「规则子进程就绪」「子模型推理 1/2」这种粒度，
而不是干巴巴一个「运行中」。

🔴 hook 方式的选择（别改成复制内部实现）：
   加载 → 替换 `retrochimera.inference.retrochimera.lookup_by_name`，返回一个**子类**，
   子类只加「构造前后各报一次」，内部逻辑一次都不抄 ⇒ 上游改实现也不会失同步。
   推理 → 给**实例**挂 `_get_reactions` 包装（实例属性优先于类属性），
   不改类、不影响缓存语义（`__call__` → `_cached_call` → `self._get_reactions` 原样走）。
"""
from __future__ import annotations

import io
import logging
import os
import threading
import time
import traceback
import types
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import redirect_stdout
from typing import Any, Callable, Optional

MAX_LOGS = 500
JOB_TTL_SECONDS = float(os.environ.get("RETRO_JOB_TTL", "3600"))

# 阶段权重（用于估算百分比；skipped 的阶段不计入分母）
WEIGHTS = {
    "validate": 0.03,
    "load": 0.62,
    "infer": 0.30,
    "post": 0.05,
}


class Cancelled(Exception):
    """任务被用户取消。"""


# ---------------------------------------------------------------------------
# 日志捕获
# ---------------------------------------------------------------------------
class _JobLogHandler(logging.Handler):
    """把 job 运行期间产生的 logging 记录搬进 job.logs。"""

    def emit(self, record: logging.LogRecord) -> None:
        job = current_job()
        if job is None:
            return
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001
            text = record.getMessage()
        job.log(text, level=record.levelname.lower())


class _StdoutTap(io.TextIOBase):
    """把 print 输出搬进 job.logs（只接 stdout；stderr 交给 logging / 异常流程）。"""

    def __init__(self, job: "Job") -> None:
        self._job = job
        self._buf = ""

    def write(self, s: str) -> int:  # noqa: D102
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._job.log(line.rstrip(), level="info")
        return len(s)

    def flush(self) -> None:  # noqa: D102
        if self._buf.strip():
            self._job.log(self._buf.rstrip(), level="info")
        self._buf = ""


_handler_installed = False


def _install_log_handler() -> None:
    global _handler_installed
    if _handler_installed:
        return
    h = _JobLogHandler()
    h.setLevel(logging.INFO)
    h.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    logging.getLogger().addHandler(h)
    _handler_installed = True


# ---------------------------------------------------------------------------
# 当前线程正在跑的 job（hook 回调靠它找到目标）
# ---------------------------------------------------------------------------
_CTX = threading.local()


def current_job() -> Optional["Job"]:
    return getattr(_CTX, "job", None)


class Job:
    """一个后台预测任务。所有字段都用锁保护（轮询线程会并发读）。"""

    def __init__(self, job_id: str, kind: str, params: dict) -> None:
        self.id = job_id
        self.kind = kind
        self.params = params
        self.state = "queued"  # queued|running|succeeded|failed|cancelled
        self.stages: list[dict] = []
        self.logs: list[dict] = []
        self.log_seq = 0  # 单调递增，前端用它做增量拉取游标
        self.result: Optional[dict] = None
        self.error: Optional[str] = None
        self.error_reasons: list[str] = []
        self.error_payload: Optional[dict] = None
        self.created_at = time.time()
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.cancel_requested = False
        self.cancelable = True
        self._lock = threading.Lock()

    # -- 日志 ---------------------------------------------------------------
    def log(self, text: str, level: str = "info") -> None:
        with self._lock:
            self.log_seq += 1
            self.logs.append(
                {
                    "seq": self.log_seq,
                    "t": round(time.time() - (self.started_at or self.created_at), 1),
                    "level": level,
                    "text": text,
                }
            )
            if len(self.logs) > MAX_LOGS:
                del self.logs[: len(self.logs) - MAX_LOGS]

    # -- 阶段 ---------------------------------------------------------------
    def begin_stage(self, key: str, label: str, detail: str = "", weight: Optional[float] = None) -> None:
        """开始一个阶段；上一个未收尾的阶段自动标记完成。"""
        with self._lock:
            for s in self.stages:
                if s["state"] == "running":
                    s["state"] = "done"
                    s["seconds"] = round(time.time() - s["_t0"], 2)
            self.stages.append(
                {
                    "key": key,
                    "label": label,
                    "detail": detail,
                    "state": "running",
                    "sub": 0.0,
                    "seconds": 0.0,
                    "weight": weight if weight is not None else WEIGHTS.get(key.split(":")[0], 0.1),
                    "_t0": time.time(),
                }
            )
        self.log(f"▸ {label}" + (f" — {detail}" if detail else ""), level="stage")

    def end_stage(self, detail: str = "") -> None:
        with self._lock:
            for s in self.stages:
                if s["state"] == "running":
                    s["state"] = "done"
                    s["seconds"] = round(time.time() - s["_t0"], 2)
                    if detail:
                        s["detail"] = detail

    def set_detail(self, detail: str, sub: Optional[float] = None) -> None:
        with self._lock:
            for s in self.stages:
                if s["state"] == "running":
                    s["detail"] = detail
                    if sub is not None:
                        s["sub"] = max(0.0, min(1.0, sub))
        self.log(f"  {detail}", level="info")

    def skip_stage(self, key: str, label: str, detail: str = "") -> None:
        """记录一个被跳过的阶段（例如模型已加载）。"""
        with self._lock:
            self.stages.append(
                {
                    "key": key,
                    "label": label,
                    "detail": detail,
                    "state": "skipped",
                    "sub": 1.0,
                    "seconds": 0.0,
                    "weight": WEIGHTS.get(key, 0.1),
                    "_t0": time.time(),
                }
            )
        self.log(f"· {label}（{detail or '跳过'}）", level="stage")

    def check_cancel(self) -> None:
        if self.cancel_requested:
            raise Cancelled("任务已被取消")

    # -- 快照 ---------------------------------------------------------------
    def _progress_unlocked(self) -> float:
        """计算百分比（**调用者必须已持有 self._lock**）。

        🔴 为什么拆出这个「无锁版」：`snapshot()` 自己在 `with self._lock:` 里，
        若在那里直接调 `progress()`，它会**再次**获取同一把非重入锁 ⇒ 同线程自锁死，
        请求线程永久挂起（正是 GET /api/jobs/<id> 无限超时的真凶）。
        """
        active = [s for s in self.stages if s["state"] != "skipped"]
        if not active:
            return 0.0
        total = sum(s["weight"] for s in active) or 1.0
        done = sum(s["weight"] for s in active if s["state"] == "done")
        cur = next((s for s in active if s["state"] == "running"), None)
        p = done + (cur["weight"] * cur["sub"] * 0.95 if cur else 0.0)
        return round(max(0.0, min(0.99, p / total)), 4)

    def progress(self) -> float:
        with self._lock:
            return self._progress_unlocked()

    def snapshot(self, since: int = 0) -> dict:
        with self._lock:
            now = time.time()
            stages = [
                {k: v for k, v in s.items() if not k.startswith("_")} for s in self.stages
            ]
            cur = next((s for s in stages if s["state"] == "running"), None)
            logs = [l for l in self.logs if l["seq"] > since]
            elapsed = round(now - (self.started_at or self.created_at), 1)
            out = {
                "job_id": self.id,
                "kind": self.kind,
                "state": self.state,
                "progress": 1.0 if self.state == "succeeded" else self._progress_unlocked(),
                "current": cur["label"] if cur else "",
                "current_detail": cur.get("detail", "") if cur else "",
                "elapsed": elapsed,
                "queued_for": round((self.started_at or now) - self.created_at, 1),
                "cancelable": self.cancelable and self.state in ("queued", "running"),
                "stages": stages,
                "logs": logs,
                "log_seq": self.log_seq,
            }
            if self.state == "succeeded":
                out["result"] = self.result
            if self.state == "failed":
                out["error"] = self.error
                out["error_reasons"] = self.error_reasons
                out["error_payload"] = self.error_payload
            if self.state == "cancelled":
                out["error"] = self.error or "已取消"
            return out


# ---------------------------------------------------------------------------
# 注册表 + 串行 worker
# ---------------------------------------------------------------------------
_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()
# 模型加载吃内存（4.4 GB 规则库 + 若干子进程），任务必须串行，绝不能并发
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="retrojob")


def set_executor(exc) -> None:
    """允许宿主注入执行器（例如「主线程执行器」）。

    🔴 为什么需要：Windows 上 multiprocessing 用 spawn 起规则子进程，
    而从「非主线程」spawn 会死锁（子进程 re-import 主模块时卡死）。
    Flask 的请求处理在后台线程里跑，如果预测任务（会 spawn 子进程）也在
    后台线程执行，就会触发这个死锁 ⇒ 模型永远加载不出来、内存不涨、
    GET /api/jobs 无限挂起。

    解决办法：把任务的真正执行放到**主线程**（见 web/app.py 的
    MainThreadExecutor + 主线程 pump 循环），spawn 发生在主线程就安全了。
    """
    global _executor
    _executor = exc


def get_job(job_id: str) -> Optional[Job]:
    with _jobs_lock:
        return _jobs.get(job_id)


def submit(kind: str, params: dict, runner: Callable[[Job], dict]) -> Job:
    job = Job(uuid.uuid4().hex[:12], kind, params)
    with _jobs_lock:
        _jobs[job.id] = job
        _reap_locked()

    def _run() -> None:
        _CTX.job = job
        job.started_at = time.time()
        job.state = "running"
        # 🔴 不再在这里装模型进度 hook：hook 打补丁的是「模型所在的进程」，
        #    而模型现在跑在独立 worker 进程里（见 web/app.py），本进程（Flask/
        #    主线程 pump）装了也是针对另一份 import 空间，对 worker 里的模型毫无作用，
        #    反而会在主线程首次 import retrochimera/torch/syntheseus，拖慢并饿死
        #    GET /api/jobs 轮询（之前「模型好像没加载 / GET 挂起」的真凶之一）。
        #    细粒度进度改为 coarse 阶段（validate/infer/post）即可，足够前端展示。
        _install_log_handler()
        tap = _StdoutTap(job)
        try:
            with redirect_stdout(tap):
                job.result = runner(job)
            if job.cancel_requested:
                raise Cancelled("任务已被取消")
            job.state = "succeeded"
        except Cancelled as e:
            job.state = "cancelled"
            job.error = str(e) or "已取消"
            job.log(f"✕ {job.error}", level="warn")
        except Exception as e:  # noqa: BLE001
            job.state = "failed"
            job.error = f"{type(e).__name__}: {e}"
            job.log(traceback.format_exc(), level="error")
            traceback.print_exc()
        finally:
            with job._lock:
                for s in job.stages:
                    if s["state"] == "running":
                        s["state"] = "done" if job.state == "succeeded" else "failed"
                        s["seconds"] = round(time.time() - s["_t0"], 2)
            job.finished_at = time.time()
            tap.flush()
            _CTX.job = None

    _executor.submit(_run)
    return job


def _reap_locked() -> None:
    """清掉过期任务，避免长时间运行后内存里堆满快照。"""
    now = time.time()
    dead = [
        jid
        for jid, j in _jobs.items()
        if j.finished_at and now - j.finished_at > JOB_TTL_SECONDS
    ]
    for jid in dead:
        _jobs.pop(jid, None)
    # 兜底：任务数异常膨胀时按创建时间淘汰
    if len(_jobs) > 200:
        for jid, _ in sorted(_jobs.items(), key=lambda kv: kv[1].created_at)[: len(_jobs) - 200]:
            _jobs.pop(jid, None)


def cancel(job_id: str) -> bool:
    job = get_job(job_id)
    if job is None or job.state not in ("queued", "running"):
        return False
    job.cancel_requested = True
    job.log("收到取消请求（在下一个阶段边界生效；模型加载/推理本身不可中断）", level="warn")
    return True


# ---------------------------------------------------------------------------
# 模型内部 hook
# ---------------------------------------------------------------------------
_hooks_installed = False
_hooks_lock = threading.Lock()


def install_model_hooks() -> None:
    """给 RetroChimera 装上进度上报（幂等，只做装饰不复制内部逻辑）。"""
    global _hooks_installed
    with _hooks_lock:
        if _hooks_installed:
            return
        try:
            from retrochimera.chem import rule_application_server as ras_mod
            from retrochimera.inference import retrochimera as rc_mod
        except Exception as e:  # noqa: BLE001
            print(f"[jobhub] 进度 hook 未安装（{e}），将退化为只有粗粒度阶段", flush=True)
            _hooks_installed = True
            return

        # --- 1) 子模型加载：包装 lookup_by_name，返回一个会报进度的子类 -------
        orig_lookup = rc_mod.lookup_by_name

        def patched_lookup(name, *a, **k):  # type: ignore[no-untyped-def]
            cls = orig_lookup(name, *a, **k)
            label = str(name)

            class _Progress(cls):  # type: ignore[misc,valid-type]
                def __init__(self, *args, **kwargs):  # noqa: D107
                    job = current_job()
                    if job is not None:
                        job.set_detail(f"加载子模型 {label}（读取权重 / 起规则服务器）")
                    super().__init__(*args, **kwargs)
                    _wrap_inference(self, label)
                    if job is not None:
                        job.log(f"  ✓ 子模型 {label} 就绪", level="info")

            _Progress.__name__ = getattr(cls, "__name__", label)
            _Progress.__qualname__ = _Progress.__name__
            _Progress.__module__ = getattr(cls, "__module__", __name__)
            return _Progress

        rc_mod.lookup_by_name = patched_lookup  # type: ignore[assignment]

        # --- 2) 规则服务器：起子进程前后各报一次 -----------------------------
        RAS = ras_mod.RuleApplicationServer
        orig_init = RAS.__init__
        orig_start = getattr(RAS, "_start_processes", None)

        def patched_init(self, *a, **k):  # type: ignore[no-untyped-def]
            job = current_job()
            if job is not None:
                job.set_detail("启动规则应用服务器（spawn 子进程加载规则库，通常最慢）")
            orig_init(self, *a, **k)
            if job is not None:
                n = getattr(self, "_num_processes", "?")
                job.set_detail(f"规则服务器就绪（{n} 个子进程）")

        RAS.__init__ = patched_init  # type: ignore[assignment]

        if orig_start is not None:

            def patched_start(self, process_ids):  # type: ignore[no-untyped-def]
                out = orig_start(self, process_ids)
                job = current_job()
                if job is not None:
                    total = getattr(self, "_num_processes", 0)
                    job.set_detail(
                        f"规则子进程已启动，等待规则库加载就绪（{len(process_ids)}/{total}）"
                    )
                return out

            RAS._start_processes = patched_start  # type: ignore[assignment]

        _hooks_installed = True
        print("[jobhub] 模型进度 hook 已安装", flush=True)


def _wrap_inference(model: Any, label: str) -> None:
    """包装实例级 `_get_reactions`，让每次推理都产生一个可见阶段。"""
    try:
        orig = getattr(type(model), "_get_reactions")
    except Exception:  # noqa: BLE001
        return

    def patched(self, inputs, num_results):  # type: ignore[no-untyped-def]
        job = current_job()
        n_in = len(inputs) if inputs is not None else 0
        if job is not None:
            job.check_cancel()
            job.begin_stage(
                f"infer:{label}",
                f"子模型推理 · {label}",
                f"{n_in} 个分子 × Top{num_results}",
            )
        try:
            return orig(self, inputs, num_results)
        finally:
            if job is not None:
                job.end_stage(detail=f"{n_in} 个分子完成")

    model._get_reactions = types.MethodType(patched, model)


# ---------------------------------------------------------------------------
# 预测任务的 runner
# ---------------------------------------------------------------------------
def make_predict_runner(
    *,
    validate_target_smiles: Callable[[str], tuple],
    predict_fn: Callable[[str, str, int], list],
    model_keys: set,
) -> Callable[[Job], dict]:
    """构造 /api/jobs(kind=predict) 的执行体。依赖从 app 注入，避免循环 import。

    🔴 `predict_fn(smiles, model_key, num_results)` 是「推理桥」：
       真正的模型放在**独立的单线程 worker 进程**里（见 web/app.py），
       这里只是把请求丢进队列、等结果回来。这样本进程（Flask）永远不调用
       `multiprocessing` 起子进程，彻底绕开 Windows 上「多线程进程里 spawn 死锁」。
       worker 返回的是已序列化的候选列表：[{precursors, probability}, ...]
    """

    def runner(job: Job) -> dict:
        smiles = str(job.params.get("smiles") or "").strip()
        model_key = job.params.get("model", "pistachio")
        num_results = int(job.params.get("num_results", 5))

        # 1) 校验
        job.begin_stage("validate", "校验分子结构", smiles[:60])
        if not smiles:
            raise ValueError("请输入目标分子的结构或 SMILES")
        if model_key not in model_keys:
            raise ValueError(f"未知模型: {model_key}")
        num_results = max(1, min(num_results, 20))
        ok_smi, smiles, err = validate_target_smiles(smiles)
        job.check_cancel()
        if not ok_smi:
            # 结构化失败：把人话诊断原样带给前端
            job.error_payload = err or {"ok": False, "error": "分子不合法"}
            job.error = (err or {}).get("error", "分子不合法")
            job.error_reasons = list((err or {}).get("reasons") or [])
            raise ValueError(job.error)
        job.end_stage(detail=smiles)
        job.log(f"目标分子已规范化：{smiles}", level="info")

        # 2) 模型跑在**独立常驻服务**里（web/model_server.py），本进程不加载。
        #    🔴 这里以前写「独立进程已预加载，跳过」—— 现在模型可能还在后台加载
        #        （API 是秒起的），那样说就是撒谎。如实描述：加载在哪、进度看
        #        /api/models。若真没就绪，predict_fn 会抛一句说清原因的话。
        job.skip_stage("load", "加载模型", "模型在独立常驻服务里，进度见 /api/models")

        # 3) 推理（经队列交给 worker 进程；worker 复用已起的规则子进程，不再 spawn）
        job.begin_stage("infer", "单步逆合成推理", f"Top{num_results}")
        t1 = time.time()
        raw = predict_fn(smiles, model_key, num_results)
        infer_s = time.time() - t1
        job.end_stage(detail=f"{len(raw)} 个候选，用时 {infer_s:.1f}s")
        job.check_cancel()

        # 4) 后处理（worker 已返回纯数据，这里只整理成前端需要的形状）
        job.begin_stage("post", "整理候选与概率", "")
        results = []
        for i, c in enumerate(raw, 1):
            # 🔴 syntheseus（retrochimera 1.3.0）返回的 prediction 是**反应 SMILES**：
            #    形如 "reactants>>product"（如 "CC(=O)OC(C)=O.O=C(O)c1ccccc1O>>CC(=O)Oc1ccccc1C(=O)O"）。
            #    逆合成里我们关心的是**反应物一侧**（前体）。若直接对整个反应串 split(".")，
            #    最后一个片段会带上 "...>>产品"，下游（前端建树 / 校验脚本）会把它当成
            #    非法分子（"这个 SMILES 无法被识别为合法分子"）。所以先按 ">>" 切出反应物侧。
            rxn = str(c.get("reaction") or c["precursors"])
            prob = float(c["probability"])
            left, sep, right = rxn.partition(">>")
            reactant_str = left if sep else rxn
            parts = [p for p in reactant_str.split(".") if p]
            results.append(
                {
                    "rank": i,
                    "precursors": reactant_str,  # 反应物侧（前端展示 / 建树都用它）
                    "precursor_list": parts,     # 拆分后的前体分子列表
                    "reaction": rxn,             # 完整反应 SMILES（供需要时展示）
                    "product": right if sep else "",
                    "probability": prob,
                    "num_molecules": len(parts),
                    "level": "high" if prob > 0.5 else ("mid" if prob > 0.1 else "low"),
                }
            )
        job.end_stage(detail=f"{len(results)} 个候选")

        return {
            "ok": True,
            "product": smiles,
            "model": model_key,
            "results": results,
            "infer_seconds": round(infer_s, 1),
        }

    return runner


def make_search_runner(
    *,
    validate_target_smiles: Callable[[str], tuple],
    search_fn: Callable[[dict], dict],
    model_keys: set,
) -> Callable[[Job], dict]:
    """构造 /api/jobs(kind=search) 的执行体。与 predict 的 runner 对称。

    🔴 `search_fn(params)` 是「搜索桥」：真正的 RetroStar 搜索在**独立 worker 进程**
        里执行（复用那里已经加载的模型，不在 Flask 进程里再载一份 —— 否则内存翻倍，
        冷启动那 126s 就是第二份模型在加载）。worker 返回的是已序列化的路线列表 +
        起始物料 + 统计。本进程（Flask/主线程 pump）只做 IPC，绝不 spawn 子进程。

    参数契约与同步 /api/search 一致（见 app.py api_search）：
      params = {smiles, model, max_routes, max_calls, time_limit, max_depth,
                max_heavy_atoms, value_fn, purchasable, required_materials}
    """

    def runner(job: Job) -> dict:
        smiles = str(job.params.get("smiles") or "").strip()
        model_key = job.params.get("model", "pistachio")

        # 1) 校验
        job.begin_stage("validate", "校验分子结构", smiles[:60])
        if not smiles:
            raise ValueError("请输入目标分子的结构或 SMILES")
        if model_key not in model_keys:
            raise ValueError(f"未知模型: {model_key}")
        ok_smi, smiles, err = validate_target_smiles(smiles)
        job.check_cancel()
        if not ok_smi:
            job.error_payload = err or {"ok": False, "error": "分子不合法"}
            job.error = (err or {}).get("error", "分子不合法")
            job.error_reasons = list((err or {}).get("reasons") or [])
            raise ValueError(job.error)
        job.end_stage(detail=smiles)
        job.log(f"目标分子已规范化：{smiles}", level="info")

        # 2) 模型跑在**独立常驻服务**里（web/model_server.py），本进程不加载。
        #    🔴 这里以前写「独立进程已预加载，跳过」—— 与 predict runner 同一个谎：
        #        API 是秒起的，模型可能还在后台加载。如实描述加载在哪、进度看哪。
        #        若真没就绪，search_fn 会抛一句说清原因的话（不会被静默吞掉）。
        job.skip_stage("load", "加载模型", "模型在独立常驻服务里，进度见 /api/models")

        # 3) 搜索在**模型服务进程**里跑（HTTP 转接，复用那里已加载的模型）。
        #    search 是唯一的重活，给它 0.92 的权重（validate/post 各占一点）；
        #    load 被 skip 掉不计入分母，所以 search 要拿掉进度条的大头。
        job.begin_stage("search", "多步路线搜索", "Retro* 广度优先 · 吃满调用预算", weight=0.92)
        t1 = time.time()
        result = search_fn(
            {
                "smiles": smiles,
                "model": model_key,
                "max_routes": int(job.params.get("max_routes", 5)),
                "max_calls": int(job.params.get("max_calls", 60)),
                "time_limit": float(job.params.get("time_limit", 120)),
                "max_depth": int(job.params.get("max_depth", 3)),
                "max_heavy_atoms": int(job.params.get("max_heavy_atoms", 10)),
                "value_fn": str(job.params.get("value_fn") or "complexity"),
                "purchasable": job.params.get("purchasable") or [],
                "required_materials": job.params.get("required_materials") or [],
            }
        )
        search_s = time.time() - t1
        job.check_cancel()
        n_routes = len(result.get("routes") or [])
        job.end_stage(detail=f"{n_routes} 条路线，用时 {search_s:.1f}s")

        # 4) 后处理：worker 已返回纯数据，这里只补一层「本次搜索的元信息」
        job.begin_stage("post", "整理路线与起始物料", "")
        result.setdefault("ok", True)
        result["search_seconds"] = round(search_s, 1)
        job.end_stage(detail=f"{n_routes} 条路线")

        return result

    return runner


_LOADED_REF: dict = {}


def bind_loaded_cache(cache: dict) -> None:
    """把 app 的模型缓存字典挂进来，用于判断「这次要不要加载」。"""
    global _LOADED_REF
    _LOADED_REF = cache


def _peek_loaded() -> dict:
    return _LOADED_REF


def wait(job_id: str, timeout: float = 900.0, poll: float = 0.2) -> Optional[Job]:
    """同步等待任务结束（供兼容用的同步 /api/predict 使用）。"""
    end = time.time() + timeout
    while time.time() < end:
        job = get_job(job_id)
        if job is None or job.state in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(poll)
    return get_job(job_id)
