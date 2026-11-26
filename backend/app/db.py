from __future__ import annotations
import os
import sqlite3
from pathlib import Path

数据目录 = Path(os.environ.get("数据目录", Path(__file__).resolve().parent.parent / "data"))
夹具目录 = Path(os.environ.get("夹具目录", Path(__file__).resolve().parent.parent.parent / "fixtures"))
库路径 = 数据目录 / "投递台.db"
# 导入期默认值：用于识别测试是否显式 monkeypatch 了 db.库路径 / db.数据目录。
_导入期数据目录 = 数据目录
_导入期默认库路径 = 库路径


def 当前库路径() -> Path:
    """解析此刻应使用的 SQLite 文件，兼容常见测试隔离手法。

    优先级：
    1. 显式改写过的 ``db.库路径``（hook-01 三红测试采用，函数级隔离）；
    2. 只改写 ``db.数据目录`` 的夹具 -> ``数据目录/投递台.db``；
    3. 运行期设置/改写的 ``数据目录`` 环境变量（import 后才 setenv 的夹具）；
    4. 导入期默认 ``db.数据目录 / 投递台.db``。
    """
    if 库路径 != _导入期默认库路径:
        return Path(库路径)
    if 数据目录 != _导入期数据目录:
        return Path(数据目录) / "投递台.db"
    env_dir = os.environ.get("数据目录")
    if env_dir:
        return Path(env_dir) / "投递台.db"
    return Path(数据目录) / "投递台.db"


def 连接() -> sqlite3.Connection:
    # timeout + busy_timeout：热更与投递争用同一把库写锁时排队等待，而不是立刻 database is locked。
    路径 = 当前库路径()
    路径.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(路径), timeout=30.0, check_same_thread=True)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 30000")
    # WAL + NORMAL：热更/投递争用时读写不互堵，提交 fsync 更轻；跨进程由写事务 + busy_timeout 兜底。
    try:
        c.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError:
        pass
    c.execute("PRAGMA synchronous = NORMAL")
    return c
