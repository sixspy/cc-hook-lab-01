#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""骨签 hook-01 · 并发双请求打同锁脚本。

对着真实跑起来的 HTTP 服务验证：
  1. 热更完成后，两路 Barrier 齐发的投递都认库内最新钥、同票、验签通过；
  2. 热更当刻「一热更 + 两投递」三路齐发，进程内端点锁把临界区串行化，
     不允许出现「成功票却被对端拒签 / 影子与正式异票」；
  3. /api/锁 名册里 endpoint:<端点> 累计进入增长；
  4. /api/水位 新旧票分轨，/api/对账密钥 同票异常数为 0。

用法：
  python scripts/concurrent_key_lock.py [base_url]
默认 base_url=http://127.0.0.1:8500
退出码 0 = 全部 invariant 成立；1 = 有破口。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8500"
# 每次跑用独立端点，避免历史票污染收尾的「新版已齐」断言。
EP = "ep-lock-" + uuid.uuid4().hex[:6]
EP_RACE = EP + "r"
LOCK_NAME = "endpoint:" + EP
RACE_LOCK_NAME = "endpoint:" + EP_RACE

_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def call(method: str, path: str, body: dict | None = None):
    # urllib 只收 ASCII URL，把中文路径段百分号编码（保留查询符号）。
    url = BASE + urllib.parse.quote(path, safe="/?&=%:+")
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"错误": raw}


_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [OK] " if cond else "  [破] ") + msg)
    if not cond:
        _failures.append(msg)


def must(cond: bool, msg: str) -> None:
    if not cond:
        print("致命：" + msg)
        sys.exit(2)


def register(ep: str) -> None:
    st, r = call("POST", "/api/端点", {
        "端点键": ep, "名称": "并发锁验证端点 " + ep,
        "回调URL": "https://peer.example/hook/lock", "密钥版本": "v1",
    })
    if st == 200:
        print(f"[准备] 已登记 {ep}")
    else:
        print(f"[准备] {ep} 已存在或登记返回 {st}（沿用）")
        call("POST", "/api/端点/启用", {"端点键": ep, "启用": True})


def setup() -> None:
    st, r = call("POST", "/api/登录", {"账号": "admin", "密码": "123456"})
    must(st == 200 and (not isinstance(r, dict) or r.get("ok", True) is not False),
         f"登录失败 {st} {r}")
    print(f"[准备] 已登录 {BASE}")
    register(EP)
    register(EP_RACE)

    st, badge = call("GET", f"/api/密钥版本/{EP}")
    must(st == 200 and isinstance(badge, dict), f"读不到密钥徽章 {st} {badge}")
    print(f"[准备] {EP} 当前徽章：{badge.get('徽章')}")


def enqueue(eid: str, ep: str = EP) -> None:
    st, r = call("POST", "/api/事件/入队", {
        "事件号": eid, "事件类型": "lock.probe", "端点键": ep,
        "载荷摘要": f"probe={eid}", "优先级": "普通",
    })
    if st != 200 and isinstance(r, dict) and "已存在" not in str(r.get("错误", "")):
        must(False, f"入队失败 {eid}: {st} {r}")


