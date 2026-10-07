"""Cooperative pause for the explicitly launched classification helper."""
from netease_organizer.runtime import OperationControl


class ClassificationFileControl(OperationControl):
    def __init__(self, marker):
        super().__init__()
        self.marker=marker

    def checkpoint(self):
        if self.marker.exists():self.request_pause()
        super().checkpoint()
