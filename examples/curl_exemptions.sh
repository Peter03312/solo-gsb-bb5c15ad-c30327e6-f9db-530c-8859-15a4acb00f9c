#!/usr/bin/env bash
# 最小区域豁免预演调用示例（只读；不改变区域、凭证、承诺、观察、审计、revision）。
#
# 演示固定行程（相对秒数）：(-50,50) @0s → (150,50) @60s，对正方形
# [0,100]×[0,100] 的几何接触为相对时刻 15s~45s；候选出发窗口
# [2030-06-01T10:00Z, 12:00Z)。
#
#   1. 登记两个长期生效、各自都会封锁整窗的限行区域 A/B（id 递增）
#   2. 只把 A 列为可豁免：B 仍在 → feasible=false（明确不可行结果，HTTP 200）
#   3. 把 A、B 都列为可豁免：feasible=true，豁免 [A,B]，整窗安全
#   4. 原本安全：登记一个与行程不相交的区域 C，仅候选 C → 豁免集为空
#   5. 编号未知 404、编号重复 422、revision 过期 409、轨迹/窗口非法 422
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_exemptions.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }

WS="2030-06-01T10:00:00Z"
WE="2030-06-01T12:00:00Z"
# 相对秒数轨迹（首点 t=0）：直穿 [0,100]×[0,100]，接触 15s~45s
TRAJ='[{"x":-50,"y":50,"t":0},{"x":150,"y":50,"t":60}]'
# 从所有已登记区域上方经过的行程：对 A/B（y∈[0,100]）与 C（y∈[700,800]）
# 都不接触
TRAJ_HIGH='[{"x":-50,"y":400,"t":0},{"x":150,"y":400,"t":60}]'

create_region() {
  curl -fsS -X POST "$BASE_URL/api/regions" \
    -H 'Content-Type: application/json' \
    -d "$1" | J "['region']['id']"
}

echo "== 1. 登记两个长期生效的限行区域（各自禁止区间覆盖整窗） =="
ID_A=$(create_region '{
      "name": "A区-预演示例",
      "polygon": [[0,0],[100,0],[100,100],[0,100]],
      "valid_from": "2020-01-01T00:00:00Z",
      "valid_to":   "2032-01-01T00:00:00Z"
    }')
ID_B=$(create_region '{
      "name": "B区-预演示例",
      "polygon": [[0,0],[100,0],[100,100],[0,100]],
      "valid_from": "2020-01-01T00:00:00Z",
      "valid_to":   "2032-01-01T00:00:00Z"
    }')
ID_C=$(create_region '{
      "name": "C区-几何无关",
      "polygon": [[700,700],[800,700],[800,800],[700,800]],
      "valid_from": "2020-01-01T00:00:00Z",
      "valid_to":   "2032-01-01T00:00:00Z"
    }')
echo "区域编号 A=$ID_A B=$ID_B C=$ID_C"
REV=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
echo "当前 revision=$REV（预演全程不会推进它）"

preview() {  # $1 = candidate_region_ids JSON 数组，$2 = expected_revision，$3 = 轨迹（可选）
  local traj="${3:-$TRAJ}"
  curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE_URL/api/schedule/exemptions" \
    -H 'Content-Type: application/json' \
    -d "{
          \"points\": $traj,
          \"window_start\": \"$WS\",
          \"window_end\": \"$WE\",
          \"candidate_region_ids\": $1,
          \"expected_revision\": $2
        }"
}

echo
echo "== 2. 只豁免 A：B 不可豁免且封锁整窗 → 200 feasible=false（无可行方案） =="
preview "[$ID_A]" "$REV"

echo
echo "== 3. A、B 均可豁免 → feasible=true，最少豁免 [A,B]，最早时刻=窗口起点，整窗安全 =="
preview "[$ID_A,$ID_B]" "$REV"

echo
echo "== 4. 从所有区域上方经过的行程：原本安全，候选 C → 豁免集为空 =="
preview "[$ID_C]" "$REV" "$TRAJ_HIGH"

echo
echo "== 5a. 候选编号未知 → 404 region_not_found =="
preview "[999999]" "$REV"

echo
echo "== 5b. 候选编号重复 → 422 duplicate_region =="
preview "[$ID_A,$ID_A]" "$REV"

echo
echo "== 5c. expected_revision 过期 → 409 revision_conflict（details 带当前值） =="
preview "[$ID_A]" "$((REV - 1))"

echo
echo "== 5d. 相对轨迹非法（首点 t≠0）→ 422 invalid_trajectory =="
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE_URL/api/schedule/exemptions" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": [{\"x\":-50,\"y\":50,\"t\":1},{\"x\":150,\"y\":50,\"t\":60}],
        \"window_start\": \"$WS\", \"window_end\": \"$WE\",
        \"candidate_region_ids\": [$ID_A], \"expected_revision\": $REV
      }"

echo
echo "== 5e. 窗口倒序 → 422 invalid_input =="
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE_URL/api/schedule/exemptions" \
  -H 'Content-Type: application/json' \
  -d "{
        \"points\": $TRAJ,
        \"window_start\": \"$WE\", \"window_end\": \"$WS\",
        \"candidate_region_ids\": [$ID_A], \"expected_revision\": $REV
      }"

echo
echo "== 只读核对：revision 仍为 $REV，区域数仍为 3，审计历史不含预演记录 =="
curl -fsS "$BASE_URL/api/revision"
echo
curl -fsS "$BASE_URL/api/regions/changes?limit=3" | python3 -m json.tool
