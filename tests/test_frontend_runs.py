"""frontend/server.py 运行列表排序的回归测试。

背景（2026-10-16 实测）：两次带图需求运行（20261016-021302-0e03 /
20261016-021411-0911）的 run_summary.json 已正常落盘、GET /api/runs 也确实返回，
但列表里排在 45 条中的第 18/19 位——load_runs 原按 run_id 字符串倒序，
而 CLI 演示运行是 ``task01-...``~``task06-...`` 命名（``'t' > '2'``），
17 条旧运行全部压在两套日期格式运行之上，最新运行被埋到列表中段，
用户翻不到即误判「运行记录里没有对应条目」。

修复：排序键改为开始时间戳（run_summary.metrics.ts），run_id 仅兜底——
任何命名格式下最新运行都在最前。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from frontend.server import load_runs


class LoadRunsSortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)
        self.runs_dir = self.workspace / "runs"
        self.runs_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_run(self, run_id: str, ts: str, verdict: str = "fail") -> None:
        run_dir = self.runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_summary.json").write_text(
            json.dumps({
                "run_id": run_id,
                "requirement_title": "t",
                "verdict": verdict,
                "steps": 1,
                "rollbacks": 0,
                "review_rejections": 0,
                "bugs_total": 0,
                "bugs_fixed": 0,
                "metrics": {"ts": ts, "duration_s": 1.0},
            }, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_newest_first_regardless_of_run_id_format(self):
        # 复现真实场景：两条日期格式最新运行 + task0x 前缀旧运行混排
        self._write_run("20261016-021302-0e03", "2026-10-16T02:13:02+08:00")
        self._write_run("20261016-021411-0911", "2026-10-16T02:14:11+08:00")
        self._write_run("task06-tally-be", "2026-10-15T19:45:57+08:00", "pass")
        self._write_run("task01-cron-attempt01", "2026-10-14T04:59:00+08:00", "pass")
        self._write_run("20261015-125140-e8d0", "2026-10-15T12:51:40+08:00", "pass")
        runs = load_runs(self.workspace)
        self.assertEqual(
            [r["run_id"] for r in runs],
            [
                "20261016-021411-0911",   # 时间最新，尽管 '2' < 't'
                "20261016-021302-0e03",
                "task06-tally-be",
                "20261015-125140-e8d0",
                "task01-cron-attempt01",
            ],
        )

    def test_same_second_tie_broken_by_run_id_desc(self):
        self._write_run("20261016-021302-0e03", "2026-10-16T02:13:02+08:00")
        self._write_run("20261016-021302-aa01", "2026-10-16T02:13:02+08:00")
        runs = load_runs(self.workspace)
        # 时间戳相同（同一秒发起）时按 run_id 倒序兜底，保证顺序稳定可复现
        self.assertEqual([r["run_id"] for r in runs],
                         ["20261016-021302-aa01", "20261016-021302-0e03"])

    def test_entries_survive_roundtrip(self):
        # 排序不得丢条目：每条运行的判定字段原样返回
        self._write_run("20261016-021302-0e03", "2026-10-16T02:13:02+08:00")
        runs = load_runs(self.workspace)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["verdict"], "fail")
        self.assertEqual(runs[0]["status"], "已完成")
        self.assertTrue(runs[0]["finished"])


if __name__ == "__main__":
    unittest.main()
