import hashlib
import json
from pathlib import Path

from .dispatch import Dispatcher


class LocalMailbox:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def deliver(self, delivery_id: str, recipient: str, message: dict) -> str:
        key = hashlib.sha256(f"{delivery_id}\0{recipient}".encode()).hexdigest()
        path = self.directory / f"{key}.json"
        payload = json.dumps({"delivery_id": delivery_id, "recipient": recipient, "message": message}, ensure_ascii=False, sort_keys=True)
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(payload)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != payload:
                raise ValueError("同一投递编号对应了不同内容") from None
        return key

    def messages(self) -> list[dict]:
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.directory.glob("*.json"))]


def send_digest(dispatcher: Dispatcher, mailbox: LocalMailbox, run_id: str, rows: list[dict], definition: dict, recipients: list[str]) -> list[str]:
    message = dispatcher.send(run_id, rows, definition, recipients)
    return [mailbox.deliver(run_id, recipient, message) for recipient in message["recipients"]]
