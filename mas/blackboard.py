"""共享黑板：所有 Agent 交付物的版本化存储。

Agent 之间不直接互相调用，而是通过黑板共享成果：
- 每个产物名（artifact）保留全部版本（v1/v2/...），支持“谁在第几轮改了什么”的追溯；
- 下游 Agent 通过 `require()` 感知上游已完成的工作，缺失即暴露；
- 门禁表达式直接在 `snapshot()`（最新版本快照）上求值。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .events import EventLog

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
        version = len(self._refs.get(name, [])) + 1
        path = directory / f"v{version}.json"
        serialized = json.dumps(data, ensure_ascii=False, indent=2, default=str)
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

    # ---------------------------------------------------------------- 读取
    def has(self, name: str) -> bool:
        return name in self._latest

    def get(self, name: str, default: Any = None) -> Any:
        return self._latest.get(name, default)

    def require(self, names: List[str]) -> Tuple[Dict[str, Any], List[str]]:
        found = {name: self._latest[name] for name in names if name in self._latest}
        missing = [name for name in names if name not in self._latest]
        return found, missing

    def snapshot(self) -> Dict[str, Any]:
        return dict(self._latest)

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
