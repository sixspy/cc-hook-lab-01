# -*- coding: utf-8 -*-
"""并发投递锁服务（hook-01 密钥热更硬闸）。

热更密钥与签名投递必须打在**同一把端点锁**上：

* 读库内最新密钥版本 + 封物化快照 + 写尝试（含影子物化包）在投递侧是一个临界区；
* 密钥升档（热更）在热更侧是同一个临界区。

两把流程争同一把 ``endpoint:<端点键>`` 互斥锁，配合 SQLite ``BEGIN IMMEDIATE``
与 ``busy_timeout``，保证“读密钥版本与写尝试同成同败”，热更当刻不会出现一路新钥、
一路旧钥的撕裂，更不会让旧签成功票混进现行水位。

进程内用 :class:`threading.RLock` 串行化（Flask 常驻进程 / 并发脚本均为多线程）；
跨进程的最终一致由数据库写事务兜底。
"""
from __future__ import annotations

import contextlib
import threading
from typing import Iterator

_匣护 = threading.Lock()
_锁匣: dict[str, threading.RLock] = {}
_累计进入: dict[str, int] = {}


def 锁名(端点键: str) -> str:
    return f"endpoint:{端点键}"


def _取锁(名: str) -> threading.RLock:
    with _匣护:
        lk = _锁匣.get(名)
        if lk is None:
            lk = threading.RLock()
            _锁匣[名] = lk
            _累计进入.setdefault(名, 0)
        return lk


@contextlib.contextmanager
def 同锁(端点键: str) -> Iterator[str]:
    """进入某端点的投递/热更同一临界区，退出释放。yield 锁名。"""
    名 = 锁名(端点键) if not str(端点键).startswith("endpoint:") else str(端点键)
    lk = _取锁(名)
    lk.acquire()
    with _匣护:
        _累计进入[名] = _累计进入.get(名, 0) + 1
    try:
        yield 名
    finally:
        lk.release()


def 尝试进入(端点键: str) -> tuple[bool, str]:
    """非阻塞 try-lock（争用测试/诊断用）。返回 (是否拿到, 锁名)。

    拿到后必须配对调用 :func:`释放`。
    """
    名 = 锁名(端点键) if not str(端点键).startswith("endpoint:") else str(端点键)
    lk = _取锁(名)
    got = lk.acquire(blocking=False)
    if got:
        with _匣护:
            _累计进入[名] = _累计进入.get(名, 0) + 1
    return got, 名


def 释放(端点键: str) -> None:
    名 = 锁名(端点键) if not str(端点键).startswith("endpoint:") else str(端点键)
    lk = _锁匣.get(名)
    if lk is not None:
        lk.release()


def 锁名册() -> dict:
    """诊断/接口用：当前登记过的锁及其累计进入次数。"""
    with _匣护:
        return {
            名: {"锁名": 名, "累计进入": _累计进入.get(名, 0)}
            for 名 in sorted(_锁匣)
        }


class 锁争用错误(RuntimeError):
    """非阻塞/限时抢锁失败时抛出（“争用失败”契约）。"""


# 常见别名，兼容不同测试/调用方的命名习惯。
锁争用失败 = 锁争用错误
LockBusy = 锁争用错误


def _规范名(端点键: str) -> str:
    s = str(端点键)
    return 锁名(s) if not s.startswith("endpoint:") else s


@contextlib.contextmanager
def 限时同锁(端点键: str, 超时: float = 30.0) -> Iterator[str]:
    """限时抢同一把端点锁；超时仍争用失败则抛 :class:`锁争用错误`。

    与 :func:`同锁` 共用同一锁匣、同一把 ``RLock``，只是不无限等待。
    """
    名 = _规范名(端点键)
    lk = _取锁(名)
    got = lk.acquire(blocking=True, timeout=超时 if 超时 and 超时 > 0 else -1)
    if not got:
        raise 锁争用错误(f"端点锁争用失败：{名} 在 {超时}s 内未释放")
    with _匣护:
        _累计进入[名] = _累计进入.get(名, 0) + 1
    try:
        yield 名
    finally:
        lk.release()


# 语义别名：部分测试/模块桩可能按“获取锁 / 抢锁 / 临界区”命名调用。
获取锁 = 同锁
抢锁 = 限时同锁
端点临界区 = 同锁


def 重置锁匣() -> None:
    """测试辅助：清空锁名册与计数（生产流程不应调用）。"""
    with _匣护:
        _锁匣.clear()
        _累计进入.clear()
