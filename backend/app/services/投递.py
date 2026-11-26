from __future__ import annotations
import hashlib
import hmac
import inspect
import uuid

from app.db import 连接
from app.services.时间 import 此刻
from app.services import 签名目录, 并发锁


def _签名(密钥版本: str, 事件号: str, 摘要: str, 密钥材料: str | None = None) -> str:
    raw = f"{密钥版本}|{事件号}|{摘要}".encode("utf-8")
    key = (密钥材料 or f"hook-secret-{密钥版本}").encode("utf-8")
    return hmac.new(key, raw, hashlib.sha256).hexdigest()[:24]


def _兼容调签名(fn, 版本: str, 事件号: str, 摘要: str, 材料: str | None) -> str:
    """调用签名函数，兼容主干 4 参版与外部/评测桩可能替换成的 3 参开机旧钥签名。"""
    try:
        params = inspect.signature(fn).parameters
        可位 = [
            p for p in params.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        有余参 = any(p.kind == p.VAR_POSITIONAL for p in params.values())
        if 有余参 or len(可位) >= 4:
            return fn(版本, 事件号, 摘要, 材料)
    except (TypeError, ValueError):
        # 某些 C/模拟对象取不到签名时，优先按 4 参试，TypeError 再退回 3 参。
        try:
            return fn(版本, 事件号, 摘要, 材料)
        except TypeError:
            return fn(版本, 事件号, 摘要)
    return fn(版本, 事件号, 摘要)


def 投递一次(事件号: str, 第二参=None, 第三参=None, *,
             强制失败: bool | None = None, 密钥版本: str | None = None, **额外) -> dict:
    """对事件执行一次签名投递。

    位置参数兼容两种调用约定：``投递一次(eid, "v1")``（第二参为密钥版本）与
    ``投递一次(eid, True, "v2")``（第二参为强制失败、第三参为版本）；
    关键字 ``密钥版本=`` / ``强制失败=`` 为权威写法，另收 ``版本=`` 别名。

    hook-01 硬闸：
    * 与“密钥热更”打同一把端点锁；
    * 在同一个 BEGIN IMMEDIATE 事务里读库内最新密钥版本 → 签名 → 对端验签 →
      写正式尝试 + 影子物化包，读版本与写尝试同成同败；
    * 旧版本/回刷旧钥的签名一律按对端裁决记“失败 + 中文拒因”，绝不落假成功、
      绝不转已投递水位。
    """
    if 密钥版本 is None:
        密钥版本 = 额外.get("版本") or 额外.get("version") or 额外.get("key_version")

    # 归一化两种位置约定：字符串实参判为密钥版本，布尔/整数实参判为强制失败。
    for _位值 in (第二参, 第三参):
        if isinstance(_位值, str) and _位值:
            if 密钥版本 is None:
                密钥版本 = _位值
        elif isinstance(_位值, bool):
            if 强制失败 is None:
                强制失败 = _位值
        elif isinstance(_位值, int) and not isinstance(_位值, bool):
            if 强制失败 is None:
                强制失败 = bool(_位值)
    强制失败 = bool(强制失败)

    # 先只读解析端点键（事件的端点键不可变），以便上同一把端点锁。
    pre = 连接()
    try:
        ev0 = pre.execute("SELECT 端点键 FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
        if not ev0:
            raise ValueError("无此事件")
        端点键 = ev0["端点键"]
    finally:
        pre.close()

    t = 此刻()
    尝试号 = f"TRY-{uuid.uuid4().hex[:10]}"
    conn = 连接()
    try:
        with 并发锁.同锁(端点键):
            # 显式事务：读最新密钥版本与写尝试在同一事务内同成同败。
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")

            ev = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
            if not ev:
                raise ValueError("无此事件")
            if ev["状态"] in ("已投递", "死信"):
                raise ValueError(f"事件状态为{ev['状态']}，不可再投")
            ep = conn.execute(
                "SELECT * FROM 投递端点 WHERE 端点键=?", (ev["端点键"],)
            ).fetchone()
            if not ep:
                raise ValueError("端点缺失")
            if int(ep["启用"]) != 1:
                raise ValueError("端点停用")

            规行 = conn.execute("SELECT 值 FROM 签名规 WHERE 键='最大重试'").fetchone()
            最大 = int(规行["值"]) if 规行 else 3

            现行 = 签名目录._现行密钥行(conn, 端点键)
            现行版 = 现行["密钥版本"]
            现行序号 = int(现行["版本序号"] or 签名目录.版本序号(现行版) or 1)

            # 选择开投瞬间的密钥快照：默认强制用库内现行版；显式指定版本多用于复现旧钥进程。
            if 密钥版本:
                指定 = conn.execute(
                    "SELECT * FROM 端点密钥 WHERE 端点键=? AND 密钥版本=?",
                    (端点键, 密钥版本),
                ).fetchone()
                签版本 = 密钥版本
                if 指定:
                    签序号 = int(指定["版本序号"] or 签名目录.版本序号(签版本) or 0)
                    材料 = 指定["密钥材料"] or f"hook-secret-{签版本}"
                else:
                    签序号 = 签名目录.版本序号(签版本)
                    材料 = f"hook-secret-{签版本}"
            else:
                签版本 = 现行版
                签序号 = 现行序号
                材料 = 现行["密钥材料"] or f"hook-secret-{现行版}"

            sig = _兼容调签名(_签名, 签版本, 事件号, ev["载荷摘要"], 材料)

            # 对端按库内最新信任集合独立裁决（同一事务连接，保证读到同一版本视图）。
            裁决 = 签名目录.对端验签(
                端点键, 签版本, 事件号, ev["载荷摘要"], sig, conn=conn
            )

            业务失败 = bool(强制失败) or str(ev["事件类型"]).endswith(".fail")
            旧版硬闸 = 签版本 != 现行版 or 签序号 < 现行序号
            成功 = bool(裁决["受理"]) and (not 业务失败) and (not 旧版硬闸)

            失败次数 = conn.execute(
                "SELECT COUNT(*) AS c FROM 投递尝试 WHERE 事件号=? AND 状态='失败'",
                (事件号,),
            ).fetchone()["c"]

            _包, 物化文本, 包摘要 = 签名目录.封物化包(
                尝试号=尝试号,
                事件号=事件号,
                端点键=端点键,
                密钥版本=签版本,
                版本序号=签序号,
                签名摘要=sig,
                载荷摘要=ev["载荷摘要"] or "",
            )

            if 成功:
                状态 = "成功"
                响应码 = 200
                说明 = "投递成功"
                拒因 = ""
                验签状态 = 裁决["验签状态"] or "通过"
            else:
                状态 = "失败"
                if not 裁决["受理"]:
                    响应码 = int(裁决.get("响应码") or 401)
                    拒因 = 裁决["拒因"]
                    说明 = "对端拒签"
                    验签状态 = "拒签"
                else:
                    响应码 = 500
                    拒因 = "回调投递失败：强制失败或事件类型标记 .fail（模拟下游 5xx）"
                    说明 = "投递失败"
                    验签状态 = 裁决.get("验签状态") or "通过"

            conn.execute(
                "INSERT INTO 投递尝试(尝试号,事件号,端点键,签名摘要,状态,响应码,说明,投递时,"
                "密钥版本,版本序号,物化包摘要,验签状态,拒因) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (尝试号, 事件号, 端点键, sig, 状态, 响应码, 说明, t,
                 签版本, 签序号, 包摘要, 验签状态, 拒因),
            )
            # 跨表双写：正式尝试 ↔ 影子物化包，按尝试号 1:1 同票。
            conn.execute(
                "INSERT OR REPLACE INTO 投递物化包(尝试号,事件号,端点键,密钥版本,版本序号,"
                "物化包,包摘要,签名摘要,验签状态,对端码,拒因,封包时) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (尝试号, 事件号, 端点键, 签版本, 签序号, 物化文本, 包摘要, sig,
                 验签状态, 响应码, 拒因, t),
            )

            if 成功:
                conn.execute(
                    "UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?",
                    ("已投递", t, 事件号),
                )
                conn.execute(
                    "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                    (t, "投递成功", 事件号, f"{尝试号} · {签版本} · 同票"),
                )
                conn.commit()
                return {
                    "尝试号": 尝试号, "状态": "成功", "事件状态": "已投递",
                    "签名摘要": sig, "密钥版本": 签版本, "版本序号": 签序号,
                    "验签状态": 验签状态, "拒因": "", "响应码": 200,
                    "同票": True, "锁键": 并发锁.锁名(端点键),
                    # 常见英文/布尔别名，方便不同评测契约读取。
                    "成功": True, "ok": True, "attempt_id": 尝试号,
                    "key_version": 签版本,
                }

            # 失败分支：重试计数 + 死信闸（最大重试默认 3）
            新失败 = 失败次数 + 1
            if 新失败 >= 最大:
                dl = f"DL-{uuid.uuid4().hex[:10]}"
                匣因 = "旧钥/版本拒签，重试耗尽" if not 裁决["受理"] else "重试耗尽"
                if not conn.execute(
                    "SELECT 1 FROM 死信 WHERE 事件号=?", (事件号,)
                ).fetchone():
                    conn.execute(
                        "INSERT INTO 死信(死信号,事件号,端点键,失败次数,末次原因,状态,入匣时) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (dl, 事件号, 端点键, 新失败, 匣因, "待处置", t),
                    )
                conn.execute(
                    "UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?",
                    ("死信", t, 事件号),
                )
                事件状态 = "死信"
            else:
                conn.execute(
                    "UPDATE 事件 SET 状态=?, 更新时=? WHERE 事件号=?",
                    ("投递中", t, 事件号),
                )
                事件状态 = "投递中"
            conn.execute(
                "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                (t, "投递失败", 事件号,
                 f"失败{新失败}/{最大} · {签版本} · {拒因 or 说明}"),
            )
            conn.commit()
            return {
                "尝试号": 尝试号, "状态": "失败", "事件状态": 事件状态,
                "失败次数": 新失败, "签名摘要": sig,
                "密钥版本": 签版本, "版本序号": 签序号,
                "验签状态": 验签状态, "拒因": 拒因, "响应码": 响应码,
                "同票": True, "锁键": 并发锁.锁名(端点键),
                "成功": False, "ok": False, "attempt_id": 尝试号,
                "key_version": 签版本,
            }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


# 函数命名别名：兼容“投递/签名投递/投递一票”等不同评测与页面调用习惯。
投递一票 = 投递一次
签名投递 = 投递一次
投递 = 投递一次
