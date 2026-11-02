"""Windows 兼容补丁（sitecustomize 自动加载）。

Windows 默认的事件循环是 ProactorEventLoop，它不实现 add_reader / add_writer
（BaseEventLoop 里直接 raise NotImplementedError）。小米官方集成 xiaomi_home
的 MQTT(MIPS) 用 add_reader 监听 socket，因此在 Windows 上必然抛
NotImplementedError，导致云端属性推送收不到（能写不能读）。

这里给 ProactorEventLoop 补一个基于 select 轮询的降级实现，
让 add_reader / add_writer / remove_reader / remove_writer 可用。
"""

import asyncio
import os
import select

if os.name == "nt" and hasattr(asyncio, "ProactorEventLoop"):

    _Loop = asyncio.ProactorEventLoop
    _POLL_INTERVAL = 0.05

    def _ensure_state(loop):
        if not hasattr(loop, "_wb_readers"):
            loop._wb_readers = {}
            loop._wb_writers = {}
            loop._wb_handle = None

    def _schedule_poll(loop):
        if loop.is_closed():
            return
        if getattr(loop, "_wb_handle", None) is None:
            loop._wb_handle = loop.call_later(_POLL_INTERVAL, _poll, loop)

    def _poll(loop):
        loop._wb_handle = None
        if loop.is_closed():
            return
        readers = loop._wb_readers
        writers = loop._wb_writers
        if not readers and not writers:
            return
        read_fds = list(readers)
        write_fds = list(writers)
        try:
            ready_r, ready_w, _ = select.select(read_fds, write_fds, [], 0)
        except (OSError, ValueError):
            ready_r, ready_w = [], []
        for fd in ready_r:
            item = readers.get(fd)
            if item:
                try:
                    item[0](*item[1])
                except Exception:  # noqa: BLE001
                    pass
        for fd in ready_w:
            item = writers.get(fd)
            if item:
                try:
                    item[0](*item[1])
                except Exception:  # noqa: BLE001
                    pass
        _schedule_poll(loop)

    def add_reader(self, fd, callback, *args):
        _ensure_state(self)
        self._wb_readers[fd] = (callback, args)
        _schedule_poll(self)

    def add_writer(self, fd, callback, *args):
        _ensure_state(self)
        self._wb_writers[fd] = (callback, args)
        _schedule_poll(self)

    def remove_reader(self, fd):
        _ensure_state(self)
        return self._wb_readers.pop(fd, None) is not None

    def remove_writer(self, fd):
        _ensure_state(self)
        return self._wb_writers.pop(fd, None) is not None

    _Loop.add_reader = add_reader
    _Loop.add_writer = add_writer
    _Loop.remove_reader = remove_reader
    _Loop.remove_writer = remove_writer
