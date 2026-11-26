# -*- coding: utf-8 -*-
"""争用失败测丙 · 热更当刻双请求必须打同一把锁 —— 归属服务：并发投递锁服务。

事故命题：热更密钥的同一刻两路投递若没有互斥，会出现一边用新钥、一边用旧钥的撕裂，
旧签成功进而污染已投递水位。本文件钉死三件事：

1. ``app.services.并发锁.同锁`` 是真互斥：临界区内最大并发持有者必须为 1；
2. 热更与投递打同一个端点锁，热更完成后并发双投必须同钥同版本、双双同票成功；
3. 热更当刻混合并发（1 热更 + N 投递）不得抛 sqlite 锁错，且现行水位里的成功票
   必须全部等于库内最新版本且正式/影子同票，旧票只能进旧签桶，不得混入现行水位。

未接线主干没有 ``app.services.并发锁``（``modules/并发投递锁`` 是空桩）—— 本文件先红，
救绿责任在并发锁服务 + 投递/热更对同一把锁的接入。
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="hook_key_c_env_")
os.environ["数据目录"] = _TMP
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")

import pytest  # noqa: E402

import app.db as db  # noqa: E402
from app import seed  # noqa: E402
from app.services import 入队, 投递, 签名目录, 互证, 并发锁  # noqa: E402

EP = "ep-billing"
LOCK_NAME = f"endpoint:{EP}"


@pytest.fixture()
def 库(tmp_path):
    old_dir, old_path = db.数据目录, db.库路径
    db.数据目录 = tmp_path
    db.库路径 = tmp_path / "hook_key_c.db"
    seed.初始化库()
    try:
        yield db.库路径
    finally:
        db.数据目录, db.库路径 = old_dir, old_path


def test_丙_同锁临界区最大并发为1(库):
    线程数 = 8
    栅 = threading.Barrier(线程数)
    护 = threading.Lock()
    状态 = {"在內": 0, "最大": 0}

    def 工():
        栅.wait()
        with 并发锁.同锁("ep-mutex"):
            with 护:
                状态["在內"] += 1
                状态["最大"] = max(状态["最大"], 状态["在內"])
            time.sleep(0.02)  # 放大交叠；若无真锁几乎必然 >1
            with 护:
                状态["在內"] -= 1

    ts = [threading.Thread(target=工) for _ in range(线程数)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert 状态["最大"] == 1, f"同锁未互斥，临界区最大并发={状态['最大']}"


def test_丙_热更完成后并发双投同钥同版本且打同锁(库):
    签名目录.热更密钥(EP)  # 先升 v2
    e1 = 入队.建事件({"事件类型": "invoice.paid", "端点键": EP, "载荷摘要": "c-1"})["事件号"]
    e2 = 入队.建事件({"事件类型": "invoice.paid", "端点键": EP, "载荷摘要": "c-2"})["事件号"]

    栅 = threading.Barrier(2)
    结果: dict = {}
    错: list = []

    def 投(号: str, eid: str):
        try:
            栅.wait()
            结果[号] = 投递.投递一次(eid)
        except BaseException as exc:  # noqa: BLE001
            错.append(exc)

    ta = threading.Thread(target=投, args=("甲", e1))
    tb = threading.Thread(target=投, args=("乙", e2))
    ta.start(); tb.start(); ta.join(); tb.join()

    assert not 错, f"并发投递报错: {错!r}"
    for 号 in ("甲", "乙"):
        r = 结果[号]
        assert r["状态"] == "成功"
        assert r["密钥版本"] == "v2", "热更已完成，双投必须同读库内 v2，不得一新一旧"
        assert r.get("同票") is True

    名册 = 并发锁.锁名册()
    assert LOCK_NAME in 名册, "投递必须打在端点同名锁上"
    assert 名册[LOCK_NAME]["累计进入"] >= 2


def test_丙_热更当刻混合并发_旧签不冲现行水位(库):
    轮数 = 6
    错: list = []
    所有返回: list = []

    for 轮 in range(轮数):
        # 每轮重新升到一个新版本，并灌两张待投票
        签名目录.热更密钥(EP)
        版本前 = 签名目录.最新密钥版本(EP)
        ids = [
            入队.建事件(
                {"事件类型": "invoice.paid", "端点键": EP, "载荷摘要": f"c-mix-{轮}-{i}"}
            )["事件号"]
            for i in range(2)
        ]
        栅 = threading.Barrier(3)

        def 热更():
            try:
                栅.wait()
                签名目录.热更密钥(EP)
            except BaseException as exc:  # noqa: BLE001
                错.append(exc)

        def 投(eid: str):
            try:
                栅.wait()
                所有返回.append(投递.投递一次(eid))
            except BaseException as exc:  # noqa: BLE001
                错.append(exc)

        ts = [threading.Thread(target=热更)] + [
            threading.Thread(target=投, args=(e,)) for e in ids
        ]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert 版本前  # 仅为可读性

    assert not 错, f"热更/投递争用出现异常（含 sqlite 锁）: {错!r}"

    # 每张成功票都必须验签通过且同票，绝不允许“成功但验签拒”的假绿
    for r in 所有返回:
        if r.get("状态") == "成功":
            assert r.get("验签状态") == "通过"
            assert r.get("同票") is True

    # 现行水位里的成功票必须全部等于库内最终最新版本；旧票只许待在旧签桶
    最新 = 签名目录.最新密钥版本(EP)
    水位 = 签名目录.投递水位(EP)
    assert 水位["现行版本"] == 最新
    for row in 水位["成功票"]:
        assert row["密钥版本"] == 最新
        assert row.get("同票") is True
    for row in 水位["旧签票"]:
        assert row["密钥版本"] != 最新

    audit = 互证.对账密钥版本(EP)
    assert audit["同票异常数"] == 0, "争用后正式尝试与影子物化包必须仍同票"