def fire_concurrent(named_jobs):
    """named_jobs: [(名字, method, path, body)]，Barrier 齐发。"""
    barrier = threading.Barrier(len(named_jobs))
    results: dict[str, tuple[int, dict | None, float]] = {}

    def worker(name, method, path, body):
        barrier.wait()
        t0 = time.perf_counter()
        st, r = call(method, path, body)
        results[name] = (st, r, (time.perf_counter() - t0) * 1000)

    threads = [threading.Thread(target=worker, args=j) for j in named_jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def phase_same_version() -> None:
    print("\n[阶段一] 先热更到新版，再双请求齐发投递（必须同新版、同票）")
    st, rot = call("POST", "/api/密钥/热更", {"端点键": EP})
    must(st == 200 and isinstance(rot, dict) and rot.get("密钥版本"),
         f"热更失败 {st} {rot}")
    latest = rot["密钥版本"]
    print(f"  热更完成：{rot.get('原版本')} -> {latest}")

    e1, e2 = f"EVT-LOCK-A-{int(time.time())}-1", f"EVT-LOCK-A-{int(time.time())}-2"
    enqueue(e1)
    enqueue(e2)
    res = fire_concurrent([
        ("投递甲", "POST", "/api/投递", {"事件号": e1}),
        ("投递乙", "POST", "/api/投递", {"事件号": e2}),
    ])
    versions = []
    for name in ("投递甲", "投递乙"):
        st, r, ms = res[name]
        print(f"  {name}: HTTP {st} {ms:.0f}ms -> "
              f"状态={r and r.get('状态')} 版本={r and r.get('密钥版本')} "
              f"验签={r and r.get('验签状态')} 同票={r and r.get('同票')} "
              f"拒因={r and r.get('拒因')}")
        check(st == 200, f"{name} HTTP 200（实 {st} {r and r.get('错误')}）")
        if isinstance(r, dict):
            check(r.get("状态") == "成功", f"{name} 投递成功")
            check(r.get("密钥版本") == latest, f"{name} 签的是库内最新版 {latest}")
            check(r.get("验签状态") == "通过", f"{name} 对端验签通过")
            check(r.get("同票") is True, f"{name} 正式尝试与物化包同票")
            check(r.get("锁键") == LOCK_NAME, f"{name} 打在同锁 {LOCK_NAME}")
            versions.append(r.get("密钥版本"))
    check(len(set(versions)) == 1, "两路投递密钥版本一致（无一边新一边旧）")


def phase_hot_reload_race() -> None:
    print(f"\n[阶段二] 热更当刻：一热更 + 两投递 Barrier 齐发打同锁（{EP_RACE}）")
    stamp = int(time.time())
    e1, e2 = f"EVT-LOCK-B-{stamp}-1", f"EVT-LOCK-B-{stamp}-2"
    enqueue(e1, EP_RACE)
    enqueue(e2, EP_RACE)
    res = fire_concurrent([
        ("热更", "POST", "/api/密钥/热更", {"端点键": EP_RACE}),
        ("投递丙", "POST", "/api/投递", {"事件号": e1}),
        ("投递丁", "POST", "/api/投递", {"事件号": e2}),
    ])
    rot_st, rot, _ = res["热更"]
    print(f"  热更: HTTP {rot_st} -> {rot and rot.get('原版本')}->{rot and rot.get('密钥版本')}")
    check(rot_st == 200 and isinstance(rot, dict), "热更请求成功（同锁串行，不报错）")

    for name in ("投递丙", "投递丁"):
        st, r, ms = res[name]
        print(f"  {name}: HTTP {st} {ms:.0f}ms -> "
              f"状态={r and r.get('状态')} 版本={r and r.get('密钥版本')} "
              f"验签={r and r.get('验签状态')} 同票={r and r.get('同票')} "
              f"锁键={r and r.get('锁键')} 拒因={r and r.get('拒因')}")
        check(st == 200, f"{name} HTTP 200（实 {st} {r and r.get('错误')}）")
        if isinstance(r, dict) and r.get("状态") == "成功":
            # 锁互斥下，成功票必是「封包瞬间的现行版」且对端认、影子同票，
            # 绝不允许成功 + 拒签 / 异票的假绿。
            check(r.get("验签状态") == "通过", f"{name} 成功票必过对端验签（无假绿）")
            check(r.get("同票") is True, f"{name} 成功票必同票")
            check(bool(r.get("密钥版本")), f"{name} 成功票留有封票版本")
            check(r.get("锁键") == RACE_LOCK_NAME, f"{name} 打在端点同锁 {RACE_LOCK_NAME}")
        elif isinstance(r, dict):
            print(f"    （{name} 未成功：{r.get('拒因') or r.get('事件状态')} —— "
                  f"串行裁决下允许失败，但不得污染水位）")

    # 竞态端点的硬 invariant：同票异常必须为 0；审计点名的「旧签冒新」
    # 必须真有旧签成功票支撑（审计不得误判）。
    st, audit = call("GET", f"/api/对账密钥?ep={EP_RACE}")
    must(st == 200 and isinstance(audit, dict), f"竞态端点对账失败 {st}")
    check(int(audit.get("同票异常数", -1)) == 0, "竞态端点影子↔正式同票异常为 0")
    ep_row = next((x for x in audit.get("端点", []) if x.get("端点键") == EP_RACE), None)
    if ep_row:
        for eid in ep_row.get("旧签冒新事件", []):
            st2, detail = call("GET", "/api/事件/" + eid)
            attempts = (detail or {}).get("尝试", []) if isinstance(detail, dict) else []
            has_old_win = any(
                a.get("状态") == "成功" and a.get("密钥版本") != ep_row.get("现行版本")
                for a in attempts
            )
            check(has_old_win, f"审计点名 {eid} 旧签冒新，确有旧版成功票（非误判）")
        print(f"  竞态对账：旧签成功票={audit.get('旧签成功票')} "
              f"旧签冒新={audit.get('旧签冒新事件')}（串行先后的真实记录）")


def phase_lock_and_audit() -> None:
    print("\n[阶段三] 锁名册 / 水位 / 密钥对账")
    st, locks = call("GET", "/api/锁")
    must(st == 200 and isinstance(locks, dict), f"读锁名册失败 {st}")
    info = locks.get(LOCK_NAME)
    print(f"  /api/锁 {LOCK_NAME} = {info}")
    check(bool(info) and int(info.get("累计进入", 0)) >= 2,
          f"同锁累计进入 >= 2（实 {info and info.get('累计进入')}）")

    st, wm = call("GET", f"/api/水位?ep={EP}")
    must(st == 200 and isinstance(wm, dict), f"读水位失败 {st} {wm}")
    latest = wm.get("现行版本")
    成功票 = wm.get("成功票") or []
    旧签票 = wm.get("旧签票") or []
    print(f"  水位：现行={latest} 现行成功票={len(成功票)} 旧签票={len(旧签票)} "
          f"异票={wm.get('异票数', wm.get('同票异常数', '?'))}")
    bad_new = [t for t in 成功票 if t.get("密钥版本") != latest]
    check(not bad_new, "水位成功票全部是现行版本（旧签不污染已投递水位）")
    bad_ticket = [t for t in 成功票 if t.get("同票") is False]
    check(not bad_ticket, "水位成功票全部同票")
    bad_old = [t for t in 旧签票 if t.get("密钥版本") == latest]
    check(not bad_old, "旧签档不含现行版本票（新旧分轨）")

    st, audit = call("GET", f"/api/对账密钥?ep={EP}")
    must(st == 200 and isinstance(audit, dict), f"密钥对账失败 {st} {audit}")
    print(f"  对账（干净端点）：对齐={audit.get('对齐')} 新版成功票={audit.get('新版成功票')} "
          f"旧签成功票={audit.get('旧签成功票')} 同票异常={audit.get('同票异常数')} "
          f"旧签冒新={audit.get('旧签冒新事件')}")
    check(int(audit.get("同票异常数", -1)) == 0, "干净端点影子↔正式同票异常数为 0")
    check(not audit.get("旧签冒新事件"), "干净端点无旧签冒新事件")
    check(int(audit.get("旧签成功票", -1)) == 0, "干净端点无旧签成功票")
    check(audit.get("对齐") is True, "干净端点密钥对账对齐（新版已齐）")
    ep_row = next((x for x in audit.get("端点", []) if x.get("端点键") == EP), None)
    check(bool(ep_row and ep_row.get("新版已齐") is True), "端点报告『新版已齐』=true")


def main() -> int:
    print(f"=== hook-01 并发同锁验证 · 目标 {BASE} · 端点 {EP} ===")
    setup()
    phase_same_version()
    phase_hot_reload_race()
    phase_lock_and_audit()
    print("\n" + "=" * 60)
    if _failures:
        print(f"结论：{len(_failures)} 项 invariant 破裂：")
        for f in _failures:
            print("  - " + f)
        return 1
    print("结论：全部 invariant 成立 ✅  两路请求确打同锁，无一边新一边旧，无旧签污染水位。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
