#!/usr/bin/env bash
# 抢修降额预演 —— 水源/管段临时容量上限的现状 vs 降额情景对比 调用示例。
# 用法：bash examples/derating_demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; fi; }

echo "== 0. 健康检查 + 录入示例管网 =="
curl -sS "$BASE/health"; echo
curl -sS -X PUT "$BASE/api/network" \
  -H 'Content-Type: application/json' \
  --data @"$(dirname "$0")/sample_network.json" > /tmp/d_base.json
REV=$(python3 -c "import json;print(json.load(open('/tmp/d_base.json'))['revision'])")
echo "录入完成，基准修订号 revision=$REV"

echo
echo "== 1. 降额预演：泵站 S1 临时上限 4、管段 P3 临时上限 2（只读，不写状态） =="
curl -sS -X POST "$BASE/api/derating" \
  -H 'Content-Type: application/json' \
  -d "{\"revision\": $REV, \"items\": [
        {\"kind\": \"source\", \"id\": \"S1\", \"max_capacity\": 4},
        {\"kind\": \"pipe\",   \"id\": \"P3\", \"max_capacity\": 2}
      ]}" > /tmp/d_preview.json
python3 - <<'PY'
import json
d = json.load(open('/tmp/d_preview.json'))
print("依据修订号 revision =", d["revision"])
print("各用水点获水变化（现状 -> 降额，delta）：")
for c in d["demand_changes"]:
    print(f"  {c['demand_id']}: {c['baseline_supplied']} -> {c['derated_supplied']}"
          f"  (delta {c['delta']}, 降额后足额={c['derated_fulfilled']})")
print("降额设施容量使用（额定/临时上限/已用/剩余）：")
for u in d["derated_utilization"]:
    print(f"  {u['kind']} {u['id']}: {u['rated_capacity']} / {u['max_capacity']}"
          f" / {u['used']} / {u['remaining']}")
print("总量：现状", d["baseline"]["totals"], " 降额", d["derated"]["totals"])
PY

echo
echo "== 2. 预演不改变任何状态：修订号与额定容量保持原值 =="
curl -sS "$BASE/api/network" | python3 -c "
import sys, json
d = json.load(sys.stdin)
s1 = next(s for s in d['network']['sources'] if s['id'] == 'S1')
print('revision =', d['revision'], '| S1 额定容量 =', s1['capacity'])"

echo
echo "== 3. 无降额项：降额情景与现状一致 =="
curl -sS -X POST "$BASE/api/derating" \
  -H 'Content-Type: application/json' \
  -d "{\"revision\": $REV, \"items\": []}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
same = d['derated']['allocations'] == d['baseline']['allocations']
print('derated 与 baseline 一致：', same)"

echo
echo "== 4. 非法降额清单（未知设施 + 错误类别 + 重复 + 越额 → 400 逐条定位，不计算） =="
curl -sS -o /tmp/d_bad.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/derating" \
  -H 'Content-Type: application/json' \
  -d "{\"revision\": $REV, \"items\": [
        {\"kind\": \"source\", \"id\": \"GHOST\", \"max_capacity\": 1},
        {\"kind\": \"valve\",  \"id\": \"V1\",    \"max_capacity\": 1},
        {\"kind\": \"pipe\",   \"id\": \"P3\",    \"max_capacity\": 2},
        {\"kind\": \"pipe\",   \"id\": \"P3\",    \"max_capacity\": 3},
        {\"kind\": \"source\", \"id\": \"S1\",    \"max_capacity\": 99}
      ]}"
python3 - <<'PY'
import json
for d in json.load(open('/tmp/d_bad.json'))["error"]["details"]:
    print(f"  {d['location']}: [{d['code']}] {d['message']}")
PY

echo
echo "== 5. 过期修订号（409，不返回混合版本结果） =="
curl -sS -o /tmp/d_stale.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/derating" \
  -H 'Content-Type: application/json' \
  -d "{\"revision\": $((REV+99)), \"items\": []}"
python3 -c "import json;print(json.load(open('/tmp/d_stale.json'))['error']['message'])"

echo
echo "降额预演演示完成。"
