"""降额预演端到端测试：同一快照现状 vs 降额情景、逐条定位拒绝、只读不写。"""
from __future__ import annotations

import os
import tempfile

import pytest

# 独立运行本文件时（不经过 test_api）确保有数据库文件
os.environ.setdefault(
    "WATER_DB_PATH",
    tempfile.NamedTemporaryFile(suffix=".db", delete=False).name,
)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402

init_db()


# 与 test_api 同一演示管网：
#   S1(cap10) --P1(cap10,V1)--> D1(需求10, 优先级9)
#   S2(cap6)  --P2(cap6)------> J1 --P3(cap4,V3)--> D2(需求8, 优先级1)
#                                 \--P6(cap6)----> D3(需求6, 优先级1)
#   P5(cap5,V5, 关闭): S1 -> S2（互锁水源连接管，常闭）
# 现状分派：D1=10（S1 独立供），D2=4（P3 限），D3=2（S2 余量），合计 16。
def network():
    return {
        "sources": [
            {"id": "S1", "capacity": 10},
            {"id": "S2", "capacity": 6},
        ],
        "demands": [
            {"id": "D1", "demand": 10, "priority": 9},
            {"id": "D2", "demand": 8, "priority": 1},
            {"id": "D3", "demand": 6, "priority": 1},
        ],
        "junctions": [{"id": "J1"}],
        "pipes": [
            {"id": "P1", "from_node": "S1", "to_node": "D1", "capacity": 10},
            {"id": "P2", "from_node": "S2", "to_node": "J1", "capacity": 6},
            {"id": "P3", "from_node": "J1", "to_node": "D2", "capacity": 4},
            {"id": "P6", "from_node": "J1", "to_node": "D3", "capacity": 6},
            {"id": "P5", "from_node": "S1", "to_node": "S2", "capacity": 5},
        ],
        "valves": [
            {"id": "V1", "pipe_id": "P1", "is_open": True},
            {"id": "V3", "pipe_id": "P3", "is_open": True},
            {"id": "V5", "pipe_id": "P5", "is_open": False},
        ],
        "forbidden_pairs": [{"source_a": "S1", "source_b": "S2"}],
    }


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def load(client):
    """录入演示管网，返回当前基准修订号。"""
    r = client.put("/api/network", json=network())
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def supplied_map(result):
    return {a["demand_id"]: a["supplied"] for a in result["allocations"]}


def test_derating_source_reduces_supply(client):
    rev = load(client)
    r = client.post("/api/derating", json={
        "revision": rev,
        "items": [{"kind": "source", "id": "S1", "max_capacity": 4}],
    })
    assert r.status_code == 200, r.text
    data = r.json()

    # 所依据的修订号即所见修订号；两种情景同快照
    assert data["revision"] == rev
    assert supplied_map(data["baseline"]) == {"D1": 10.0, "D2": 4.0, "D3": 2.0}
    derated = supplied_map(data["derated"])
    assert derated["D1"] == pytest.approx(4)   # S1 降额 10 -> 4，D1 等量减少
    assert derated["D2"] == pytest.approx(4)   # S2 支路不受影响
    assert derated["D3"] == pytest.approx(2)

    # 各用水点获水变化
    changes = {c["demand_id"]: c for c in data["demand_changes"]}
    assert changes["D1"]["baseline_supplied"] == pytest.approx(10)
    assert changes["D1"]["derated_supplied"] == pytest.approx(4)
    assert changes["D1"]["delta"] == pytest.approx(-6)
    assert changes["D1"]["baseline_fulfilled"] is True
    assert changes["D1"]["derated_fulfilled"] is False
    assert changes["D2"]["delta"] == pytest.approx(0)
    assert changes["D3"]["delta"] == pytest.approx(0)

    # 降额设施容量使用：额定 10、临时上限 4、降额情景下打满
    util = data["derated_utilization"]
    assert len(util) == 1
    u = util[0]
    assert u["kind"] == "source" and u["id"] == "S1"
    assert u["rated_capacity"] == pytest.approx(10)
    assert u["max_capacity"] == pytest.approx(4)
    assert u["used"] == pytest.approx(4)
    assert u["remaining"] == pytest.approx(0)

    # 降额情景的 source_utilization 以临时上限为口径
    src = {s["source_id"]: s for s in data["derated"]["source_utilization"]}
    assert src["S1"]["capacity"] == pytest.approx(4)
    base_src = {s["source_id"]: s for s in data["baseline"]["source_utilization"]}
    assert base_src["S1"]["capacity"] == pytest.approx(10)


