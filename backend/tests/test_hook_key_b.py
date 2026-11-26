# -*- coding: utf-8 -*-
"""失败测乙 · 旧钥成功不得对账成“新版已齐” —— 归属服务：互证服务（app/services/互证.py）。

事故命题：热更到 v2 后，账上“已投递”若仅由 v1 旧钥成功票支撑，互证对账不能把它
误标为“新版已齐”。互证必须跨表联查正式尝试 ↔ 影子物化包（同票），并按端点库内现行
密钥版本重新归类：旧签成功票只读陈列、不计入现行水位 / 新版已齐。

在未接线主干上没有 ``互证.对账密钥版本``，``跑一次`` 也不做密钥版本对账 —— 本文件先红，
救绿责任在互证服务（不是改某处页面展示）。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook_key_b_env_")
os.environ["数据目录"] = _TMP
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

import app.db as db  # noqa: E402
from app import seed  # noqa: E402
from app.services import 入队, 投递, 签名目录, 互证  # noqa: E402

EP = "ep-billing"


@pytest.fixture()
def 库(tmp_path):
    old_dir, old_path = db.数据目录, db.库路径
    db.数据目录 = tmp_path
    db.库路径 = tmp_path / "hook_key_b.db"
    seed.初始化库()
    try:
        yield db.库路径
    finally:
        db.数据目录, db.库路径 = old_dir, old_path


def _端点审计(audit: dict, ep: str = EP) -> dict:
    rows = [x for x in audit["端点"] if x["端点键"] == ep]
    assert rows, f"密钥对账缺少端点 {ep}"
    return rows[0]


def test_乙_旧钥成功不得误标新版已齐(库):
    # 1) v1 现行时正常投递一票 -> 合法的 v1 成功票（正式+影子同票）
    ev = 入队.建事件(
        {"事件类型": "invoice.paid", "端点键": EP, "载荷摘要": "inv=b-old", "优先级": "普通"}
    )
    eid = ev["事件号"]
    r1 = 投递.投递一次(eid)
    assert r1["状态"] == "成功" and r1["密钥版本"] == "v1"

    # 2) 密钥热更升档 v2（旧 v1 票成为“旧签成功票”）
    签名目录.热更密钥(EP)

    # 3) 互证密钥版本对账：不得把这票算成“新版已齐”
    audit = 互证.对账密钥版本()
    a = _端点审计(audit)
    assert a["现行版本"] == "v2"
    assert a["旧签成功票"] >= 1
    assert a["新版成功票"] == 0
    assert a["新版已齐"] is False, "仅靠 v1 旧钥成功票，不能判 v2 新版已齐"
    assert eid in audit["旧签冒新事件"], "该已投递事件应被点名：水位仅由旧签支撑"
    assert audit["对齐"] is False

    rep = 互证.跑一次()
    assert rep["密钥对齐"] is False
    assert rep["是否全部对齐"] is False, "旧签冒新时总对账不得判全部对齐"


def test_乙_补投新版同票成功后_新版才算齐(库):
    ev = 入队.建事件({"事件类型": "invoice.paid", "端点键": EP, "载荷摘要": "inv=b-new"})
    eid = ev["事件号"]
    投递.投递一次(eid)                 # v1 成功
    签名目录.热更密钥(EP)              # -> v2

    # 旧票状态下未齐
    assert _端点审计(互证.对账密钥版本())["新版已齐"] is False

    # 重新驱动该事件回待投，用 v2 补投一票（模拟热更后重试/互证回流）
    conn = db.连接()
    conn.execute("UPDATE 事件 SET 状态='待投' WHERE 事件号=?", (eid,))
    conn.commit()
    conn.close()
    r2 = 投递.投递一次(eid)
    assert r2["状态"] == "成功" and r2["密钥版本"] == "v2" and r2.get("同票") is True

    audit = 互证.对账密钥版本()
    a = _端点审计(audit)
    assert a["新版成功票"] >= 1
    assert a["新版已齐"] is True, "v2 同票成功补齐后，新版才算齐"
    assert eid not in audit["旧签冒新事件"]
    assert audit["同票异常数"] == 0
