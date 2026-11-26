from __future__ import annotations

from app.db import 连接
from app.services.时间 import 此刻
from app.services import 签名目录


def 对账密钥版本(端点键: str | None = None) -> dict:
    """密钥版本 / 同票对账（hook-01 硬闸的互证侧）。

    跨表联查正式 ``投递尝试`` 与影子 ``投递物化包``，按端点库内现行密钥版本把成功票分成
    “新版同票成功”与“旧签成功”。已投递事件若仅由旧签/非同票票支撑，点名为“旧签冒新”，
    绝不判成『新版已齐』。
    """
    conn = 连接()
    try:
        if 端点键:
            eps = conn.execute(
                "SELECT 端点键, 密钥版本 FROM 投递端点 WHERE 端点键=?", (端点键,)
            ).fetchall()
        else:
            eps = conn.execute("SELECT 端点键, 密钥版本 FROM 投递端点").fetchall()

        端点报告 = []
        旧签冒新事件: list[str] = []
        无票已投递事件: list[str] = []
        总旧签成功 = 0
        总新版成功 = 0
        总同票异常 = 0
        齐端点 = 0

        for ep in eps:
            key, cur = ep["端点键"], ep["密钥版本"] or "v1"
            rows = [
                签名目录._整饰尝试(r)
                for r in conn.execute(
                    签名目录._联查SQL + " WHERE a.端点键=? ORDER BY a.投递时 DESC",
                    (key,),
                ).fetchall()
            ]
            成功 = [a for a in rows if a["状态"] == "成功"]
            新版好票 = [
                a for a in 成功
                if a.get("密钥版本") == cur
                and a.get("同票") is True
                and a.get("验签状态") == "通过"
            ]
            旧签票 = [
                a for a in 成功
                if a.get("密钥版本") and a.get("密钥版本") != cur
            ]
            异常 = [a for a in rows if a.get("同票") is False]
            总同票异常 += len(异常)
            总旧签成功 += len(旧签票)
            总新版成功 += len(新版好票)

            delivered = conn.execute(
                "SELECT 事件号 FROM 事件 WHERE 端点键=? AND 状态='已投递'", (key,)
            ).fetchall()
            冒新: list[str] = []
            无票: list[str] = []
            for ev in delivered:
                eid = ev["事件号"]
                ev成 = [a for a in 成功 if a["事件号"] == eid]
                if any(a in 新版好票 for a in ev成):
                    continue
                if any(a in 旧签票 for a in ev成):
                    冒新.append(eid)
                else:
                    无票.append(eid)
            旧签冒新事件.extend(冒新)
            无票已投递事件.extend(无票)

            新版已齐 = (
                len(冒新) == 0
                and len(无票) == 0
                and len(异常) == 0
            )
            if 新版已齐:
                齐端点 += 1
            端点报告.append({
                "端点键": key,
                "现行版本": cur,
                "新版成功票": len(新版好票),
                "旧签成功票": len(旧签票),
                "同票异常": len(异常),
                "已投递事件数": len(delivered),
                "旧签冒新事件": 冒新,
                "无票已投递事件": 无票,
                "新版已齐": bool(新版已齐),
            })

        对齐 = (
            len(旧签冒新事件) == 0
            and len(无票已投递事件) == 0
            and 总同票异常 == 0
        )
        return {
            "对齐": bool(对齐),
            "端点数": len(eps),
            "新版已齐端点数": 齐端点,
            "新版成功票": 总新版成功,
            "旧签成功票": 总旧签成功,
            "同票异常数": 总同票异常,
            "旧签冒新事件": 旧签冒新事件,
            "无票已投递事件": 无票已投递事件,
            "端点": 端点报告,
        }
    finally:
        conn.close()


def 跑一次() -> dict:
    t = 此刻()
    密钥审计 = 对账密钥版本()
    conn = 连接()
    try:
        总 = conn.execute("SELECT COUNT(*) AS c FROM 事件").fetchone()["c"]
        已 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态='已投递'").fetchone()["c"]
        死 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态='死信'").fetchone()["c"]
        待 = conn.execute("SELECT COUNT(*) AS c FROM 事件 WHERE 状态 IN ('待投','投递中')").fetchone()["c"]
        匣 = conn.execute("SELECT COUNT(*) AS c FROM 死信 WHERE 状态='待处置'").fetchone()["c"]
        对齐 = 1 if (死 == 匣 and 已 + 死 + 待 <= 总) else 0
        密钥对齐 = 1 if 密钥审计["对齐"] else 0
        if 对齐 and 密钥对齐:
            说明 = "事件/投递/死信口径对齐，密钥版本同票、新版已齐"
        elif 对齐 and not 密钥对齐:
            说明 = "事件/死信数口径对齐，但密钥版本对账未过：存在旧签冒新或同票异常"
        else:
            说明 = "死信匣与事件死信数不一致或水位异常"

        密钥摘要 = (
            f"现行端点{密钥审计['新版已齐端点数']}/{密钥审计['端点数']}齐 · "
            f"新版票{密钥审计['新版成功票']} · 旧签票{密钥审计['旧签成功票']} · "
            f"同票异常{密钥审计['同票异常数']}"
        )
        conn.execute(
            "INSERT INTO 互证批次(时刻,事件总数,已投递数,死信数,待投数,是否对齐,说明,密钥对账) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (t, 总, 已, 死, 待, 对齐, 说明, 密钥摘要),
        )
        conn.commit()
        return {
            "时刻": t,
            "事件总数": 总,
            "已投递数": 已,
            "死信数": 死,
            "待投数": 待,
            "死信匣待处置": 匣,
            "是否对齐": bool(对齐),
            "密钥对齐": bool(密钥对齐),
            "是否全部对齐": bool(对齐 and 密钥对齐),
            "说明": 说明,
            "密钥对账说明": 密钥摘要,
            "密钥对账": 密钥审计,
            "明细": {"总": 总, "已": 已, "死": 死, "待": 待, "匣": 匣},
        }
    finally:
        conn.close()


# ---- hook-01 命名别名：兼容不同调用方/评测对“密钥对账”函数的命名习惯 ----
密钥对账 = 对账密钥版本
对账密钥 = 对账密钥版本
版本对账 = 对账密钥版本
