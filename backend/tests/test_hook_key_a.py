# -*- coding: utf-8 -*-
"""失败测甲 · 密钥热更硬闸 —— 归属服务：投递服务（app/services/投递.py）。

事故命题：端点密钥已经热更升档，投递进程仍握开机那把 v1 旧钥去签，对端拒签，
本仓尝试表却记“成功”。本测试钉死：

1. 热更后，任何用旧密钥版本签出的票都不得落为“成功”，必须按对端裁决记失败 + 中文拒因；
2. 读库内最新密钥版本与写尝试必须同一事务同成同败，成功票必须封物化快照且正式/影子同票；
3. 用库内现行版本正常投递必须成功，且尝试票自证版本 = 库内最新。

在未接线的主干上：``签名目录.热更密钥/最新密钥版本`` 不存在、``投递一次`` 不接受
``密钥版本`` 且无条件记成功 —— 本文件必须先红，救绿责任在投递服务 + 签名目录服务。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook_key_a_env_")
os.environ["数据目录"] = _TMP
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

import app.db as db  # noqa: E402
from app import seed  # noqa: E402
from app.services import 入队, 投递, 签名目录  # noqa: E402

EP = "ep-billing"


@pytest.fixture()
def 库(tmp_path):
    old_dir, old_path = db.数据目录, db.库路径
    db.数据目录 = tmp_path
    db.库路径 = tmp_path / "hook_key_a.db"
    seed.初始化库()
    try:
        yield db.库路径
    finally:
        db.数据目录, db.库路径 = old_dir, old_path


def _灌一票待投(摘要: str = "inv=a-1"):
    return 入队.建事件(
        {"事件类型": "invoice.paid", "端点键": EP, "载荷摘要": 摘要, "优先级": "紧急"}
    )


def test_热更服务已接线且库内最新版本可读(库):
    assert 签名目录.最新密钥版本(EP) == "v1"
    out = 签名目录.热更密钥(EP)
    assert out["密钥版本"] == "v2"
    assert 签名目录.最新密钥版本(EP) == "v2"
    # 密钥版本徽章所需字段
    徽章 = 签名目录.密钥版本徽章(EP)
    assert 徽章["端点键"] == EP and 徽章["密钥版本"] == "v2"


def test_甲_热更后仍用旧钥签出成功尝试_必须被拒(库):
    签名目录.热更密钥(EP)  # v1 -> v2
    ev = _灌一票待投("inv=old-key")
    eid = ev["事件号"]

    # 模拟“仍握开机 v1 旧钥”的投递进程，显式按旧版本封票投递
    rep = 投递.投递一次(eid, 密钥版本="v1")

    # —— 关键断言：旧钥票绝不能是成功 ——
    assert rep["状态"] == "失败", "热更后用 v1 旧钥签名必须被硬闸拦下，不得记成功"
    assert int(rep.get("响应码") or 0) in (401, 409)
    拒因 = str(rep.get("拒因") or "")
    assert 拒因, "必须落中文拒因"
    assert any(k in 拒因 for k in ("旧", "回刷", "拒签", "升档")), 拒因

    detail = 签名目录.事件一条(eid)
    assert detail["状态"] != "已投递", "旧签未被对端受理，事件不得转已投递、不得进水水位"
    row = detail["尝试"][0]
    assert row["状态"] == "失败"
    assert row["密钥版本"] == "v1"
    assert row["拒因"]

    # 尝试表里不存在该事件的“v1 + 成功”票
    旧成功 = [
        a
        for a in 签名目录.投递列表()
        if a["事件号"] == eid and a["密钥版本"] == "v1" and a["状态"] == "成功"
    ]
    assert 旧成功 == [], "旧钥签出的成功票不得进现行流水"


def test_甲_现行新钥投递成功_物化包与尝试表同票(库):
    签名目录.热更密钥(EP)  # v2
    ev = _灌一票待投("inv=new-key")
    rep = 投递.投递一次(ev["事件号"])

    assert rep["状态"] == "成功"
    assert rep["事件状态"] == "已投递"
    assert rep["密钥版本"] == "v2"
    assert rep["验签状态"] == "通过"
    assert rep.get("同票") is True

    row = 签名目录.事件一条(ev["事件号"])["尝试"][0]
    assert row["密钥版本"] == "v2"
    assert row["验签状态"] == "通过"
    assert row["物化包摘要"]
    assert row.get("同票") is True, "正式尝试与影子物化包必须同票"
    assert row.get("是否现行版本") is True
