r"""限制 RetroChimera 规则应用服务的子进程数量 —— 解决 WinError 1455「页面文件太小」。

问题
----
`retrochimera/chem/rule_application_server.py` 里：
    class RuleApplicationServer:
        def __init__(self, rulebase_dir, num_processes: int = cpu_count(), ...)
`cpu_count()` 返回 `os.cpu_count()`（本机 = 16），于是会 **spawn 16 个子进程**，
每个子进程都把完整的 reaction rulebase 载入内存，实测每个约 4.4 GB 提交内存：
    16 × 4.4 GB ≈ 70 GB   （加上主进程与搜索图 ≈ 86 GB）
本机页面文件（D:\\pagefile.sys）分配 70 GB，提交上限约 102 GB，
被这一下打满 → 之后再加载 cublas64_12.dll 就报：
    OSError: [WinError 1455] 页面文件太小，无法完成操作。

解决
----
在导入 retrochimera 之前，把 `cpu_count` 补丁成返回一个较小的值。
多步搜索是「模型调用 → 规则匹配」的串行/小批量流程，并不需要 16 个规则进程；
4 个足够，且总占用降到约 18 GB，安全落在页面文件预算内。

用法
----
在任何会触发 `RuleApplicationServer` 的脚本**最顶部**（早于 retrochimera 导入）：

    from bootstrap import limit_workers  # noqa: F401

脚本位于子目录时，先按 bootstrap/__init__.py 的说明把仓库根补进 sys.path。

可用环境变量覆盖进程数：
    set RETRO_RULE_WORKERS=4
"""
import os

# ── 允许环境变量覆盖，默认 4（16 核机器上足够，且内存可控） ──
try:
    _WORKERS = max(1, min(16, int(os.environ.get("RETRO_RULE_WORKERS", "4"))))
except ValueError:
    _WORKERS = 4

_patched = False


def _patch_cpu_count() -> None:
    """把 syntheseus 的 cpu_count 固定为 _WORKERS。"""
    global _patched
    if _patched:
        return
    try:
        from syntheseus.reaction_prediction.utils import misc as _misc

        _orig = _misc.cpu_count

        def _limited(default: int = 8) -> int:  # noqa: ARG001
            return _WORKERS

        _misc.cpu_count = _limited
        _patched = True

        # 同时兜住院子里可能存在的其它引用
        try:
            import syntheseus.reaction_prediction.utils as _u

            if hasattr(_u, "cpu_count"):
                _u.cpu_count = _limited
        except Exception:
            pass

        print(f"[bootstrap.limit_workers] cpu_count 已限制为 {_WORKERS}（原值 {_orig()}），"
              f"预计规则子进程内存约 {_WORKERS * 4.4:.0f} GB")
    except Exception as e:  # noqa: BLE001
        print(f"[bootstrap.limit_workers] 打补丁失败（将使用默认值）: {e}")


_patch_cpu_count()
