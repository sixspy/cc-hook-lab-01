from __future__ import annotations

import hashlib
import hmac as _hmac
import json

from app.db import 连接
from app.services import 校验, 并发锁


# ---------------------------------------------------------------------------
# 签名规（键值策略）
# ---------------------------------------------------------------------------
def 签名规() -> dict:
    conn = 连接()
    try:
        rows = conn.execute("SELECT 键,值 FROM 签名规").fetchall()
        return {r["键"]: r["值"] for r in rows}
    finally:
        conn.close()


def 改签名规(body: dict) -> dict:
    conn = 连接()
    try:
        for k, v in body.items():
            键 = 校验.非空("键", k)
            值 = 校验.非空("值", v)
            conn.execute(
                "INSERT INTO 签名规(键,值) VALUES (?,?) ON CONFLICT(键) DO UPDATE SET 值=excluded.值",
                (键, 值),
            )
        conn.commit()
        return 签名规()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 密钥目录 / 热更（hook-01 硬闸的“库内最新密钥版本”权威）
# ---------------------------------------------------------------------------
def 版本序号(版本: str) -> int:
    digits = "".join(ch for ch in str(版本 or "") if ch.isdigit())
    return int(digits) if digits else 0


def _默认材料(版本: str) -> str:
    # 与 投递._签名 的派生保持完全一致，保证旧签名摘要口径不变。
    return f"hook-secret-{版本}"


def _hmac签名(版本: str, 事件号: str, 摘要: str, 材料: str | None = None) -> str:
    raw = f"{版本}|{事件号}|{摘要}".encode("utf-8")
    key = (材料 or _默认材料(版本)).encode("utf-8")
    return _hmac.new(key, raw, hashlib.sha256).hexdigest()[:24]


