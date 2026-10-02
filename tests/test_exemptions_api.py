"""最小区域豁免预演 API 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_exemptions_api.py
脚本会清空 regions/region_meta 及各业务表后跑全套用例，结束时再次清空。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def at(h: int, m: int = 0, s: int = 0) -> str:
    return datetime(2030, 6, 1, h, m, s, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]
WIDE = ("2020-01-01T00:00:00Z", "2032-01-01T00:00:00Z")
TRAJ = [
    {"x": -50, "y": 50, "t": 0},   # 接触 [15s,45s]
    {"x": 150, "y": 50, "t": 60},
]
WINDOW = (at(10, 0), at(12, 0))

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE observation_events, observations, commitments, passes, "
                "region_change_audit, idempotency_keys, regions RESTART IDENTITY CASCADE"
            )
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def create_region(name, polygon=SQUARE, vf=WIDE[0], vt=WIDE[1]):
    r = client.post("/api/regions", json={
        "name": name, "polygon": polygon,
        "valid_from": vf, "valid_to": vt,
    })
    assert r.status_code == 201, r.text
    return r.json()["region"]["id"]


def preview(candidate_ids, expected_revision, points=None,
            window_start=WINDOW[0], window_end=WINDOW[1]):
    return client.post("/api/schedule/exemptions", json={
        "points": points or TRAJ,
        "window_start": window_start,
        "window_end": window_end,
        "candidate_region_ids": candidate_ids,
        "expected_revision": expected_revision,
    })


def table_count(table: str) -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            return cur.fetchone()[0]


reset_all()

# ====================================================================
# 1. 空集合快照：revision=0，原本安全，豁免为空
# ====================================================================
r = preview([], 0)
# 空 candidate 列表本身非法（min_length=1）→ 改用必须存在的编号走不通；
# 「原本安全」通过登记一个几何无关区域后给出空候选集效果验证（见第 4 组）
check("空候选列表 422 validation_error", r.status_code == 422
      and r.json()["error"]["code"] == "validation_error", r.text)

# ====================================================================
# 2. 编号未知 → 404 region_not_found
# ====================================================================
r = preview([999], 0)
check("未知区域编号 404 region_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "region_not_found"
      and r.json()["error"]["details"][0]["region_id"] == 999, r.text)

# ====================================================================
# 3. 编号重复 → 422 duplicate_region
# ====================================================================
rid1 = create_region("R1")
rid2 = create_region("R2")
r = preview([rid1, rid2, rid1], 2)
check("重复候选编号 422 duplicate_region，details 给出重复编号",
      r.status_code == 422 and r.json()["error"]["code"] == "duplicate_region"
      and r.json()["error"]["details"][0]["duplicate_region_ids"] == [rid1],
      r.text)

# ====================================================================
# 4. 原本安全 → 豁免集为空
# ====================================================================
# rid1、rid2 是宽窗 SQUARE 会封锁窗口；另起干净集合，只放一个几何无关区域
reset_all()
rid_high = create_region("R-HIGH", [[0, 700], [100, 700], [100, 800], [0, 800]])
TRAJ_HIGH = [
    {"x": -50, "y": 550, "t": 0},
    {"x": 150, "y": 550, "t": 60},
]
r = preview([rid_high], 1, points=TRAJ_HIGH)
body = r.json()
check("行程不穿任何区域：200 feasible、豁免为空、最早时刻=窗口起点、整窗安全",
      r.status_code == 200 and body["feasible"] is True
      and body["exempt_region_ids"] == []
      and body["earliest_departure"] == WINDOW[0]
      and len(body["intervals"]) == 1
      and body["intervals"][0]["start"] == WINDOW[0]
      and body["intervals"][0]["end"] == WINDOW[1]
      and body["revision"] == 1
      and body["candidate_region_ids"] == [rid_high],
      r.text)

# 再登记两个宽窗 SQUARE 区域（会封锁 TRAJ 的窗口），revision 推进到 3
rid1 = create_region("R1")
rid2 = create_region("R2")

# ====================================================================
# 5. 最小豁免：两个宽窗区域必须全部豁免
# ====================================================================
r = preview([rid1, rid2, rid_high], 3, points=TRAJ_HIGH)
body = r.json()
check("高轨行程不穿任何区域：候选含无关与封锁区域时豁免仍为空",
      body["feasible"] is True and body["exempt_region_ids"] == []
      and body["earliest_departure"] == WINDOW[0]
      and len(body["intervals"]) == 1
      and body["intervals"][0]["end"] == WINDOW[1], r.text)

r = preview([rid1, rid2], 3)
body = r.json()
check("两个宽窗区域：feasible、豁免两者、整窗安全",
      r.status_code == 200 and body["feasible"] is True
      and body["exempt_region_ids"] == sorted([rid1, rid2])
      and body["earliest_departure"] == WINDOW[0]
      and len(body["intervals"]) == 1
      and body["intervals"][0]["end"] == WINDOW[1],
      r.text)

# 只豁免其中一个：另一个仍封锁整窗 → 无可行方案
r = preview([rid1], 3)
body = r.json()
check("只给一个候选且另一宽窗区域不可豁免：feasible=false 的明确不可行结果",
      r.status_code == 200 and body["feasible"] is False
      and body["exempt_region_ids"] == []
      and body["earliest_departure"] is None
      and body["intervals"] == []
      and body["revision"] == 3,
      r.text)

# ====================================================================
# 6. 豁免数最少：一个半窗区域 + 一个全窗区域
# ====================================================================
reset_all()
# 半窗：生效 [10:00,11:00)、接触 [15,45] → 禁止 [09:59:15,10:59:45)
rid_half = create_region("半窗", vf=at(10, 0), vt=at(11, 0))
# 窗口 [10:00,12:00) 内：半窗区域禁止 [10:00,10:59:45)，[10:59:45,12:00) 安全
r = preview([rid_half], 1)
body = r.json()
check("单区域只挡窗口前段：空豁免即可，最早安全时刻为 10:59:45",
      body["feasible"] is True and body["exempt_region_ids"] == []
      and body["earliest_departure"] == at(10, 59, 45)
      and len(body["intervals"]) == 1
      and body["intervals"][0]["start"] == at(10, 59, 45)
      and body["intervals"][0]["end"] == WINDOW[1],
      r.text)

# 宽窗区域也登记：它封整窗，半窗是冗余的；豁免宽窗后半窗仍挡前段
rid_wide = create_region("宽窗")
r = preview([rid_wide, rid_half], 2)
body = r.json()
check("宽窗必豁免；半窗不挡后段：方案恰为 {宽窗}（最少豁免数）",
      body["feasible"] is True and body["exempt_region_ids"] == [rid_wide]
      and body["earliest_departure"] == at(10, 59, 45)
      and len(body["intervals"]) == 1
      and body["intervals"][0]["start"] == at(10, 59, 45)
      and body["intervals"][0]["end"] == WINDOW[1],
      r.text)

# ====================================================================
# 7. 不可行：不可豁免宽窗区域封锁，候选只给半窗
# ====================================================================
# rid_wide 不在候选中；rid_half 在候选中。豁免半窗后宽窗仍封整窗
r = preview([rid_half], 2)
body = r.json()
check("不可豁免宽窗封锁整窗：feasible=false（200 明确不可行）",
      r.status_code == 200 and body["feasible"] is False
      and body["intervals"] == [] and body["earliest_departure"] is None,
      r.text)

# ====================================================================
# 8. revision 过期 → 409 revision_conflict
# ====================================================================
r = preview([rid_half], 1)
check("expected_revision 过期：409 revision_conflict，details 带当前值",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict"
      and r.json()["error"]["details"][0]["current_revision"] == 2,
      r.text)

# ====================================================================
# 9. 非法轨迹 / 非法窗口 → 422
# ====================================================================
r = client.post("/api/schedule/exemptions", json={
    "points": [{"x": -50, "y": 50, "t": 10}, {"x": 150, "y": 50, "t": 60}],
    "window_start": WINDOW[0], "window_end": WINDOW[1],
    "candidate_region_ids": [rid_half], "expected_revision": 2,
})
check("首点相对秒数非 0：422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = client.post("/api/schedule/exemptions", json={
    "points": TRAJ,
    "window_start": WINDOW[1], "window_end": WINDOW[0],
    "candidate_region_ids": [rid_half], "expected_revision": 2,
})
check("窗口倒序：422 invalid_input",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_input", r.text)

r = client.post("/api/schedule/exemptions", json={
    "points": TRAJ,
    "window_start": "2030-06-01T10:00:00", "window_end": WINDOW[1],
    "candidate_region_ids": [rid_half], "expected_revision": 2,
})
check("窗口不带时区：422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)

r = client.post("/api/schedule/exemptions", json={
    "points": TRAJ,
    "window_start": WINDOW[0], "window_end": WINDOW[1],
    "candidate_region_ids": [0], "expected_revision": 2,
})
check("候选编号 < 1：422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)

r = client.post("/api/schedule/exemptions", json={
    "points": TRAJ,
    "window_start": WINDOW[0], "window_end": WINDOW[1],
    "candidate_region_ids": [rid_half],
})
check("缺 expected_revision：422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)

# ====================================================================
# 10. 只读保证：预演不改 revision、区域、审计、凭证、承诺、观察
# ====================================================================
rev_before = client.get("/api/revision").json()["revision"]
n_audit = table_count("region_change_audit")
n_regions = table_count("regions")
r1 = preview([rid_wide, rid_half], 2)
r2 = preview([rid_half], 2)
r3 = preview([9999], 2)
rev_after = client.get("/api/revision").json()["revision"]
check("预演（含可行/不可行/未知编号）不推进 revision",
      r1.status_code == 200 and rev_before == rev_after == 2,
      f"{rev_before} -> {rev_after}")
check("预演不新增区域与审计行",
      table_count("regions") == n_regions and table_count("region_change_audit") == n_audit)
check("未知编号的预演也是只读 404", r3.status_code == 404)

# 既有 /api/schedule 结果不受豁免预演影响：仍按完整集合计算（整窗被封 → 空）
r = client.post("/api/schedule", json={
    "points": TRAJ, "window_start": WINDOW[0], "window_end": WINDOW[1],
})
check("既有 /api/schedule 不受影响：完整集合下窗口无安全时刻",
      r.status_code == 200 and r.json()["intervals"] == []
      and r.json()["revision"] == 2, r.text)

# /api/verify 既有行为不变
r = client.post("/api/verify", json={
    "points": [
        {"x": -50, "y": 50, "t": at(10, 0)},
        {"x": 150, "y": 50, "t": at(10, 1)},
    ],
})
check("既有 /api/verify 不受影响：轨迹穿越宽窗区域",
      r.status_code == 200 and r.json()["safe"] is False, r.text)

# ====================================================================
# 11. 时区归一化：+08:00 与对应 UTC 等价
# ====================================================================
r = client.post("/api/schedule/exemptions", json={
    "points": TRAJ,
    "window_start": "2030-06-01T18:00:00+08:00",   # == 10:00Z
    "window_end": "2030-06-01T20:00:00+08:00",     # == 12:00Z
    "candidate_region_ids": [rid_wide, rid_half],
    "expected_revision": 2,
})
body = r.json()
check("带时区偏移窗口归一化为 UTC，结果与 UTC 写法一致",
      r.status_code == 200 and body["feasible"] is True
      and body["exempt_region_ids"] == [rid_wide]
      and body["window_start"] == WINDOW[0]
      and body["earliest_departure"] == at(10, 59, 45),
      r.text)

# ====================================================================
# 12. 快照一致性：预演进行中另一会话提交修订，不影响本次结果
# ====================================================================
# REPEATABLE READ 下整次计算用同一已提交快照；这里用「先算后登记」不易构造
# 真正并发，改为验证多次预演在 revision 未变时结果幂等
a = preview([rid_wide, rid_half], 2).json()
b = preview([rid_wide, rid_half], 2).json()
check("同 revision 下预演结果幂等",
      a["exempt_region_ids"] == b["exempt_region_ids"]
      and a["intervals"] == b["intervals"])

reset_all()

print()
if failures:
    print(f"{len(failures)} 个用例失败：{failures}")
    sys.exit(1)
print("全部最小区域豁免 API 集成用例通过")
