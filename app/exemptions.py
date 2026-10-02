"""最小区域豁免预演引擎（纯函数，不依赖数据库/HTTP）。

安全员为一条固定行程评估「最小区域豁免」：在给定的可豁免现有区域集合中
选取一个子集，**仅在本次预演中**忽略（豁免）这些区域，使候选出发窗口内
至少存在一个安全出发时刻。方案选优按以下顺序（字典序）：

  1. 豁免区域数最少；
  2. 该方案下窗口内最早安全出发时刻最早；
  3. 仍并列时，按豁免区域编号升序序列的字典序取小。

沿用既有规则：相邻轨迹点直线匀速、边界接触即违规、区域生效时段
左闭右开 ``[valid_from, valid_to)``、候选窗口左闭右开。

核心观察
--------
对任意一个可行方案（豁免集合 S ⊆ 可豁免集合 C），取其任一安全出发时刻 s，
令 C_s 为「在 s 处造成禁止出发的可豁免区域」集合，则 C_s ⊆ S，且只豁免
C_s 时 s 仍然安全（S\\C_s 中的区域在 s 处本就不禁止任何东西）。因此最优
方案必为某个出发时刻 s 诱导出的集合 C_s——用扫描线沿「候选禁止区间端点 +
不可豁免区间右端」推进覆盖集合即可枚举全部不同的诱导集合，而无须做
2^|C| 的子集穷举，也不在每个方案上重跑几何。
"""
from __future__ import annotations

from bisect import bisect_right

from . import geometry

# 与 geometry.EPS 同量级：仅用于吸收区间端点的浮点误差
_EPS = 1e-9


def _point_in_intervals(t: float, intervals: list[tuple[float, float]]) -> bool:
    """t 是否落在左闭右开区间并集内（区间已按起点排序且互不重叠）。"""
    if not intervals:
        return False
    starts = [lo for lo, _ in intervals]
    i = bisect_right(starts, t) - 1
    if i < 0:
        return False
    lo, hi = intervals[i]
    return lo <= t < hi


def _clip_to_window(
    intervals: list[tuple[float, float]], window_start: float, window_end: float
) -> list[tuple[float, float]]:
    """把左闭右开区间裁剪进候选窗口 [window_start, window_end)，丢弃零长度段。"""
    out: list[tuple[float, float]] = []
    for lo, hi in intervals:
        if hi <= window_start + _EPS or lo >= window_end - _EPS:
            continue
        lo2 = max(lo, window_start)
        hi2 = min(hi, window_end)
        if hi2 - lo2 > _EPS:
            out.append((lo2, hi2))
    return out


def _safe_intervals_from_forbidden(
    forbidden_parts: list[tuple[float, float]],
    window_start: float,
    window_end: float,
) -> list[dict]:
    """窗口减去给定禁止区间（左闭右开）后的安全区间；纯区间运算，与
    geometry.safe_departure_intervals 结果一致但不重跑 Shapely 几何。"""
    merged = geometry.merge_intervals(
        [geometry.EpochInterval(lo, hi) for lo, hi in forbidden_parts]
    )
    safe: list[dict] = []
    cursor = window_start
    for iv in merged:
        lo, hi = iv.start, iv.end
        if hi <= cursor + _EPS:
            continue
        if lo >= window_end - _EPS:
            break
        if lo > cursor + _EPS:
            safe.append({
                "start": cursor,
                "end": min(lo, window_end),
                "start_inclusive": True,
                "end_inclusive": False,
            })
        if hi > cursor:
            cursor = hi
    if cursor < window_end - _EPS:
        safe.append({
            "start": cursor,
            "end": window_end,
            "start_inclusive": True,
            "end_inclusive": False,
        })
    return safe


