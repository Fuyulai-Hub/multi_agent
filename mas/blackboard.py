"""共享黑板：所有 Agent 交付物的版本化存储。

Agent 之间不直接互相调用，而是通过黑板共享成果：
- 每个产物名（artifact）保留全部版本（v1/v2/...），支持“谁在第几轮改了什么”的追溯；
- 下游 Agent 通过 `require()` 感知上游已完成的工作，缺失即暴露；
- 门禁表达式直接在 `snapshot()`（最新版本快照）上求值。

分布式共享（多进程/多实例）保证：
- 版本号按「磁盘上已有 v*.json 的最大版本 + 1」计算，两个进程同时发布同一产物
  不会都写成 v1 互相覆盖；
- publish 的写文件过程在跨进程文件锁保护下完成（见 mas.filelock）；
- `get()/has()` 在进程内缓存未命中时回落读磁盘 latest.json，
  使其他进程新发布的产物对本进程可见（最终一致，读取路径无锁、保证可用性）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .events import EventLog
from .filelock import file_lock

__all__ = ["ArtifactRef", "Blackboard"]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class ArtifactRef:
    name: str
    version: int
    producer: str
    node: str
    iteration: int
    path: Path
    digest: str
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "producer": self.producer,
            "node": self.node,
            "iteration": self.iteration,
            "path": str(self.path),
            "digest": self.digest,
            "created_at": self.created_at,
        }


class Blackboard:
    """基于文件系统的版本化产物存储。"""

    def __init__(self, root: Path, events: Optional[EventLog] = None, run_id: str = "") -> None:
        self.root = Path(root)
        self.events = events
        self.run_id = run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._latest: Dict[str, Any] = {}
        self._refs: Dict[str, List[ArtifactRef]] = {}

    # ---------------------------------------------------------------- 写入
    def publish(
        self,
        name: str,
        data: Any,
        *,
        producer: str,
        node: str,
        iteration: int,
        summary: str = "",
    ) -> ArtifactRef:
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(data, ensure_ascii=False, indent=2, default=str)
        # 跨进程安全：锁内完成「版本号计算 + 落盘」，避免多实例并发发布相互覆盖
        with file_lock(directory / "latest.json"):
            version = self._next_version(directory)
            path = directory / f"v{version}.json"
            path.write_text(serialized, encoding="utf-8")
            (directory / "latest.json").write_text(serialized, encoding="utf-8")

        ref = ArtifactRef(
            name=name,
            version=version,
            producer=producer,
            node=node,
            iteration=iteration,
            path=path,
            digest=hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12],
            created_at=_now(),
        )
        self._refs.setdefault(name, []).append(ref)
        self._latest[name] = data
        if self.events is not None:
            self.events.emit(
                "artifact_published",
                agent=producer,
                node=node,
                iteration=iteration,
                payload={"artifact": name, "version": version, "digest": ref.digest, "summary": summary},
                refs=[str(path)],
            )
        return ref

    @staticmethod
    def _next_version(directory: Path) -> int:
        """扫描磁盘上已有版本文件取 max+1（进程间不会重复计数，替代进程内 len(refs)+1）。"""
        versions = [
            int(match.group(1))
            for path in directory.glob("v*.json")
            for match in [re.fullmatch(r"v(\d+)\.json", path.name)]
            if match
        ]
        return (max(versions) + 1) if versions else 1

    # ---------------------------------------------------------------- 读取
    def has(self, name: str) -> bool:
        if name in self._latest:
            return True
        return self._load_from_disk(name) is not None

    def get(self, name: str, default: Any = None) -> Any:
        if name in self._latest:
            return self._latest[name]
        # 进程内未命中：回落磁盘（其他进程/实例可能刚发布过该产物）
        data = self._load_from_disk(name)
        if data is not None:
            self._latest[name] = data
            return data
        return default

    def _load_from_disk(self, name: str) -> Any:
        """读取其他进程发布的 latest.json；不存在或损坏时返回 None（不抛异常）。"""
        path = self.root / name / "latest.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def require(self, names: List[str]) -> Tuple[Dict[str, Any], List[str]]:
        found: Dict[str, Any] = {}
        missing: List[str] = []
        for name in names:
            data = self.get(name)
            if data is None and name not in self._latest:
                missing.append(name)
            else:
                found[name] = data
        return found, missing

    def snapshot(self) -> Dict[str, Any]:
        """最新版本快照（门禁表达式求值用）；含磁盘回落，多进程发布对本进程可见。"""
        merged = dict(self._latest)
        if self.root.exists():
            for directory in self.root.iterdir():
                if directory.is_dir() and directory.name not in merged:
                    data = self._load_from_disk(directory.name)
                    if data is not None:
                        merged[directory.name] = data
        return merged

    def versions(self, name: str) -> List[ArtifactRef]:
        return list(self._refs.get(name, []))

    def names(self) -> List[str]:
        return sorted(self._latest)

    def brief(self, names: Optional[List[str]] = None, max_chars_per_item: int = 900) -> str:
        """生成紧凑的产物摘要，供 Prompt 的“上游感知”段落使用。"""
        chunks: List[str] = []
        for name in names or self.names():
            if name not in self._latest:
                continue
            refs = self._refs.get(name, [])
            version = refs[-1].version if refs else 1
            payload = json.dumps(self._latest[name], ensure_ascii=False, default=str)
            if len(payload) > max_chars_per_item:
                payload = payload[:max_chars_per_item] + f"...(截断, 共 {len(payload)} 字符)"
            producer = refs[-1].producer if refs else "-"
            chunks.append(f"### 产物 {name} (v{version}, 由 {producer} 产出)\n{payload}")
        return "\n\n".join(chunks) if chunks else "(暂无上游产物)"

    def missing_report(self, names: List[str]) -> str:
        missing = [name for name in names if name not in self._latest]
        return "、".join(missing) if missing else ""
