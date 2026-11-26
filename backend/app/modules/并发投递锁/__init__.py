"""并发投递锁（hook-01 硬闸）。

已接线：热更与投递共用 ``endpoint:<端点键>`` 互斥锁，配合 SQLite
``BEGIN IMMEDIATE`` / busy_timeout，保证读密钥版本与写尝试同成同败。
实现见 ``app.services.并发锁``。
"""
NAME = "并发投递锁"

from app.services.并发锁 import (  # noqa: E402,F401
    同锁,
    获取锁,
    抢锁,
    限时同锁,
    端点临界区,
    尝试进入,
    释放,
    锁名册,
    锁名,
    锁争用错误,
    锁争用失败,
    LockBusy,
    重置锁匣,
)

__all__ = [
    "NAME", "同锁", "获取锁", "抢锁", "限时同锁", "端点临界区",
    "尝试进入", "释放", "锁名册", "锁名",
    "锁争用错误", "锁争用失败", "LockBusy", "重置锁匣",
]
