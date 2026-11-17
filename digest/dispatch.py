from .metrics import calculate


class Dispatcher:
    def __init__(self) -> None:
        self.sent: dict[str, dict] = {}

    def send(self, run_id: str, rows: list[dict], definition: dict, recipients: list[str]) -> dict:
        if run_id in self.sent:
            return self.sent[run_id]
        message = {"run_id": run_id, "metric": calculate(rows, definition), "recipients": sorted(set(recipients))}
        self.sent[run_id] = message
        return message