def plan_minimum_exemptions(
    traj: list[tuple[float, float, float]],
    regions: list[dict],
    candidate_ids: list[int],
    window_start: float,
    window_end: float,
) -> dict:
    """计算最小区域豁免方案。

    traj: 已校验的相对秒数轨迹 [(t, x, y), ...]（首点 t=0、严格递增）。
    regions: 同一已提交快照下的完整区域集合（结构同 geometry 引擎输入）。
    candidate_ids: 可豁免的互异现有区域编号（调用方负责查重与存在性校验）。
    window_start/window_end: UTC epoch 秒表示的候选窗口（左闭右开，起早于止）。

    返回::

        {
          "feasible": bool,
          "exempt_region_ids": [int, ...],      # 可行时为最优豁免编号（升序）
          "earliest_departure": float | None,   # 可行时为该方案最早安全时刻
          "intervals": [ {start, end, start_inclusive, end_inclusive}, ... ],
        }
    """
    candidate_set = set(candidate_ids)

    # 逐区域的禁止出发区间（不做跨区域并集），再裁剪到候选窗口。
    # 后续全部运算只基于这些区间列表，不再重跑 Shapely 几何。
    per_region = geometry.forbidden_departure_intervals_per_region(traj, regions)
    clipped: dict[int, list[tuple[float, float]]] = {}
    blocker_parts: list[tuple[float, float]] = []
    for r in regions:
        rid = r["id"]
        parts = _clip_to_window(per_region.get(rid, []), window_start, window_end)
        if not parts:
            continue
        if rid in candidate_set:
            clipped[rid] = parts
        else:
            blocker_parts.extend(parts)
    # 不可豁免区域的禁止区间取并集：任何可行方案都必须落在这些区间之外
    blockers = [
        (iv.start, iv.end)
        for iv in geometry.merge_intervals(
            [geometry.EpochInterval(lo, hi) for lo, hi in blocker_parts]
        )
    ]

    # 扫描线枚举全部不同的诱导覆盖集合（区间均已裁剪进 [WS, WE)）。
    # 事件 (时刻, 类型, 区域id)：
    #   kind=0：候选区间右端（开端）→ 移除覆盖；
    #   kind=1：候选区间左端（闭端）→ 加入覆盖；
    #   kind=2：不可豁免区间右端（开端）→ 该点重新可出发，仅用于采样。
    # 同刻顺序「先移除、后加入、最后采样 blocker 右端」，对应右端开、左端闭：
    # 区间 A 在 t 结束、B 在同刻 t 开始时，t 已不属于 A 而属于 B。
    # 不可豁免区间内部及左端没有可行点，其左端不产生诱导集合。
    events: list[tuple[float, int, int]] = []
    for rid, parts in clipped.items():
        for lo, hi in parts:
            if lo > window_start + _EPS:
                events.append((lo, 1, rid))
            if hi < window_end - _EPS:
                events.append((hi, 0, rid))
    for lo, hi in blockers:
        if window_start + _EPS < hi < window_end - _EPS:
            events.append((hi, 2, 0))
    events.sort()

    # 窗口左端处的初始候选覆盖（含左端闭）
    covering: set[int] = set()
    for rid, parts in clipped.items():
        for lo, hi in parts:
            if lo <= window_start + _EPS < hi:
                covering.add(rid)

    # 单次扫描同时完成诱导集合枚举与「每个集合最早可行时刻」的记录：
    # 覆盖集合沿事件单调演化，同一集合第一次在可出发位置出现时的时刻，就是
    # 豁免该集合后窗口内的最早安全出发时刻（更早的窗口部分要么被 blocker
    # 占据，要么该集合在那里还包含更多区域）。
    first_seen: dict[frozenset[int], float] = {}

    def sample(t: float) -> None:
        if not _point_in_intervals(t, blockers):
            key = frozenset(covering)
            if key not in first_seen:
                first_seen[key] = t

    sample(window_start)
    # 同一时刻的所有状态变更必须先全部应用再采样一次：半开/闭端点在该时刻
    # 的真实成员集合是「全部移除 + 全部加入」之后的集合，中间态不可出发。
    i = 0
    while i < len(events):
        t = events[i][0]
        j = i
        while j < len(events) and events[j][0] <= t + _EPS:
            _, kind, rid = events[j]
            if kind == 1:
                covering.add(rid)
            elif kind == 0:
                covering.discard(rid)
            j += 1
        sample(t)
        i = j

    # 按（豁免数、最早可行时刻、豁免编号升序序列）选出最优集合；此处只做
    # 集合/标量比较，不为每个候选重算整窗区间。
    best_set: frozenset[int] | None = None
    best_ids: tuple[int, ...] = ()
    best_earliest = 0.0
    for exempt, earliest in first_seen.items():
        ids = tuple(sorted(exempt))
        if best_set is None:
            better = True
        elif len(ids) != len(best_ids):
            better = len(ids) < len(best_ids)
        elif abs(earliest - best_earliest) > _EPS:
            better = earliest < best_earliest
        else:
            better = ids < best_ids
        if better:
            best_set, best_ids, best_earliest = exempt, ids, earliest

    if best_set is None:
        return {
            "feasible": False,
            "exempt_region_ids": [],
            "earliest_departure": None,
            "intervals": [],
        }

    # 只为最优方案构造一次完整安全区间：
    # 窗口 −（blocker 并集 ∪ 未豁免候选区域禁止区间）
    forbidden = list(blocker_parts)
    for rid, parts in clipped.items():
        if rid not in best_set:
            forbidden.extend(parts)
    intervals = _safe_intervals_from_forbidden(
        forbidden, window_start, window_end
    )
    return {
        "feasible": True,
        "exempt_region_ids": list(best_ids),
        "earliest_departure": intervals[0]["start"] if intervals else best_earliest,
        "intervals": intervals,
    }
