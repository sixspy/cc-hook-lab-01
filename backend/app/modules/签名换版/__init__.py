"""签名换版（hook-01 邻题，随密钥热更一并接通版本权威）。

换版即热更：单调升档端点的现行密钥版本，旧版降为旧钥。
"""
NAME = "签名换版"

from app.services.签名目录 import 热更密钥 as 换版  # noqa: E402,F401
from app.services.签名目录 import 最新密钥版本, 密钥目录  # noqa: E402,F401

__all__ = ["NAME", "换版", "最新密钥版本", "密钥目录"]
