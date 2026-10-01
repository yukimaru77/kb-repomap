"""Load the KB items an --remote session injects at inference time."""
from kb_items import load_session_items


class RemoteKB:
    def __init__(self, config, jsonl, *, developer_text=None):
        # Session metadata belongs to Codex. Keep the portable memories and
        # the store dev.txt; never import the producer's session configuration.
        self.items = load_session_items(jsonl, developer_text=developer_text)