def test_derating_pipe_reduces_supply_and_reallocates(client):
    rev = load(client)
    r = client.post("/api/derating", json={
        "revision": rev,
        "items": [{"kind": "pipe", "id": "P3", "max_capacity": 2}],
    })
    assert r.status_code == 200, r.text
    data = r.json()

    # P3 降额 4 -> 2：D2 少得 2，S2 余量让给 D3（共享容量规则不变）
    derated = supplied_map(data["derated"])
    assert derated["D2"] == pytest.approx(2)
    assert derated["D3"] == pytest.approx(4)
    changes = {c["demand_id"]: c for c in data["demand_changes"]}
    assert changes["D2"]["delta"] == pytest.approx(-2)
    assert changes["D3"]["delta"] == pytest.approx(2)
    assert changes["D1"]["delta"] == pytest.approx(0)

    u = data["derated_utilization"][0]
    assert u["kind"] == "pipe" and u["id"] == "P3"
    assert u["rated_capacity"] == pytest.approx(4)
    assert u["max_capacity"] == pytest.approx(2)
    assert u["used"] == pytest.approx(2)
    assert u["remaining"] == pytest.approx(0)
    assert u["conducting"] is True


def test_derating_multiple_items(client):
    rev = load(client)
    r = client.post("/api/derating", json={
        "revision": rev,
        "items": [
            {"kind": "source", "id": "S2", "max_capacity": 3},
            {"kind": "pipe", "id": "P6", "max_capacity": 1},
        ],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    derated = supplied_map(data["derated"])
    # S2 降额 6 -> 3：D2 先得 3（P3 容量 4 足够），D3 受 S2 余量 0 与 P6=1 双重限制
    assert derated["D1"] == pytest.approx(10)
    assert derated["D2"] == pytest.approx(3)
    assert derated["D3"] == pytest.approx(0)
    util = {(u["kind"], u["id"]): u for u in data["derated_utilization"]}
    assert util[("source", "S2")]["used"] == pytest.approx(3)
    assert util[("pipe", "P6")]["used"] == pytest.approx(0)


def test_derating_empty_items_matches_baseline(client):
    rev = load(client)
    r = client.post("/api/derating", json={"revision": rev, "items": []})
    assert r.status_code == 200, r.text
    data = r.json()
    # 没有降额项：降额情景与现状一致
    assert data["derated"]["allocations"] == data["baseline"]["allocations"]
    assert data["derated"]["totals"] == data["baseline"]["totals"]
    assert data["items"] == [] and data["derated_utilization"] == []
    assert all(c["delta"] == 0 for c in data["demand_changes"])


def test_derating_unknown_facility_and_kind(client):
    rev = load(client)
    r = client.post("/api/derating", json={
        "revision": rev,
        "items": [
            {"kind": "source", "id": "GHOST", "max_capacity": 1},
            {"kind": "pipe", "id": "P404", "max_capacity": 1},
            {"kind": "valve", "id": "V1", "max_capacity": 1},
            {"kind": "source", "id": "D1", "max_capacity": 1},  # 用水点不是水源
        ],
    })
    assert r.status_code == 400
    body = r.json()["error"]
    assert body["code"] == "invalid_derating"
    locs = [(d["location"], d["code"]) for d in body["details"]]
    assert ("items[0].id", "unknown_source") in locs
    assert ("items[1].id", "unknown_pipe") in locs
    assert ("items[2].kind", "unknown_kind") in locs
    assert ("items[3].id", "unknown_source") in locs


def test_derating_duplicate_item(client):
    rev = load(client)
    r = client.post("/api/derating", json={
        "revision": rev,
        "items": [
            {"kind": "source", "id": "S1", "max_capacity": 5},
            {"kind": "pipe", "id": "P3", "max_capacity": 2},
            {"kind": "source", "id": "S1", "max_capacity": 6},
        ],
    })
    assert r.status_code == 400
    details = r.json()["error"]["details"]
    assert len(details) == 1
    assert details[0]["location"] == "items[2].id"
    assert details[0]["code"] == "duplicate_item"


def test_derating_capacity_exceeded(client):
    rev = load(client)
    r = client.post("/api/derating", json={
        "revision": rev,
        "items": [{"kind": "pipe", "id": "P3", "max_capacity": 4.5}],
    })
    assert r.status_code == 400
    d = r.json()["error"]["details"][0]
    assert d["location"] == "items[0].max_capacity"
    assert d["code"] == "capacity_exceeded"
    assert d["rated_capacity"] == pytest.approx(4)

    # 等于额定容量是合法边界（不视为降额，但允许）
    r = client.post("/api/derating", json={
        "revision": rev,
        "items": [{"kind": "pipe", "id": "P3", "max_capacity": 4}],
    })
    assert r.status_code == 200, r.text


def test_derating_negative_and_nan_rejected_422(client):
    rev = load(client)
    r = client.post("/api/derating", json={
        "revision": rev,
        "items": [{"kind": "source", "id": "S1", "max_capacity": -1}],
    })
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["location"].endswith("max_capacity")

    # httpx 的 JSON 编码器拒绝 NaN，改为发送原始请求体（服务端按 JSON 解析后走有限性校验）
    r = client.post(
        "/api/derating",
        content=(
            '{"revision": %d, "items": [{"kind": "source", "id": "S1",'
            ' "max_capacity": NaN}]}'
        ) % rev,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 422


def test_derating_stale_revision_rejected(client):
    rev = load(client)
    # 推进修订号：关闭再打开 V3
    r = client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V3", "action": "close"}]})
    rev2 = r.json()["revision"]
    assert rev2 == rev + 1

    r = client.post("/api/derating", json={
        "revision": rev,
        "items": [{"kind": "source", "id": "S1", "max_capacity": 5}],
    })
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "revision_conflict"
    d = body["details"][0]
    assert d["seen_revision"] == rev
    assert d["current_revision"] == rev2

    # 恢复 V3
    client.post("/api/valves/change",
                json={"changes": [{"valve_id": "V3", "action": "open"}]})


def test_derating_does_not_mutate_state(client):
    rev = load(client)
    # 建一个复水计划，验证降额预演不会使其失效
    r = client.post("/api/restore/plans", json={"steps": [
        {"valve_id": "V3", "action": "close"},
    ]})
    assert r.status_code == 200, r.text
    plan_id = r.json()["plan_id"]

    r = client.post("/api/derating", json={
        "revision": rev,
        "items": [
            {"kind": "source", "id": "S1", "max_capacity": 2},
            {"kind": "pipe", "id": "P2", "max_capacity": 1},
        ],
    })
    assert r.status_code == 200, r.text

    # 修订号、管网容量、阀位均未改变；计划仍 active
    net = client.get("/api/network").json()
    assert net["revision"] == rev
    src = {s["id"]: s for s in net["network"]["sources"]}
    assert src["S1"]["capacity"] == pytest.approx(10)
    pipes = {p["id"]: p for p in net["network"]["pipes"]}
    assert pipes["P2"]["capacity"] == pytest.approx(6)
    detail = client.get(f"/api/restore/plans/{plan_id}").json()
    assert detail["status"] == "active"


def test_derating_network_not_initialized(client):
    client.delete("/api/network")
    r = client.post("/api/derating", json={"revision": 0, "items": []})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "network_not_initialized"
    load(client)  # 恢复，避免影响其它用例