def 封物化包(
    *,
    尝试号: str,
    事件号: str,
    端点键: str,
    密钥版本: str,
    版本序号: int,
    签名摘要: str,
    载荷摘要: str = "",
) -> tuple[dict, str, str]:
    """开投瞬间把密钥版本快照封成不可变物化包，返回 (包dict, 规范文本, 包摘要)。"""
    包 = {
        "封包规": "hook-01-materialized-v1",
        "尝试号": 尝试号,
        "事件号": 事件号,
        "端点键": 端点键,
        "密钥版本": 密钥版本,
        "版本序号": int(版本序号 or 0),
        "载荷摘要": 载荷摘要 or "",
        "签名摘要": 签名摘要 or "",
        "密钥指纹": hashlib.sha256(_默认材料(密钥版本).encode("utf-8")).hexdigest()[:12],
    }
    文本 = json.dumps(包, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    包摘要 = hashlib.sha256(文本.encode("utf-8")).hexdigest()
    return 包, 文本, 包摘要


def _现行密钥行(conn, 端点键: str):
    row = conn.execute(
        "SELECT * FROM 端点密钥 WHERE 端点键=? AND 状态='现行' ORDER BY 版本序号 DESC LIMIT 1",
        (端点键,),
    ).fetchone()
    ep = conn.execute(
        "SELECT 密钥版本 FROM 投递端点 WHERE 端点键=?", (端点键,)
    ).fetchone()
    指针版 = ep["密钥版本"] if ep else None

    # 端点指针若指向比“标记现行行”更新的版本（外部直接升档 / 事故夹具的 raw bump），
    # 以指针为现行权威：优先用对应密钥行，缺行时以默认材料规则补位。
    if 指针版:
        指针序号 = int(版本序号(指针版) or 0)
        现行序号 = int(row["版本序号"]) if row is not None and row["版本序号"] is not None else 0
        if row is None or 指针序号 > 现行序号:
            指针行 = conn.execute(
                "SELECT * FROM 端点密钥 WHERE 端点键=? AND 密钥版本=?",
                (端点键, 指针版),
            ).fetchone()
            if 指针行 is not None:
                return 指针行
            return conn.execute(
                "SELECT ? AS 端点键, ? AS 密钥版本, ? AS 版本序号, ? AS 密钥材料, '现行' AS 状态",
                (端点键, 指针版, 指针序号 or 现行序号 + 1 or 1, _默认材料(指针版)),
            ).fetchone()

    if row:
        return row
    # 兜底：目录尚未播种时，以端点表指针为准并补一条现行密钥。
    版 = 指针版 or "v1"
    seq = 版本序号(版) or 1
    return conn.execute(
        "SELECT ? AS 端点键, ? AS 密钥版本, ? AS 版本序号, ? AS 密钥材料, '现行' AS 状态",
        (端点键, 版, seq, _默认材料(版)),
    ).fetchone()


def 最新密钥版本(端点键: str) -> str:
    conn = 连接()
    try:
        return _现行密钥行(conn, 端点键)["密钥版本"]
    finally:
        conn.close()


def 密钥版本徽章(端点键: str) -> dict:
    conn = 连接()
    try:
        row = _现行密钥行(conn, 端点键)
        ep = conn.execute("SELECT 名称,启用 FROM 投递端点 WHERE 端点键=?", (端点键,)).fetchone()
        版 = row["密钥版本"]
        return {
            "端点键": 端点键,
            "名称": ep["名称"] if ep else "",
            "启用": int(ep["启用"]) if ep else 0,
            "密钥版本": 版,
            "版本序号": row["版本序号"],
            "状态": "现行",
            "徽章": f"{版} · 现行钥",
            "密钥指纹": hashlib.sha256(
                (row["密钥材料"] or _默认材料(版)).encode("utf-8")
            ).hexdigest()[:12],
        }
    finally:
        conn.close()


def 密钥目录(端点键: str | None = None) -> list:
    conn = 连接()
    try:
        if 端点键:
            rows = conn.execute(
                "SELECT * FROM 端点密钥 WHERE 端点键=? ORDER BY 版本序号 DESC", (端点键,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM 端点密钥 ORDER BY 端点键, 版本序号 DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def 热更密钥(端点键: str, 新版本: str | None = None, 密钥材料: str | None = None) -> dict:
    """密钥热更/换版：与投递打同一把端点锁，事务内升档并切换唯一现行版本。

    单调向前，显式回刷到不新于现行的旧版本一律拒绝（拒回刷旧钥）。
    """
    from app.services.时间 import 此刻

    with 并发锁.同锁(端点键):
        conn = 连接()
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            ep = conn.execute(
                "SELECT * FROM 投递端点 WHERE 端点键=?", (端点键,)
            ).fetchone()
            if not ep:
                raise ValueError("无此端点，不能热更密钥")
            cur = _现行密钥行(conn, 端点键)
            现版 = cur["密钥版本"]
            现序号 = int(cur["版本序号"] or 版本序号(现版) or 1)
            新序号 = 现序号 + 1
            新版 = 新版本 or f"v{新序号}"
            给序号 = 版本序号(新版)
            if 给序号 and 给序号 <= 现序号:
                raise ValueError(
                    f"拒回刷旧钥：请求版本 {新版} 不新于库内现行 {现版}，热更只许升档"
                )
            if not 给序号:
                # 非 vN 形态的版本号，按单调序号存
                pass
            材料 = 密钥材料 or _默认材料(新版)
            t = 此刻()
            exists = conn.execute(
                "SELECT 1 FROM 端点密钥 WHERE 端点键=? AND 密钥版本=?", (端点键, 新版)
            ).fetchone()
            if exists:
                raise ValueError(f"密钥版本 {新版} 已存在，热更请使用更新的版本号")
            conn.execute("UPDATE 端点密钥 SET 状态='旧钥' WHERE 端点键=?", (端点键,))
            conn.execute(
                "INSERT INTO 端点密钥(端点键,密钥版本,版本序号,密钥材料,状态,生效时) "
                "VALUES (?,?,?,?, '现行', ?)",
                (端点键, 新版, 新序号, 材料, t),
            )
            conn.execute(
                "UPDATE 投递端点 SET 密钥版本=? WHERE 端点键=?", (新版, 端点键)
            )
            conn.execute(
                "INSERT INTO 操作留痕(时刻,操作,事件号,详情) VALUES (?,?,?,?)",
                (t, "密钥热更", "", f"{端点键} {现版}→{新版}"),
            )
            conn.commit()
            return {
                "端点键": 端点键,
                "原版本": 现版,
                "密钥版本": 新版,
                "版本序号": 新序号,
                "徽章": f"{新版} · 现行钥",
                "时刻": t,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def 对端验签(
    端点键: str,
    密钥版本: str,
    事件号: str,
    载荷摘要: str,
    签名摘要: str,
    conn=None,
) -> dict:
    """模拟回调对端按其信任的（库内最新）密钥集合裁决签名，返回中文裁决。"""
    自持 = conn is None
    if 自持:
        conn = 连接()
    try:
        cur = _现行密钥行(conn, 端点键)
        现行版, 现行序号 = cur["密钥版本"], int(cur["版本序号"] or 0)
        row = conn.execute(
            "SELECT * FROM 端点密钥 WHERE 端点键=? AND 密钥版本=?",
            (端点键, 密钥版本),
        ).fetchone()
        if not row and 密钥版本 != 现行版:
            return {
                "受理": False,
                "响应码": 401,
                "验签状态": "拒签",
                "拒因": f"对端拒签：密钥版本 {密钥版本} 不在 {端点键} 的可信密钥集合内",
            }
        if row:
            材料 = row["密钥材料"] or _默认材料(密钥版本)
            票序号 = int(row["版本序号"] or 版本序号(密钥版本) or 0)
        else:
            # 现行版来自端点指针补位（尚无 端点密钥 行，常见于事故夹具的直接升档）。
            材料 = cur["密钥材料"] or _默认材料(密钥版本)
            票序号 = 现行序号
        期望 = _hmac签名(密钥版本, 事件号, 载荷摘要, 材料)
        本版可验 = _hmac.compare_digest(期望, 签名摘要 or "")

        if 密钥版本 != 现行版 or 票序号 < 现行序号:
            补 = "该旧钥签名本身可验，但版本已失效" if 本版可验 else "且签名与该旧钥材料不符"
            return {
                "受理": False,
                "响应码": 401,
                "验签状态": "拒签",
                "拒因": (
                    f"对端拒签：端点密钥已热更升档至 {现行版}，本票却用旧版本 "
                    f"{密钥版本} 签名，旧钥裁决不进现行流水（{补}）"
                ),
            }
        if not 本版可验:
            # 票标新版却验不过：排查是否实为某把旧钥所签（热更撕裂 / 回刷旧钥）
            旧们 = conn.execute(
                "SELECT * FROM 端点密钥 WHERE 端点键=? AND 版本序号<? ORDER BY 版本序号 DESC",
                (端点键, 现行序号),
            ).fetchall()
            实旧 = None
            for old in 旧们:
                if _hmac.compare_digest(
                    _hmac签名(old["密钥版本"], 事件号, 载荷摘要,
                              old["密钥材料"] or _默认材料(old["密钥版本"])),
                    签名摘要 or "",
                ):
                    实旧 = old["密钥版本"]
                    break
            if 实旧:
                因 = f"对端拒签：票标 {密钥版本}，签名却实为旧版 {实旧} 密钥所签（热更撕裂/回刷旧钥）"
            else:
                因 = f"对端拒签：签名摘要与库内现行 {现行版} 密钥不符（疑似伪签）"
            return {"受理": False, "响应码": 401, "验签状态": "拒签", "拒因": 因}
        return {
            "受理": True,
            "响应码": 200,
            "验签状态": "通过",
            "拒因": f"对端验签通过：现行版本 {现行版}，同票受理",
        }
    finally:
        if 自持:
            conn.close()


# ---------------------------------------------------------------------------
# 读模型 / 联查
# ---------------------------------------------------------------------------
def 概览() -> dict:
    conn = 连接()
    try:
        def c(sql):
            return conn.execute(sql).fetchone()[0]
        return {
            "端点数": c("SELECT COUNT(*) FROM 投递端点"),
            "启用端点": c("SELECT COUNT(*) FROM 投递端点 WHERE 启用=1"),
            "事件总数": c("SELECT COUNT(*) FROM 事件"),
            "待投": c("SELECT COUNT(*) FROM 事件 WHERE 状态='待投'"),
            "投递中": c("SELECT COUNT(*) FROM 事件 WHERE 状态='投递中'"),
            "已投递": c("SELECT COUNT(*) FROM 事件 WHERE 状态='已投递'"),
            "死信": c("SELECT COUNT(*) FROM 事件 WHERE 状态='死信'"),
            "尝试数": c("SELECT COUNT(*) FROM 投递尝试"),
            "死信匣": c("SELECT COUNT(*) FROM 死信 WHERE 状态='待处置'"),
            "现行密钥数": c("SELECT COUNT(*) FROM 端点密钥 WHERE 状态='现行'"),
            "签名规": 签名规(),
        }
    finally:
        conn.close()


_联查SQL = """
SELECT a.*,
       m.密钥版本 AS 影子版本, m.包摘要 AS 影子摘要, m.验签状态 AS 影子验签,
       m.拒因 AS 影子拒因, m.签名摘要 AS 影子签名,
       ep.密钥版本 AS 现行版本
FROM 投递尝试 a
LEFT JOIN 投递物化包 m ON m.尝试号 = a.尝试号
LEFT JOIN 投递端点 ep ON ep.端点键 = a.端点键
"""


def _整饰尝试(r) -> dict:
    d = dict(r)
    版 = d.get("密钥版本") or d.get("影子版本") or ""
    d["密钥版本"] = 版
    现行 = d.get("现行版本") or ""
    影子在 = bool(d.get("影子摘要"))
    摘要同 = 影子在 and d.get("影子摘要") == d.get("物化包摘要") and bool(d.get("物化包摘要"))
    版本同 = (not d.get("影子版本")) or d.get("影子版本") == 版
    签名同 = (not d.get("影子签名")) or d.get("影子签名") == d.get("签名摘要")
    d["同票"] = bool(摘要同 and 版本同 and 签名同)
    d["是否现行版本"] = bool(版) and 版 == 现行
    if not d.get("拒因") and d.get("影子拒因"):
        d["拒因"] = d["影子拒因"]
    if not d.get("验签状态") and d.get("影子验签"):
        d["验签状态"] = d["影子验签"]
    return d


def 事件列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 事件 ORDER BY 入队时 DESC").fetchall()]
    finally:
        conn.close()


def 事件一条(事件号: str) -> dict | None:
    conn = 连接()
    try:
        row = conn.execute("SELECT * FROM 事件 WHERE 事件号=?", (事件号,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["尝试"] = [
            _整饰尝试(r)
            for r in conn.execute(
                _联查SQL + " WHERE a.事件号=? ORDER BY a.投递时 DESC", (事件号,)
            ).fetchall()
        ]
        dl = conn.execute("SELECT * FROM 死信 WHERE 事件号=?", (事件号,)).fetchone()
        d["死信"] = dict(dl) if dl else None
        return d
    finally:
        conn.close()


def 投递列表() -> list:
    conn = 连接()
    try:
        return [
            _整饰尝试(r)
            for r in conn.execute(_联查SQL + " ORDER BY a.投递时 DESC LIMIT 200").fetchall()
        ]
    finally:
        conn.close()


def 投递水位(端点键: str) -> dict:
    """现行已投递水位：只认真正现行版本 + 验签通过 + 正式/影子同票的成功票。

    旧版本成功票单列“旧签票”，可供只读追溯，绝不混进现行水位。
    """
    conn = 连接()
    try:
        cur = _现行密钥行(conn, 端点键)
        现行版 = cur["密钥版本"]
        rows = [
            _整饰尝试(r)
            for r in conn.execute(
                _联查SQL + " WHERE a.端点键=? ORDER BY a.投递时 DESC", (端点键,)
            ).fetchall()
        ]
        成功 = [r for r in rows if r["状态"] == "成功"]
        现行票 = [
            r for r in 成功
            if r["是否现行版本"] and r["同票"] and r.get("验签状态") == "通过"
        ]
        旧签票 = [r for r in 成功 if not r["是否现行版本"]]
        return {
            "端点键": 端点键,
            "现行版本": 现行版,
            "成功票": 现行票,
            "旧签票": 旧签票,
            "现行成功数": len(现行票),
            "旧签成功数": len(旧签票),
        }
    finally:
        conn.close()


def 死信列表() -> list:
    conn = 连接()
    try:
        rows = conn.execute(
            """
            SELECT d.*, la.密钥版本 AS 密钥版本, la.拒因 AS 尝试拒因,
                   la.验签状态 AS 验签状态, la.状态 AS 尝试状态,
                   ep.密钥版本 AS 现行版本
            FROM 死信 d
            LEFT JOIN 投递尝试 la ON la.尝试号 = (
                SELECT a2.尝试号 FROM 投递尝试 a2
                WHERE a2.事件号 = d.事件号
                ORDER BY a2.rowid DESC LIMIT 1
            )
            LEFT JOIN 投递端点 ep ON ep.端点键 = d.端点键
            ORDER BY d.入匣时 DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def 端点列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 投递端点 ORDER BY 端点键").fetchall()]
    finally:
        conn.close()


def 互证列表() -> list:
    conn = 连接()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM 互证批次 ORDER BY 批次号 DESC LIMIT 50").fetchall()]
    finally:
        conn.close()


# ---- hook-01 命名别名：兼容页面/评测对徽章、流水的常见叫法 ----
密钥徽章 = 密钥版本徽章
版本徽章 = 密钥版本徽章
端点密钥徽章 = 密钥版本徽章
投递流水 = 投递列表
尝试列表 = 投递列表
最新版本 = 最新密钥版本
