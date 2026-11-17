"""本地邮箱：按投递编号与收件人落盘的 JSON 邮箱，无外部邮件平台。

不变量：
- 同一 (delivery_id, recipient) 的内容必须字节一致，复用编号投递不同内容会被拒绝；
- 写入是原子的（临时文件 + rename），进程中断不会留下半截消息；
- 重复投递完全幂等，重开目录后历史回执仍然有效。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


def canonical_json(payload: dict | list) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def content_hash(payload: dict | list) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class LocalMailbox:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path_for(self, delivery_id: str, recipient: str) -> Path:
        key = hashlib.sha256(f"{delivery_id}\0{recipient}".encode()).hexdigest()
        return self.directory / f"{key}.json"

    def deliver(self, delivery_id: str, recipient: str, message: dict) -> str:
        path = self._path_for(delivery_id, recipient)
        envelope = {
            "delivery_id": delivery_id,
            "recipient": recipient,
            "content_hash": content_hash(message),
            "message": message,
        }
        payload = json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2)
        try:
            self._atomic_write(path, payload)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != payload:
                raise ValueError("同一投递编号对应了不同内容") from None
        return path.stem

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        # 临时文件与目标同目录，rename 才是原子的；崩溃残留的临时文件先清理。
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                        dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            # 硬链接原子创建：目标已存在时抛 FileExistsError，绝不静默覆盖。
            os.link(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def messages(self) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(self.directory.glob("*.json"))]

    def find(self, delivery_id: str, recipient: str | None = None) -> list[dict]:
        found = []
        for envelope in self.messages():
            if envelope["delivery_id"] != delivery_id:
                continue
            if recipient is None or envelope["recipient"] == recipient:
                found.append(envelope)
        return found

    def recipients_for(self, delivery_id: str) -> list[str]:
        return sorted(envelope["recipient"] for envelope in self.find(delivery_id))
