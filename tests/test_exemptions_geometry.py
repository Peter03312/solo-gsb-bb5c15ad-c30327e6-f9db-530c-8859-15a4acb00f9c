"""最小区域豁免预演引擎（exemptions.plan_minimum_exemptions）的离线验证。

直接 `python3 tests/test_exemptions_geometry.py` 运行；失败时退出码非 0。
不依赖数据库（但需要 shapely，与 test_schedule_geometry.py 相同）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shapely.geometry import Polygon

from app import exemptions, geometry


def E(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def P(t: float, x: float, y: float):
    return (t, x, y)


def region(rid, vf, vt, ring, name="R"):
    return {
        "id": rid,
        "name": name,
        "valid_from": E(vf),
        "valid_to": E(vt),
        "polygon": Polygon(ring),
    }


failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def eq_epoch(value: float, iso: str, tol: float = 1e-6) -> bool:
    return abs(value - E(iso)) <= tol


SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]
SQUARE2 = [[20, 0], [30, 0], [30, 10], [20, 10]]
HIGH = [[0, 50], [10, 50], [10, 60], [0, 60]]

# 行程：相对 0s 在 (-5,5) → 60s 在 (15,5)；对 SQUARE 的接触为相对 [15s,45s]
TRAJ = [P(0, -5, 5), P(60, 15, 5)]
WS = E("2030-06-01T00:00:00Z")
WE = E("2030-06-01T12:00:00Z")

# 覆盖整窗的宽生效窗：禁止区间 [vf-45s, vt-15s) ⊇ 候选窗口
WIDE = ("2020-01-01T00:00:00Z", "2032-01-01T00:00:00Z")


def run(candidate_ids, regions, traj=TRAJ):
    return exemptions.plan_minimum_exemptions(
        traj, regions, list(candidate_ids), WS, WE
    )


def intervals_match_schedule(plan, regions, exempt_ids, traj=TRAJ, tol=1e-6):
    """预演给出的完整安全区间必须等于「豁免后集合」直接调度的结果。"""
    remaining = [r for r in regions if r["id"] not in set(exempt_ids)]
    expected = geometry.safe_departure_intervals(traj, remaining, WS, WE)
    got = plan["intervals"]
    if len(got) != len(expected):
        return False
    return all(
        abs(got[i]["start"] - expected[i]["start"]) <= tol
        and abs(got[i]["end"] - expected[i]["end"]) <= tol
        and got[i]["start_inclusive"] is True
        and got[i]["end_inclusive"] is False
        for i in range(len(got))
    )


def one_interval(plan, s_iso, e_iso):
    return (
        len(plan["intervals"]) == 1
        and eq_epoch(plan["intervals"][0]["start"], s_iso)
        and eq_epoch(plan["intervals"][0]["end"], e_iso)
    )


# ---------------------------------------------------------------- 1. 原本安全

plan = run([], [])
check("无区域：可行、豁免集为空、区间等于整窗",
      plan["feasible"] is True
      and plan["exempt_region_ids"] == []
      and eq_epoch(plan["earliest_departure"], "2030-06-01T00:00:00Z")
      and one_interval(plan, "2030-06-01T00:00:00Z", "2030-06-01T12:00:00Z"),
      str(plan))

R_HIGH = region(9, *WIDE, HIGH)
plan = run([9], [R_HIGH])
check("区域与行程几何不相交：可行、豁免集为空",
      plan["feasible"] is True and plan["exempt_region_ids"] == [], str(plan))

# 区域只挡窗口中段、窗口前后本来就安全 → 豁免集为空
R_MID = region(2, "2030-06-01T03:00:00Z", "2030-06-01T04:00:00Z", SQUARE)
# 生效 [03:00,04:00)、接触 [15,45] → 禁止 [02:59:15, 03:59:45)，窗口前段安全
plan = run([2], [R_MID])
check("窗口内本来就有安全时刻：豁免集为空，最早时刻为窗口起点",
      plan["feasible"] is True and plan["exempt_region_ids"] == []
      and eq_epoch(plan["earliest_departure"], "2030-06-01T00:00:00Z")
      and intervals_match_schedule(plan, [R_MID], []),
      str(plan))

# ---------------------------------------------------------------- 2. 单区域豁免

R1 = region(1, *WIDE, SQUARE)
plan = run([1], [R1])
check("单区域完全封锁：豁免 {1} 后整窗安全",
      plan["feasible"] is True and plan["exempt_region_ids"] == [1]
      and eq_epoch(plan["earliest_departure"], "2030-06-01T00:00:00Z")
      and one_interval(plan, "2030-06-01T00:00:00Z", "2030-06-01T12:00:00Z")
      and intervals_match_schedule(plan, [R1], [1]),
      str(plan))

# 候选中含几何无关区域：不应进入豁免方案
plan = run([1, 9], [R1, R_HIGH])
check("无关候选区域不进入豁免方案",
      plan["feasible"] is True and plan["exempt_region_ids"] == [1], str(plan))

# ---------------------------------------------------------------- 3. 不可豁免阻挡

R_BLOCKER = region(7, *WIDE, SQUARE, name="不可豁免")
plan = run([1], [R1, R_BLOCKER])
check("不可豁免区域完全封锁：无可行方案",
      plan["feasible"] is False and plan["exempt_region_ids"] == []
      and plan["earliest_departure"] is None and plan["intervals"] == [],
      str(plan))

# blocker 只封锁窗口后段；候选豁免后只能在 blocker 的禁止区间之前出发
# blocker 生效 [06:00:45, 次日) → 禁止 [06:00:00, ...)
R_BL_TAIL = region(7, "2030-06-01T06:00:45Z", "2030-06-02T00:00:00Z", SQUARE)
plan = run([1], [R1, R_BL_TAIL])
check("候选全窗封锁 + 不可豁免封锁后段：豁免 {1}，安全区间被 blocker 截断",
      plan["feasible"] is True and plan["exempt_region_ids"] == [1]
      and eq_epoch(plan["earliest_departure"], "2030-06-01T00:00:00Z")
      and one_interval(plan, "2030-06-01T00:00:00Z", "2030-06-01T06:00:00Z")
      and intervals_match_schedule(plan, [R1, R_BL_TAIL], [1]),
      str(plan))

# blocker 只封锁窗口前段：豁免 {1} 后安全时刻最早为 blocker 禁止区间终点
# blocker 生效 (起于很久以前, 06:00:15] → 禁止 [..., 06:00:00)
R_BL_HEAD = region(7, "2020-01-01T00:00:00Z", "2030-06-01T06:00:15Z", SQUARE)
plan = run([1], [R1, R_BL_HEAD])
check("候选全窗封锁 + 不可豁免封锁前段：豁免 {1}，最早时刻为 06:00:00",
      plan["feasible"] is True and plan["exempt_region_ids"] == [1]
      and eq_epoch(plan["earliest_departure"], "2030-06-01T06:00:00Z")
      and one_interval(plan, "2030-06-01T06:00:00Z", "2030-06-01T12:00:00Z")
      and intervals_match_schedule(plan, [R1, R_BL_HEAD], [1]),
      str(plan))

# ---------------------------------------------------------------- 4. 豁免数最少优先
# 区域 3 禁止 [00:00:00,06:00:00)，区域 4 禁止 [03:00:00,12:00:00)：
# 豁免 3 → 剩 4 封 [03:00,12:00)，安全 [00:00,03:00)（1 个豁免，最早 00:00）
# 豁免 4 → 剩 3 封 [00:00,06:00)，安全 [06:00,12:00]（1 个豁免，最早 06:00）
# 豁免 {3,4} → 整窗安全（2 个豁免）→ 1 个豁免更优，且 {3} 最早时刻更早
R3 = region(3, "2030-06-01T00:00:45Z", "2030-06-01T06:00:15Z", SQUARE)
R4 = region(4, "2030-06-01T03:00:45Z", "2030-06-01T12:00:15Z", SQUARE)
plan = run([3, 4], [R3, R4])
check("重叠半窗：单豁免足够；豁免 {3} 拿到窗口起点（最早时刻主导）",
      plan["feasible"] is True and plan["exempt_region_ids"] == [3]
      and eq_epoch(plan["earliest_departure"], "2030-06-01T00:00:00Z")
      and one_interval(plan, "2030-06-01T00:00:00Z", "2030-06-01T03:00:00Z")
      and intervals_match_schedule(plan, [R3, R4], [3]),
      str(plan))

# 必须豁免两个：行程依次穿过两个宽窗方块
TRAJ_CROSS = [P(0, -5, 5), P(120, 35, 5)]
RC1 = region(3, *WIDE, SQUARE)
RC2 = region(4, *WIDE, SQUARE2)
plan = run([3, 4], [RC1, RC2], traj=TRAJ_CROSS)
check("行程穿过两个宽窗区域：必须豁免 {3,4}",
      plan["feasible"] is True and plan["exempt_region_ids"] == [3, 4]
      and eq_epoch(plan["earliest_departure"], "2030-06-01T00:00:00Z")
      and one_interval(plan, "2030-06-01T00:00:00Z", "2030-06-01T12:00:00Z")
      and intervals_match_schedule(plan, [RC1, RC2], [3, 4], traj=TRAJ_CROSS),
      str(plan))

# 互补两半：3 封 [00:00,06:00)、4 封 [06:00,12:00)，豁免其一即安全；
# 豁免 {3} 后剩 4 → 安全 [00:00,06:00)，最早 00:00 → {3} 胜
RA = region(3, "2030-06-01T00:00:45Z", "2030-06-01T06:00:15Z", SQUARE)
RB = region(4, "2030-06-01T06:00:45Z", "2030-06-01T12:00:15Z", SQUARE)
plan = run([3, 4], [RA, RB])
check("互补两半：豁免 {3} 拿到窗口起点（最早时刻优先）",
      plan["feasible"] is True and plan["exempt_region_ids"] == [3]
      and eq_epoch(plan["earliest_departure"], "2030-06-01T00:00:00Z")
      and one_interval(plan, "2030-06-01T00:00:00Z", "2030-06-01T06:00:00Z")
      and intervals_match_schedule(plan, [RA, RB], [3]),
      str(plan))

# 豁免数最少优先于最早时刻：
# 1 封 [00,10)、2 封 [00,06)、3 封 [06,12)（小时单位的禁止带由生效窗平移得到，
# 这里直接用分钟级等价构造：禁止带端点取整小时）
R_O1 = region(
    1, "2030-06-01T00:00:45Z", "2030-06-01T10:00:15Z", SQUARE, name="封前10h"
)  # 禁止 [00:00,10:00)
R_O2 = region(
    2, "2030-06-01T00:00:45Z", "2030-06-01T06:00:15Z", SQUARE, name="封前6h"
)  # 禁止 [00:00,06:00)
R_O3 = region(
    3, "2030-06-01T06:00:45Z", "2030-06-01T12:00:15Z", SQUARE, name="封后6h"
)  # 禁止 [06:00,12:00)
# 空集：并集整窗，不可行；
# {1}：2∪3=整窗，不可行；{2}：1∪3=整窗，不可行；
# {3}：1∪2=[00,10)，安全 [10:00,12:00)，最早 10:00（唯一单豁免可行）；
# {1,2}：剩 3 封 [06,12)，安全 [00,06)，最早 00:00（两个豁免，时刻更早但数多）。
plan = run([1, 2, 3], [R_O1, R_O2, R_O3])
check("豁免数优先：{3}（1 个豁免，最早 10:00）胜 {1,2}（2 个豁免，最早 00:00）",
      plan["feasible"] is True and plan["exempt_region_ids"] == [3]
      and eq_epoch(plan["earliest_departure"], "2030-06-01T10:00:00Z")
      and one_interval(plan, "2030-06-01T10:00:00Z", "2030-06-01T12:00:00Z")
      and intervals_match_schedule(plan, [R_O1, R_O2, R_O3], [3]),
      str(plan))

# ---------------------------------------------------------------- 5. 编号序列
# 两个区域禁止区间完全相同（同为 [00:00,06:00)）：窗口后段本就安全，
# 最优方案是空豁免（无须为已经安全的时刻豁免任何区域）
RN3 = region(3, "2030-06-01T00:00:45Z", "2030-06-01T06:00:15Z", SQUARE)
RN4 = region(4, "2030-06-01T00:00:45Z", "2030-06-01T06:00:15Z", SQUARE)
plan = run([3, 4], [RN3, RN4])
check("两区域禁止区间相同且窗口后段已安全：空豁免（不会多选区域）",
      plan["feasible"] is True and plan["exempt_region_ids"] == []
      and eq_epoch(plan["earliest_departure"], "2030-06-01T06:00:00Z")
      and one_interval(plan, "2030-06-01T06:00:00Z", "2030-06-01T12:00:00Z"),
      str(plan))

# 必须豁免的宽窗区域 + 相同半窗区域：宽窗 3、4 必须豁免；豁免后两个相同
# 半窗区域 5、6 的并集仍只封前半（窗口后段安全），故无需再豁免 5/6
RW3 = region(3, *WIDE, SQUARE, name="宽A")
RW4 = region(4, *WIDE, SQUARE, name="宽B")
RN5 = region(5, "2030-06-01T00:00:45Z", "2030-06-01T06:00:15Z", SQUARE)
RN6 = region(6, "2030-06-01T00:00:45Z", "2030-06-01T06:00:15Z", SQUARE)
plan = run([3, 4, 5, 6], [RW3, RW4, RN5, RN6])
check("宽窗 3、4 必豁免；半窗 5/6 不挡后段 → 方案恰为 {3,4}（不含冗余豁免）",
      plan["feasible"] is True and plan["exempt_region_ids"] == [3, 4]
      and eq_epoch(plan["earliest_departure"], "2030-06-01T06:00:00Z")
      and one_interval(plan, "2030-06-01T06:00:00Z", "2030-06-01T12:00:00Z")
      and intervals_match_schedule(plan, [RW3, RW4, RN5, RN6], [3, 4]),
      str(plan))

# ---------------------------------------------------------------- 6. 边界接触 / 生效端点
# 行程 (-5,-5)→(5,5) 在相对 t=30s 单点擦过顶点 (0,0)
TRAJ_TOUCH = [P(0, -5, -5), P(60, 5, 5)]
# 生效 [00:00:30,12:00:30)、接触 a=b=30s → 禁止 [00:00:00,12:00:00) 整窗
RT = region(11, "2030-06-01T00:00:30Z", "2030-06-01T12:00:30Z", SQUARE)
plan = run([11], [RT], traj=TRAJ_TOUCH)
check("单点接触按闭区间参与：整窗被禁，必须豁免 {11}",
      plan["feasible"] is True and plan["exempt_region_ids"] == [11]
      and eq_epoch(plan["earliest_departure"], "2030-06-01T00:00:00Z")
      and intervals_match_schedule(plan, [RT], [11], traj=TRAJ_TOUCH),
      str(plan))

# 接触恰落在 valid_to：生效 [00:00:00,00:00:30) → 禁止 [2030-05-31T23:59:30,
# 00:00:00)，与窗口不相交 → 整窗安全，豁免为空
RT_OPEN = region(12, "2030-06-01T00:00:00Z", "2030-06-01T00:00:30Z", SQUARE)
plan = run([12], [RT_OPEN], traj=TRAJ_TOUCH)
check("接触时刻恰为 valid_to（右端开）：该出发安全，豁免集为空",
      plan["feasible"] is True and plan["exempt_region_ids"] == []
      and eq_epoch(plan["earliest_departure"], "2030-06-01T00:00:00Z"),
      str(plan))

# ---------------------------------------------------------------- 6b. 端点相接：半开/闭同刻不得采样到中间空覆盖
# 区域 3 禁止 [00:00,06:00)，区域 4 禁止 [06:00,12:00)（在 06:00 端点相接：
# 3 在该点开、4 在该点闭）。06:00 处覆盖集合是 {4} 而不是空集；扫描线在该
# 同刻事件之间不得采样到「移除 3 后、加入 4 前」的瞬时空覆盖——否则会误判
# 空豁免可行且区间为空。
RJ1 = region(3, "2030-06-01T00:00:45Z", "2030-06-01T06:00:15Z", SQUARE)
RJ2 = region(4, "2030-06-01T06:00:45Z", "2030-06-01T12:00:15Z", SQUARE)
plan = run([3, 4], [RJ1, RJ2])
check("禁止区间端点相接：不把同刻中间态当安全点；方案 {3}（最早 00:00）",
      plan["feasible"] is True and plan["exempt_region_ids"] == [3]
      and eq_epoch(plan["earliest_departure"], "2030-06-01T00:00:00Z")
      and all(iv["end"] > iv["start"] for iv in plan["intervals"])
      and intervals_match_schedule(plan, [RJ1, RJ2], [3]),
      str(plan))

# 多个区域在同一刻一起结束/开始：同刻净覆盖可能为空（全部结束且无新开始），
# 该点确实安全（每个区间右端都开）→ 应被采样且区间非空
RG1 = region(5, "2030-06-01T00:00:45Z", "2030-06-01T06:00:15Z", SQUARE)
RG2 = region(6, "2030-06-01T00:00:45Z", "2030-06-01T06:00:15Z", SQUARE)
plan = run([5, 6], [RG1, RG2])
check("两区域同在 06:00 开放：06:00 起空覆盖安全、空豁免可行、区间非空",
      plan["feasible"] is True and plan["exempt_region_ids"] == []
      and eq_epoch(plan["earliest_departure"], "2030-06-01T06:00:00Z")
      and one_interval(plan, "2030-06-01T06:00:00Z", "2030-06-01T12:00:00Z"),
      str(plan))

# ---------------------------------------------------------------- 7. 多段安全区间
# 豁免一个全窗宽窗区域后，剩余 blocker 把窗口切成多段时，intervals 必须完整。
# 生效 [03:00:15,04:00:15)、接触 [15,45] → 禁止 [02:59:30,04:00:00)
RB1 = region(21, "2030-06-01T03:00:15Z", "2030-06-01T04:00:15Z", SQUARE)
RB2 = region(22, "2030-06-01T08:00:15Z", "2030-06-01T09:00:15Z", SQUARE)
plan = run([1], [R1, RB1, RB2])
check("豁免候选后剩余两个 blocker 禁止带：完整安全区间为三段（精确端点）",
      plan["feasible"] is True and plan["exempt_region_ids"] == [1]
      and len(plan["intervals"]) == 3
      and eq_epoch(plan["intervals"][0]["start"], "2030-06-01T00:00:00Z")
      and eq_epoch(plan["intervals"][0]["end"], "2030-06-01T02:59:30Z")
      and eq_epoch(plan["intervals"][1]["start"], "2030-06-01T04:00:00Z")
      and eq_epoch(plan["intervals"][1]["end"], "2030-06-01T07:59:30Z")
      and eq_epoch(plan["intervals"][2]["start"], "2030-06-01T09:00:00Z")
      and eq_epoch(plan["intervals"][2]["end"], "2030-06-01T12:00:00Z")
      and intervals_match_schedule(plan, [R1, RB1, RB2], [1]),
      str(plan))

# ---------------------------------------------------------------- 汇总

print()
if failures:
    print(f"{len(failures)} 个用例失败：{failures}")
    sys.exit(1)
print("全部最小区域豁免引擎离线用例通过")
