"""Cooperative stop signals; an in-flight write must verify before stopping."""

from threading import Event


class OperationPaused(RuntimeError):
    def __init__(self):
        super().__init__('已请求暂停，本次不会开始下一项操作。')


class OperationControl:
    def __init__(self):
        self._paused = Event()

    @property
    def pause_requested(self):
        return self._paused.is_set()

    def request_pause(self):
        self._paused.set()

    def reset(self):
        self._paused.clear()

    def checkpoint(self):
        if self.pause_requested:
            raise OperationPaused()
