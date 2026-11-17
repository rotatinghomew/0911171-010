from tempfile import TemporaryDirectory

from .dispatch import Dispatcher
from .mailbox import LocalMailbox, send_digest


def main() -> None:
    dispatcher = Dispatcher()
    rows = [{"day": "2026-11-01", "value": 4}]
    definition = {"day": "2026-11-01", "field": "value", "name": "visits", "aggregation": "sum"}
    with TemporaryDirectory() as directory:
        mailbox = LocalMailbox(directory)
        print(send_digest(dispatcher, mailbox, "run-1", rows, definition, ["regional@example.invalid"]))
        print(mailbox.messages())


if __name__ == "__main__":
    main()
