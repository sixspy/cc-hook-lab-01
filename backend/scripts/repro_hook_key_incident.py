"""密钥换版事故夹具 · hook-01 硬闸验证脚本。

流程严格按夹具名：「改密钥版本 → 灌待投事件 → 点投递」。

动主干之前，本脚本打出过事故（对端只信 v2 已拒签，本仓尝试表却记『成功』）。
hook-01 落地后，它验证同一事故路径已被硬闸拦住：

1. 用正式的「热更密钥」把 ep-billing 升档 v1 -> v2（库内权威 + 端点锁）；
2. 运行时桩模拟“投递进程仍握开机那把 v1 旧钥”——无论服务读到什么版本，
   签名字节都用开机缓存的 v1 材料；
3. 点投递：读库内最新版本与写尝试在同一把端点锁 / 同一个
   ``BEGIN IMMEDIATE`` 事务里完成；新规验签当场识破“版本标 v2、字节是 v1”
   的撕裂票（或旧版硬闸），尝试必须记『失败』并落中文拒因，
   事件不得转『已投递』，互证不得把它认成新版已齐。

退出码 0 = 硬闸确实拦住假绿；1 = 仍存在假绿（回归）；2 = 脚本自身错误。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

_TMP = tempfile.mkdtemp(prefix="hook_key_gate_")
os.environ["数据目录"] = _TMP
os.environ["夹具目录"] = str(_ROOT.parent / "fixtures")

from app import seed  # noqa: E402
from app.services import 入队, 投递, 互证, 签名目录  # noqa: E402

夹具路径 = _ROOT.parent / "fixtures" / "密钥换版事故.json"
夹具 = json.loads(夹具路径.read_text(encoding="utf-8"))


def main() -> int:
    seed.初始化库()
    ep键 = 夹具["密钥换版"]["端点键"]

    print("=" * 72)
    print("步骤1  正式热更密钥 v1 -> v2（库内权威，单调升档，端点同锁）")
    rot = 签名目录.热更密钥(ep键)
    print(f"  {rot['端点键']} {rot['原版本']} -> {rot['密钥版本']}（{rot['徽章']}）")

    print("=" * 72)
    print("步骤2  运行时桩：投递进程仍握开机那把 v1 旧钥出签名字节")
    原签名 = 投递._签名

    def 开机旧钥签名(_版本, 事件号, 摘要, 密钥材料=None):
        # 故意无视服务从库内读到的版本/材料，永远用开机缓存的 v1 派生法
        return 原签名("v1", 事件号, 摘要, 签名目录._默认材料("v1"))

    投递._签名 = 开机旧钥签名

    print("=" * 72)
    print("步骤3  灌夹具待投事件")
    事件 = [入队.建事件(item) for item in 夹具["事件"]]
    for ev in 事件:
        print("  入队", ev["事件号"], ev["端点键"], ev["状态"])

    print("=" * 72)
    print("步骤4  点投递（第一票）：硬闸必须当场拦成失败，不得记假成功")
    target = 事件[0]
    eid = target["事件号"]
    rep = 投递.投递一次(eid)
    print("  投递返回：", rep.get("状态"), "/ 事件状态", rep.get("事件状态"),
          "/ 封票版本", rep.get("密钥版本"), "/ 验签", rep.get("验签状态"))
    print("  中文拒因：", rep.get("拒因"))

    row = 签名目录.事件一条(eid)
    尝试 = row["尝试"][0]
    print("  尝试表落库：", 尝试["尝试号"], "状态=", 尝试["状态"],
          "响应码=", 尝试["响应码"], "同票=", 尝试["同票"])

    audit = 互证.对账密钥版本(ep键)
    ep行 = next((e for e in audit["端点"] if e["端点键"] == ep键), {})
    print("  密钥对账  ：对齐=", audit["对齐"],
          "旧签成功票=", audit["旧签成功票"],
          "同票异常=", audit["同票异常数"],
          "新版已齐=", ep行.get("新版已齐"))

    假绿 = (尝试["状态"] == "成功" or rep.get("状态") == "成功")
    事件被污染 = row["状态"] == "已投递"
    # 只有“确有已投递事件、却零新版票还报新版已齐/旧签冒新”才算对账误判；
    # 本次已被拦成失败、事件仍在投递中，零已投递下的空齐不是假绿。
    对账误齐 = bool(audit.get("旧签冒新事件")) or (
        ep行.get("已投递事件数", 0) > 0
        and ep行.get("新版成功票", 0) == 0
        and ep行.get("新版已齐") is True
    )
    if 假绿 or 事件被污染 or 对账误齐:
        print("!" * 72)
        print("硬闸失效 [假绿回归]：旧钥/撕裂票竟成功，或事件已投递、对账误判新版已齐。")
        return 1

    print("=" * 72)
    print("硬闸已拦住事故 ✅  旧钥签不出成功票，事件未投递，对账未误齐。")

    # 恢复真实签名函数后，用库内最新钥补投，证明同一条链路可救绿。
    投递._签名 = 原签名
    print("=" * 72)
    print("步骤5  恢复库内最新钥签名并补投：必须 v2 成功 / 验签通过 / 同票")
    ok_rep = 投递.投递一次(eid)
    print("  补投：", ok_rep.get("状态"), ok_rep.get("密钥版本"),
          ok_rep.get("验签状态"), "同票=", ok_rep.get("同票"))
    if not (ok_rep.get("状态") == "成功" and ok_rep.get("验签状态") == "通过"
            and ok_rep.get("同票") is True):
        print("补投未达预期，硬闸可能误伤正常新钥投递。")
        return 1
    audit2 = 互证.对账密钥版本(ep键)
    ep2 = next((e for e in audit2["端点"] if e["端点键"] == ep键), {})
    print("  补投后对账：新版已齐=", ep2.get("新版已齐"),
          "新版成功票=", ep2.get("新版成功票"), "对齐=", audit2["对齐"])
    print("结论：事故可复现路径已转成『硬闸拦截 → 新钥救绿』，退出码 0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
