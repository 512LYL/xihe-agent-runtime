# -*- coding: utf-8 -*-
"""日志（自持文件句柄 + 单文件轮转）

为什么不用 logging.basicConfig：
  生产版曾出现「进程在跑、业务日志两个月一行没有」——root logger 的 FileHandler
  在某时点后静默失效，外面完全看不出来。这里每次写都自己 open/append/close，
  写失败也不抛异常，并且**按大小轮转**（旧版那份日志涨到 219 MB 没轮转）。
"""
import os
import threading
import time

_LOCK = threading.Lock()


def tail(path, n=120):
    """读文件末尾 n 行（读不到返回空列表）。"""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            back = min(size, max(4096, n * 400))
            f.seek(size - back)
            data = f.read()
        lines = data.decode("utf-8", "replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


class Logger:
    """极简文件日志。`echo=True` 时同时打到控制台（给 start.bat 窗口看）。"""

    def __init__(self, path, tag="xihe", echo=False, max_bytes=20 * 1024 * 1024,
                 keep=3):
        self.path = path
        self.tag = tag
        self.echo = echo
        self.max_bytes = int(max_bytes)
        self.keep = int(keep)
        d = os.path.dirname(path)
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                pass

    # ---------------------------------------------------------------- 内部
    def _rotate(self):
        try:
            if self.max_bytes <= 0 or not os.path.exists(self.path):
                return
            if os.path.getsize(self.path) < self.max_bytes:
                return
            for i in range(self.keep - 1, 0, -1):
                src, dst = "%s.%d" % (self.path, i), "%s.%d" % (self.path, i + 1)
                if os.path.exists(src):
                    try:
                        os.replace(src, dst)
                    except Exception:
                        pass
            os.replace(self.path, self.path + ".1")
        except Exception:
            pass

    def _emit(self, level, msg, args):
        try:
            text = (msg % args) if args else str(msg)
        except Exception:
            text = "%s %r" % (msg, args)
        line = "%s %-7s [%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                      level, self.tag, text)
        with _LOCK:
            try:
                self._rotate()
                with open(self.path, "a", encoding="utf-8", errors="replace") as f:
                    f.write(line)
                    f.flush()
            except Exception:
                pass
        if self.echo:
            try:
                print(line.rstrip("\n"), flush=True)
            except Exception:
                pass

    # ---------------------------------------------------------------- 公开
    def info(self, msg, *args):
        self._emit("INFO", msg, args)

    def warning(self, msg, *args):
        self._emit("WARNING", msg, args)

    def error(self, msg, *args):
        self._emit("ERROR", msg, args)

    def critical(self, msg, *args):
        self._emit("CRITICAL", msg, args)


_CACHE = {}


def get_logger(cfg, name="xihe", echo=False, tag=None):
    """按配置里的 logs 目录取日志器（同名同路径复用同一实例）。"""
    from . import config as _config
    d = _config.path(cfg, "paths", "logs")
    path = os.path.join(d, "%s.log" % name)
    key = (path, bool(echo), tag or name)
    if key not in _CACHE:
        _CACHE[key] = Logger(path, tag=tag or name, echo=echo)
    return _CACHE[key]
