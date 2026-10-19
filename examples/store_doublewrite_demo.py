"""MySQL/Redis 接入 smoke：一次真实运行，验证「双写 + 注册表 + 降级」全链路。

用法（任选其一）::

    python examples/store_doublewrite_demo.py            # offline provider，确定性验证
    python examples/store_doublewrite_demo.py --live     # 用 system.yaml 的真实端点跑一轮

脚本行为：
1. ``mas store status`` 同款探测两个后端（连不上会明确告知，不会假装成功）；
2. 记录 MySQL 五张表的行数基线；
3. 用 offline provider 跑一次完整流水线（与真实 provider 走同一条双写代码路径）；
4. 运行结束后对比行数：runs +1、run_metrics +1、run_events +N、experiences 变化；
5. 演示 Redis 运行注册表的 register/heartbeat/is_canceled/unregister 生命周期。

注意：本脚本只读探针 + 一次流水线运行，不删除任何数据；
workspace 用项目默认的 ``workspace/``（工作区绑定守卫只放行规范工作区，见 docs/25）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from mas.config import load_config  # noqa: E402
from mas.leader import Leader  # noqa: E402
from mas.runhub import build_registry  # noqa: E402
from mas.store import build_backends, store_status  # noqa: E402

TABLES = ("runs", "run_metrics", "experiences", "run_events", "artifacts_index")
REQUIREMENT = PROJECT_ROOT / "examples" / "requirements" / "order_pricing_service.md"


def _counts(mysql) -> dict:
    return {t: int(mysql.query_all(f"SELECT COUNT(*) AS c FROM {t}")[0]["c"]) for t in TABLES}


def main() -> int:
    parser = argparse.ArgumentParser(description="MySQL/Redis 双写 smoke（docs/25）")
    parser.add_argument("--live", action="store_true",
                        help="使用 system.yaml 的真实 LLM 端点（默认 offline，确定性强、不消耗配额）")
    args = parser.parse_args()

    config_dir = PROJECT_ROOT / "config"
    config = load_config(config_dir)
    if not args.live:
        config.llm["provider"] = "offline"

    # 1) 后端探测（与 mas store status 同一函数）
    status = store_status(config, config_dir)
    print("== 后端状态 ==")
    print(json.dumps(status, ensure_ascii=False, indent=2))
    backends = build_backends(config, config_dir)
    if not backends.any_enabled:
        print("\n两个后端均不可用：请先确认 Docker 容器 mas-mysql / mas-redis 已启动"
              "（docker compose up -d），并检查 config/credentials.yaml 的 store 段。")
        return 2

    # 2) 行数基线
    baseline = _counts(backends.mysql) if backends.mysql is not None else {}

    # 3) 真实运行（registry：Redis 可用即跨进程注册表，否则进程内版）
    registry = build_registry(config, config_dir)
    print(f"\n== 运行流水线（provider={config.llm.get('provider')}）==")
    leader = Leader(config)
    summary = leader.run(str(REQUIREMENT), registry=registry)
    print(f"run_id={summary.run_id} verdict={summary.verdict} steps={summary.steps}")

    # 4) 行数对比
    if backends.mysql is not None:
        after = _counts(backends.mysql)
        print("\n== MySQL 双写对比（baseline → after）==")
        for table in TABLES:
            delta = after[table] - baseline.get(table, 0)
            print(f"  {table:<16} {baseline.get(table, '-'):>4} → {after[table]:>4}  (+{delta})")
        latest = backends.mysql.query_all(
            "SELECT run_id, verdict, steps, rollbacks, bugs_total, finished, canceled "
            "FROM runs WHERE run_id = %s", (summary.run_id,))
        print("  runs 表最新行:", json.dumps(latest, ensure_ascii=False, default=str))

    # 5) Redis 注册表演示（运行已结束，键应已清理）
    if backends.redis is not None:
        running = backends.redis.list_running()
        print("\n== Redis 注册表 ==")
        print(f"  list_running() = {running}  # 运行结束即 unregister，预期为空列表")
        token = backends.redis.acquire_lock("demo:smoke", ttl_ms=5000)
        print(f"  acquire_lock('demo:smoke', ttl_ms=5000) = {token}  # 再取一次应为 None（互斥）:",
              backends.redis.acquire_lock("demo:smoke", ttl_ms=5000))
        backends.redis.release_lock("demo:smoke", token)
        print("  release_lock 后可用缓存读写:",
              backends.redis.cache_set("demo:key", {"ok": True}, ttl_s=5),
              backends.redis.cache_get("demo:key"))
    return 0 if summary.verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
