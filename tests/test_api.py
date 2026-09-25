"""API 端到端测试：每个用例使用独立的临时 SQLite 数据库。"""
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app


def plan(client, a, b, sample, numerator, denominator, cap):
    return client.post("/pool-plans", json={
        "ingredient_a_batch_id": a,
        "ingredient_b_batch_id": b,
        "sample_id": sample,
        "numerator": numerator,
        "denominator": denominator,
        "max_total_portions": cap,
    })


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_sample(client, name):
    r = client.post("/samples", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_pool(client, name, recipe):
    r = client.post("/pools", json={"name": name, "recipe": recipe})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def fractions_of(client, batch_id):
    r = client.get(f"/batches/{batch_id}/composition")
    assert r.status_code == 200, r.text
    return {
        c["sample_id"]: (c["numerator"], c["denominator"])
        for c in r.json()["components"]
    }


def test_single_sample_composition(client):
    s1 = make_sample(client, "S1")
    assert fractions_of(client, s1) == {s1: (1, 1)}


def test_pool_composition_is_reduced(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 2:2 投入，结果应约分为 1/2 + 1/2
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 2},
    ])
    assert fractions_of(client, p1) == {s1: (1, 2), s2: (1, 2)}


def test_multilevel_same_source_merged(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    # P2 = 1×P1 + 1×S1：S1 经两条路径到达，须合并为 1/2·1/2 + 1/2 = 3/4
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    assert fractions_of(client, p2) == {s1: (3, 4), s2: (1, 4)}


def test_deep_chain(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    # P3 = 3×P2 + 1×S2 → S1 = 3/4·3/4 = 9/16，S2 = 3/4·1/4 + 1/4 = 7/16
    p3 = make_pool(client, "P3", [
        {"batch_id": p2, "portions": 3},
        {"batch_id": s2, "portions": 1},
    ])
    assert fractions_of(client, p3) == {s1: (9, 16), s2: (7, 16)}


def test_duplicate_recipe_entries_merged(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 1},
    ]})
    assert r.status_code == 201
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 3},
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
    ]
    assert fractions_of(client, body["id"]) == {s1: (3, 4), s2: (1, 4)}


def test_empty_recipe_rejected(client):
    r = client.post("/pools", json={"name": "P1", "recipe": []})
    assert r.status_code == 422


def test_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": 999, "portions": 1},
    ]})
    assert r.status_code == 400
    assert "999" in r.json()["detail"]


@pytest.mark.parametrize("portions", [0, -3, 1.5, 2.0, "2", True])
def test_non_positive_or_non_integer_portions_rejected(client, portions):
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1, "portions": portions},
    ]})
    assert r.status_code == 422


def test_query_unknown_batch_returns_404(client):
    assert client.get("/batches/4242/composition").status_code == 404
    assert client.get("/batches/4242").status_code == 404


def test_duplicate_name_rejected(client):
    make_sample(client, "S1")
    r = client.post("/samples", json={"name": "S1"})
    assert r.status_code == 409


def test_pool_can_only_reference_existing_batches(client):
    # 引用尚未创建的批次（未来的 id）同样被拒绝
    s1 = make_sample(client, "S1")
    r = client.post("/pools", json={"name": "P1", "recipe": [
        {"batch_id": s1 + 100, "portions": 1},
    ]})
    assert r.status_code == 400


def preview(client, batch_id, recipe):
    return client.post(f"/batches/{batch_id}/recipe-preview", json={"recipe": recipe})


def preview_components(batch):
    """{(sample_id): ((原分子, 原分母), (预演分子, 预演分母), (差值分子, 差值分母))}"""
    return {
        c["sample_id"]: (
            (c["original"]["numerator"], c["original"]["denominator"]),
            (c["preview"]["numerator"], c["preview"]["denominator"]),
            (c["delta"]["numerator"], c["delta"]["denominator"]),
        )
        for c in batch["components"]
    }


def test_recipe_preview_basic(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    # 把 P1 的配方预演替换为 3×S1 + 1×S2
    r = preview(client, p1, [
        {"batch_id": s1, "portions": 3},
        {"batch_id": s2, "portions": 1},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] == p1
    assert body["batch_name"] == "P1"
    assert body["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 3},
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
    ]
    # 受影响批次 = 目标自身 + 下游 P2，按 id 升序、各出现一次
    assert [b["batch_id"] for b in body["affected_batches"]] == [p1, p2]
    b1, b2 = body["affected_batches"]
    assert preview_components(b1) == {
        s1: ((1, 2), (3, 4), (1, 4)),
        s2: ((1, 2), (1, 4), (-1, 4)),
    }
    # P2' = 1/2·P1' + 1/2·S1 → S1 = 7/8，S2 = 1/8
    assert preview_components(b2) == {
        s1: ((3, 4), (7, 8), (1, 8)),
        s2: ((1, 4), (1, 8), (-1, 8)),
    }


def test_recipe_preview_merges_duplicate_ingredients(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    r = preview(client, p1, [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 1},
    ])
    assert r.status_code == 200, r.text
    assert r.json()["recipe"] == [
        {"batch_id": s1, "batch_name": "S1", "portions": 3},
        {"batch_id": s2, "batch_name": "S2", "portions": 1},
    ]


def test_recipe_preview_multi_path_merged_and_deduped(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    # P3 经 P1、P2 两条路径汇合到 P1；与 P1 无关的 P4 不应受影响
    p3 = make_pool(client, "P3", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": p2, "portions": 1},
    ])
    s3 = make_sample(client, "S3")
    p4 = make_pool(client, "P4", [{"batch_id": s3, "portions": 1}])

    # 把 P1 预演替换为纯 S2
    r = preview(client, p1, [{"batch_id": s2, "portions": 1}])
    assert r.status_code == 200, r.text
    affected = {b["batch_id"]: b for b in r.json()["affected_batches"]}
    assert set(affected) == {p1, p2, p3}  # 多路径汇合，同一批次只返回一次

    # P1' = S2；P2' = 1/2·P1' + 1/2·S1；P3' = 1/2·P1' + 1/2·P2'
    assert preview_components(affected[p1]) == {
        s1: ((1, 2), (0, 1), (-1, 2)),
        s2: ((1, 2), (1, 1), (1, 2)),
    }
    assert preview_components(affected[p2]) == {
        s1: ((3, 4), (1, 2), (-1, 4)),
        s2: ((1, 4), (1, 2), (1, 4)),
    }
    assert preview_components(affected[p3]) == {
        s1: ((5, 8), (1, 4), (-3, 8)),
        s2: ((3, 8), (3, 4), (3, 8)),
    }


def test_recipe_preview_does_not_modify_database(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1},
        {"batch_id": s1, "portions": 1},
    ])
    before_detail = client.get(f"/batches/{p1}").json()
    before_list = client.get("/batches").json()
    before_comp_p1 = fractions_of(client, p1)
    before_comp_p2 = fractions_of(client, p2)

    r = preview(client, p1, [{"batch_id": s2, "portions": 5}])
    assert r.status_code == 200, r.text

    # 预演后详情、列表与占比查询结果保持原样
    assert client.get(f"/batches/{p1}").json() == before_detail
    assert client.get("/batches").json() == before_list
    assert fractions_of(client, p1) == before_comp_p1
    assert fractions_of(client, p2) == before_comp_p2


def test_recipe_preview_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    r = preview(client, p1, [
        {"batch_id": s1, "portions": 1},
        {"batch_id": 999, "portions": 1},
    ])
    assert r.status_code == 400
    assert "999" in r.json()["detail"]


def test_recipe_preview_ingredient_must_predate_target(client):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    s2 = make_sample(client, "S2")  # 比 P1 更晚创建，不能作为 P1 的预演原料
    r = preview(client, p1, [{"batch_id": s2, "portions": 1}])
    assert r.status_code == 400
    assert str(s2) in r.json()["detail"]
    # 自引用同样会形成环，被拒绝
    r = preview(client, p1, [{"batch_id": p1, "portions": 1}])
    assert r.status_code == 400


def test_recipe_preview_sample_target_rejected(client):
    s1 = make_sample(client, "S1")
    r = preview(client, s1, [{"batch_id": s1, "portions": 1}])
    assert r.status_code == 400


def test_recipe_preview_unknown_target_returns_404(client):
    assert preview(client, 4242, [{"batch_id": 1, "portions": 1}]).status_code == 404


def test_recipe_preview_empty_recipe_rejected(client):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    assert preview(client, p1, []).status_code == 422


@pytest.mark.parametrize("portions", [0, -3, 1.5, 2.0, "2", True])
def test_recipe_preview_non_positive_or_non_integer_portions_rejected(client, portions):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    assert preview(client, p1, [{"batch_id": s1, "portions": portions}]).status_code == 422


# --------------------------------------------------------------------------- #
# POST /pool-plans
# --------------------------------------------------------------------------- #


def test_pool_plan_pure_samples_half_is_one_to_one(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = plan(client, s1, s2, s1, 1, 2, 10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sample_id"] == s1
    assert body["target"] == {"numerator": 1, "denominator": 2}
    assert body["total_portions"] == 2
    assert body["recipe"] == [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
    ]
    assert body["components"] == [
        {"sample_id": s1, "sample_name": "S1", "numerator": 1, "denominator": 2},
        {"sample_id": s2, "sample_name": "S2", "numerator": 1, "denominator": 2},
    ]


def test_pool_plan_returns_minimal_integer_solution(client):
    # 纯原料 a=1、b=0，目标 2/3 → x:y = 2:1（不是 4:2 等更大解）
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = plan(client, s1, s2, s1, 2, 3, 100)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": s1, "portions": 2},
        {"batch_id": s2, "portions": 1},
    ]
    assert body["total_portions"] == 3
    assert body["target"] == {"numerator": 2, "denominator": 3}


def test_pool_plan_target_fraction_is_reduced(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 4/6 = 2/3，target 返回约分形式
    r = plan(client, s1, s2, s1, 4, 6, 100)
    assert r.status_code == 200, r.text
    assert r.json()["target"] == {"numerator": 2, "denominator": 3}


def test_pool_plan_multilevel_ingredients(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    # P1 = S1/S2 → S1=1/2；P2 = P1/S3 → S1=1/4
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s3, "portions": 1}])
    # 目标 S1=1/3 落在 (1/4, 1/2) 之间 → x:y = (1/3-1/4):(1/2-1/3) = 1:2
    r = plan(client, p1, p2, s1, 1, 3, 100)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": p1, "portions": 1},
        {"batch_id": p2, "portions": 2},
    ]
    assert body["total_portions"] == 3
    assert {(c["sample_id"], c["numerator"], c["denominator"]) for c in body["components"]} == {
        (s1, 1, 3), (s2, 1, 3), (s3, 1, 3),
    }


def test_pool_plan_recipe_order_matches_request(client):
    # 交换两个原料的位置，份数也要对应交换
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r1 = plan(client, s1, s2, s1, 2, 3, 100).json()
    r2 = plan(client, s2, s1, s1, 2, 3, 100).json()
    assert r1["recipe"] == [
        {"batch_id": s1, "portions": 2}, {"batch_id": s2, "portions": 1}]
    assert r2["recipe"] == [
        {"batch_id": s2, "portions": 1}, {"batch_id": s1, "portions": 2}]
    assert r1["components"] == r2["components"]


def test_pool_plan_equal_ingredient_ratios_uses_one_to_one(client):
    # Q = S1/S2、R = S2/S3：S2 在两原料中都是 1/2，目标 1/2 时取 1:1
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    q = make_pool(client, "Q", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    rpool = make_pool(client, "R", [
        {"batch_id": s2, "portions": 1}, {"batch_id": s3, "portions": 1}])
    r = plan(client, q, rpool, s2, 1, 2, 100)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["recipe"] == [
        {"batch_id": q, "portions": 1}, {"batch_id": rpool, "portions": 1}]
    assert body["total_portions"] == 2
    # 目标样本确实达到 1/2
    s2_comp = next(c for c in body["components"] if c["sample_id"] == s2)
    assert (s2_comp["numerator"], s2_comp["denominator"]) == (1, 2)


def test_pool_plan_recipe_is_submittable_to_pools(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    planned = plan(client, p1, s1, s1, 3, 4, 100).json()
    # 配方可直接提交 /pools（无多余字段），创建后实际占比与规划一致
    r = client.post("/pools", json={"name": "PNEW", "recipe": planned["recipe"]})
    assert r.status_code == 201, r.text
    new_id = r.json()["id"]
    assert fractions_of(client, new_id) == {
        c["sample_id"]: (c["numerator"], c["denominator"])
        for c in planned["components"]
    }


def test_pool_plan_does_not_create_batches(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    before = client.get("/batches").json()
    assert plan(client, s1, s2, s1, 1, 2, 10).status_code == 200
    # 目标不可达（9/10 最小解总份数 10，上限 9）的规划也不应写库
    assert plan(client, s1, s2, s1, 9, 10, 9).status_code == 422
    assert client.get("/batches").json() == before


def test_pool_plan_target_outside_range_returns_422(client):
    # a=b=1/2 而目标 3/4，正份数加权平均不可能达到
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    q = make_pool(client, "Q", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    rpool = make_pool(client, "R", [
        {"batch_id": s2, "portions": 1}, {"batch_id": s3, "portions": 1}])
    r = plan(client, q, rpool, s2, 3, 4, 100)
    assert r.status_code == 422
    assert "无法用正整数份数达到" in r.json()["detail"]


def test_pool_plan_target_equal_single_endpoint_returns_422(client):
    # a=1/2、b=0，目标 1/2 只等于一端：另一原料份数必为 0
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = plan(client, s1, s2, s1, 1, 2, 100)
    assert r.status_code == 200  # 对照组：目标严格在区间内
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    r = plan(client, p1, s2, s1, 1, 2, 100)
    assert r.status_code == 422
    assert "无法用正整数份数达到" in r.json()["detail"]


def test_pool_plan_zero_target_against_pure_sample_returns_422(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 目标 0，而原料 a 本身就是该样本（占比 1/1）→ 必须 0 份 a
    r = plan(client, s1, s2, s1, 0, 1, 100)
    assert r.status_code == 422
    assert "无法用正整数份数达到" in r.json()["detail"]


def test_pool_plan_minimal_solution_over_cap_returns_422(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 最小解总份数 3，上限 2
    r = plan(client, s1, s2, s1, 2, 3, 2)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "3" in detail and "2" in detail and "上限" in detail
    # 恰好等于上限时接受
    assert plan(client, s1, s2, s1, 2, 3, 3).status_code == 200


@pytest.mark.parametrize("numerator,denominator", [
    (1, 0),     # 分母为 0
    (-1, 2),    # 负分子
    (3, 2),     # 分子 > 分母
    (1.5, 2),   # 小数
    ("1", 2),   # 字符串
    (True, 2),  # 布尔
])
def test_pool_plan_invalid_target_rejected(client, numerator, denominator):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = plan(client, s1, s2, s1, numerator, denominator, 10)
    assert r.status_code == 422


@pytest.mark.parametrize("cap", [0, -1, 1.5, 2.0, "3", True])
def test_pool_plan_invalid_cap_rejected(client, cap):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    r = client.post("/pool-plans", json={
        "ingredient_a_batch_id": s1,
        "ingredient_b_batch_id": s2,
        "sample_id": s1,
        "numerator": 1,
        "denominator": 2,
        "max_total_portions": cap,
    })
    assert r.status_code == 422


def test_pool_plan_same_ingredient_rejected(client):
    s1 = make_sample(client, "S1")
    r = plan(client, s1, s1, s1, 1, 2, 10)
    assert r.status_code == 422
    assert "不同" in r.json()["detail"][0]["msg"]


def test_pool_plan_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # 未知原料
    r = plan(client, 999, s2, s1, 1, 2, 10)
    assert r.status_code == 400
    assert "999" in r.json()["detail"]
    # 未知目标样本
    r = plan(client, s1, s2, 998, 1, 2, 10)
    assert r.status_code == 400
    assert "998" in r.json()["detail"]


def test_pool_plan_sample_id_must_be_original_sample(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    r = plan(client, s1, p1, p1, 1, 2, 10)
    assert r.status_code == 400
    assert "原始样本" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# POST/GET /exclusion-rules + /pools 禁配
# --------------------------------------------------------------------------- #


def add_rule(client, a, b):
    return client.post("/exclusion-rules", json={"sample_a_id": a, "sample_b_id": b})


def test_create_exclusion_rule_normalizes_order_and_lists(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    # 倒序登记，响应仍按 id 升序归一
    r = add_rule(client, s2, s1)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["low_sample_id"] == s1
    assert body["high_sample_id"] == s2
    assert "created_at" in body

    # 列表按 (low, high) 字典序
    r = add_rule(client, s3, s1)
    assert r.status_code == 201
    rules = client.get("/exclusion-rules").json()
    assert [(x["low_sample_id"], x["high_sample_id"]) for x in rules] == [
        (s1, s2), (s1, s3),
    ]


def test_duplicate_unordered_pair_is_idempotent(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    first = add_rule(client, s1, s2)
    assert first.status_code == 201
    created_at = first.json()["created_at"]

    # 同一无序对、交换顺序重复登记：不生成第二条，幂等返回 200 且 created_at 不变
    r = add_rule(client, s2, s1)
    assert r.status_code == 200, r.text
    assert r.json() == first.json()
    assert r.json()["created_at"] == created_at
    rules = client.get("/exclusion-rules").json()
    assert [(x["low_sample_id"], x["high_sample_id"]) for x in rules] == [(s1, s2)]


def test_exclusion_rule_rejects_same_id_unknown_and_pool(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])

    # 两个 id 相同 → 400
    r = add_rule(client, s1, s1)
    assert r.status_code == 400
    assert "不同" in r.json()["detail"]
    # 未知 id → 400
    r = add_rule(client, s1, 999)
    assert r.status_code == 400
    assert "999" in r.json()["detail"]
    # 混样批次不是原始样本 → 400
    r = add_rule(client, s1, p1)
    assert r.status_code == 400
    assert str(p1) in r.json()["detail"]
    assert client.get("/exclusion-rules").json() == []


def test_pool_hitting_direct_pair_returns_409_and_creates_nothing(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    assert add_rule(client, s1, s2).status_code == 201

    before = client.get("/batches").json()
    r = client.post("/pools", json={"name": "PBAD", "recipe": [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}]})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["pair"] == [
        {"sample_id": s1, "sample_name": "S1"},
        {"sample_id": s2, "sample_name": "S2"},
    ]
    # 不创建批次（名称也未占用）或配方
    assert client.get("/batches").json() == before
    assert client.post("/pools", json={"name": "PBAD", "recipe": [
        {"batch_id": s1, "portions": 1}]}).status_code == 201


def test_pool_conflict_through_nested_and_merged_paths(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    # 无冲突的中间混样：P1 = S1 + S3
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s3, "portions": 1}])
    assert add_rule(client, s1, s2).status_code == 201

    # 嵌套命中：P2 直接只引用 P1 与 S2，但经 P1 可达 S1 → 禁配
    r = client.post("/pools", json={"name": "P2", "recipe": [
        {"batch_id": p1, "portions": 2}, {"batch_id": s2, "portions": 3}]})
    assert r.status_code == 409
    assert [p["sample_id"] for p in r.json()["detail"]["pair"]] == [s1, s2]


def test_multiple_hits_return_lexicographically_first_pair(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    s4 = make_sample(client, "S4")
    # 同一混样将命中 (s1,s4) 与 (s2,s3) 两条规则，字典序第一为 (s1,s4)
    assert add_rule(client, s2, s3).status_code == 201
    assert add_rule(client, s1, s4).status_code == 201
    r = client.post("/pools", json={"name": "PX", "recipe": [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
        {"batch_id": s3, "portions": 1},
        {"batch_id": s4, "portions": 1},
    ]})
    assert r.status_code == 409
    assert [p["sample_id"] for p in r.json()["detail"]["pair"]] == [s1, s4]


def test_rules_only_constrain_future_pools(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    # 规则登记前已创建的混样保留不动，占比接口照常工作
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    assert add_rule(client, s1, s2).status_code == 201
    assert fractions_of(client, p1) == {s1: (1, 2), s2: (1, 2)}
    assert client.get(f"/batches/{p1}").status_code == 200
    # 规则只约束随后创建的批次：即使引用旧批次 P1，只要新批次可达的样本不触禁即可
    p_s1 = make_pool(client, "Q1", [{"batch_id": s1, "portions": 1}])
    p_s3 = make_pool(client, "Q3", [{"batch_id": s3, "portions": 1}])
    assert client.post("/pools", json={"name": "P2", "recipe": [
        {"batch_id": p_s1, "portions": 1}, {"batch_id": s3, "portions": 1}]}).status_code == 201
    # 但新批次经旧批次 P1 同时可达 S1、S2 时，仍按规则拒绝
    r = client.post("/pools", json={"name": "P3", "recipe": [
        {"batch_id": p1, "portions": 1}, {"batch_id": p_s3, "portions": 1}]})
    assert r.status_code == 409
    assert [p["sample_id"] for p in r.json()["detail"]["pair"]] == [s1, s2]


def test_concurrent_rule_registration_and_pool_creation(client):
    # 并发登记规则与创建混样：结果只能是“规则先到→409，批次不存在”
    # 或“批次先到→201”，不允许出现规则已生效却漏检（201 且规则存在）
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    pool_codes: list[int] = []
    rule_code: dict[str, int] = {}

    def register():
        barrier.wait()
        r = add_rule(client, s1, s2)
        rule_code["status"] = r.status_code

    def create(idx):
        barrier.wait()
        r = client.post("/pools", json={
            "name": f"CP{idx}-{uuid4()}",
            "recipe": [
                {"batch_id": s1, "portions": 1},
                {"batch_id": s2, "portions": 1},
            ],
        })
        with lock:  # 按实际完成时间记录
            pool_codes.append(r.status_code)

    threads = [threading.Thread(target=register),
               threading.Thread(target=create, args=(1,)),
               threading.Thread(target=create, args=(2,)),
               threading.Thread(target=create, args=(3,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert rule_code["status"] in (200, 201)
    assert set(pool_codes) <= {201, 409}, pool_codes
    assert len(pool_codes) == 3
    # 最终规则一定存在；以写事务串行后，结果只能呈“若干 201 后全为 409”
    # 的单调形态——规则提交后不允许再漏检创建成功
    rules = client.get("/exclusion-rules").json()
    assert [(r["low_sample_id"], r["high_sample_id"]) for r in rules] == [(s1, s2)]
    first_reject = next((i for i, c in enumerate(pool_codes) if c == 409), None)
    if first_reject is not None:
        assert all(c == 409 for c in pool_codes[first_reject:])


# --------------------------------------------------------------------------- #
# POST /exclusion-rules/impact-preview
# --------------------------------------------------------------------------- #


def impact_preview(client, a, b):
    return client.post(
        "/exclusion-rules/impact-preview",
        json={"sample_a_id": a, "sample_b_id": b},
    )


def impact_map(body):
    return {
        b["batch_id"]: (b["is_first_confluence"], b["first_confluence_batch_ids"])
        for b in body["affected_batches"]
    }


def test_impact_preview_first_confluence_and_downstream(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    # P1 = S1 + S2：首次合流
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    # P2 = P1 + S3：仍含两样本但直接原料 P1 已含两者 → 非首次合流
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s3, "portions": 1}])
    # P3 仅含 S1（多路径投入 S1，份数合并），不应受影响
    p3 = make_pool(client, "P3", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s1, "portions": 2}])
    # P4 = P2 + P3：下游汇合，经 P2 可追溯到 P1
    p4 = make_pool(client, "P4", [
        {"batch_id": p2, "portions": 1}, {"batch_id": p3, "portions": 1}])

    r = impact_preview(client, s2, s1)  # 倒序请求，响应归一升序
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["low_sample_id"] == s1
    assert body["high_sample_id"] == s2
    assert [b["batch_id"] for b in body["affected_batches"]] == [p1, p2, p4]
    assert impact_map(body) == {
        p1: (True, [p1]),
        p2: (False, [p1]),
        p4: (False, [p1]),
    }
    assert body["affected_batches"][0]["batch_name"] == "P1"


def test_impact_preview_multi_path_merge_deduped(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    # P2、P3 都引用 P1：两样本经它们各自可达 P1
    p2 = make_pool(client, "P2", [{"batch_id": p1, "portions": 1}])
    p3 = make_pool(client, "P3", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s1, "portions": 1}])
    # P4 经 P2、P3 两条路径汇合到 P1：P1 只追溯一次
    p4 = make_pool(client, "P4", [
        {"batch_id": p2, "portions": 1}, {"batch_id": p3, "portions": 1}])

    r = impact_preview(client, s1, s2)
    assert r.status_code == 200, r.text
    result = impact_map(r.json())
    assert result == {
        p1: (True, [p1]),
        p2: (False, [p1]),
        p3: (False, [p1]),
        p4: (False, [p1]),  # 多路径汇合，不重复
    }


def test_impact_preview_multiple_independent_first_confluences(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    # 两个互不相干的首次合流
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    p2 = make_pool(client, "P2", [
        {"batch_id": s3, "portions": 1}, {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1}])
    # P3 = P1 + P2：可追溯的首次合流为两个，按 id 升序、无遗漏
    p3 = make_pool(client, "P3", [
        {"batch_id": p1, "portions": 1}, {"batch_id": p2, "portions": 1}])
    # P4 只引用 P1：只追溯到 P1
    p4 = make_pool(client, "P4", [{"batch_id": p1, "portions": 2}])

    r = impact_preview(client, s1, s2)
    assert r.status_code == 200, r.text
    assert impact_map(r.json()) == {
        p1: (True, [p1]),
        p2: (True, [p2]),
        p3: (False, [p1, p2]),
        p4: (False, [p1]),
    }


def test_impact_preview_first_confluence_is_nested_not_direct(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # Q1 只含 S1、Q2 只含 S2
    q1 = make_pool(client, "Q1", [{"batch_id": s1, "portions": 1}])
    q2 = make_pool(client, "Q2", [{"batch_id": s2, "portions": 1}])
    # P1 = Q1 + Q2：两样本首次在此汇合（直接原料都不同时含两者）
    p1 = make_pool(client, "P1", [
        {"batch_id": q1, "portions": 1}, {"batch_id": q2, "portions": 1}])
    p2 = make_pool(client, "P2", [{"batch_id": p1, "portions": 1}])

    r = impact_preview(client, s1, s2)
    assert r.status_code == 200, r.text
    assert impact_map(r.json()) == {
        p1: (True, [p1]),
        p2: (False, [p1]),
    }


def test_impact_preview_pair_never_meets_returns_empty(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    make_pool(client, "P2", [
        {"batch_id": s2, "portions": 1}, {"batch_id": s3, "portions": 1}])

    r = impact_preview(client, s1, s2)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["low_sample_id"] == s1
    assert body["high_sample_id"] == s2
    assert body["affected_batches"] == []


def test_impact_preview_works_without_registered_rule_and_is_readonly(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])

    before_batches = client.get("/batches").json()
    before_rules = client.get("/exclusion-rules").json()
    r = impact_preview(client, s1, s2)
    assert r.status_code == 200, r.text
    # 规则未补录、配方与批次均未改动
    assert client.get("/exclusion-rules").json() == before_rules
    assert client.get("/batches").json() == before_batches
    # 规则已登记时预演结果一致
    assert add_rule(client, s1, s2).status_code == 201
    r2 = impact_preview(client, s1, s2)
    assert r2.status_code == 200
    assert r2.json()["affected_batches"] == r.json()["affected_batches"]


def test_impact_preview_rejects_same_unknown_and_pool(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])

    # 两个 id 相同 → 400
    r = impact_preview(client, s1, s1)
    assert r.status_code == 400
    assert "不同" in r.json()["detail"]
    # 未知 id → 400
    r = impact_preview(client, s1, 999)
    assert r.status_code == 400
    assert "999" in r.json()["detail"]
    # 混样批次不是原始样本 → 400
    r = impact_preview(client, s1, p1)
    assert r.status_code == 400
    assert str(p1) in r.json()["detail"]
    assert "原始样本" in r.json()["detail"]
    # 预演不写库
    assert client.get("/exclusion-rules").json() == []


def test_impact_preview_invalid_payload_rejected(client):
    # 缺字段 / 非正整数 → 422
    r = client.post("/exclusion-rules/impact-preview", json={"sample_a_id": 1})
    assert r.status_code == 422
    r = client.post("/exclusion-rules/impact-preview",
                    json={"sample_a_id": 0, "sample_b_id": 2})
    assert r.status_code == 422



# --------------------------------------------------------------------------- #
# POST /batches/{id}/portion-diagnosis
# --------------------------------------------------------------------------- #


def diagnose(client, batch_id, measurements, max_portions):
    return client.post(f"/batches/{batch_id}/portion-diagnosis", json={
        "measurements": measurements,
        "max_portions": max_portions,
    })


def measured(*entries):
    """(sample_id, numerator, denominator) 列表 → measurements 请求体。"""
    return [
        {"sample_id": sid, "numerator": num, "denominator": den}
        for sid, num, den in entries
    ]


def test_portion_diagnosis_two_ingredients_both_directions(client):
    # P1 记录为 2×S1 + 1×S2，实测 1/2 + 1/2：记错 S1（2→1）或记错 S2（1→2）都成立
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2}, {"batch_id": s2, "portions": 1}])
    r = diagnose(client, p1, measured((s1, 1, 2), (s2, 1, 2)), 10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch_id"] == p1
    assert body["batch_name"] == "P1"
    # 解按原料 id 升序
    assert body["solutions"] == [
        {"ingredient_batch_id": s1, "ingredient_name": "S1",
         "original_portions": 2, "corrected_portions": 1},
        {"ingredient_batch_id": s2, "ingredient_name": "S2",
         "original_portions": 1, "corrected_portions": 2},
    ]
    assert body["undetermined"] == []


def test_portion_diagnosis_nested_recipe(client):
    # P2 记录为 1×P1 + 1×S3，实际应为 3×P1 + 1×S3 → S1=3/8, S2=3/8, S3=1/4
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s3, "portions": 1}])
    r = diagnose(client, p2, measured((s1, 3, 8), (s2, 3, 8), (s3, 1, 4)), 10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solutions"] == [
        {"ingredient_batch_id": p1, "ingredient_name": "P1",
         "original_portions": 1, "corrected_portions": 3},
    ]
    assert body["undetermined"] == []


def test_portion_diagnosis_undetermined_when_portions_irrelevant(client):
    # Q 与 S1 组成完全相同（都是纯 S1）：P 中两项份数怎么变组成都不变
    s1 = make_sample(client, "S1")
    q = make_pool(client, "Q", [{"batch_id": s1, "portions": 2}])
    p = make_pool(client, "P", [
        {"batch_id": s1, "portions": 1}, {"batch_id": q, "portions": 1}])
    r = diagnose(client, p, measured((s1, 1, 1)), 10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["solutions"] == []
    # 无法判定的项不枚举份数
    assert body["undetermined"] == [
        {"ingredient_batch_id": s1, "ingredient_name": "S1", "original_portions": 1},
        {"ingredient_batch_id": q, "ingredient_name": "Q", "original_portions": 1},
    ]


def test_portion_diagnosis_no_solution_returns_empty(client):
    # 实测 2/5 + 3/5：记错任一项都需要非整数份数（2/3 或 3/2），无解
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    r = diagnose(client, p1, measured((s1, 2, 5), (s2, 3, 5)), 100)
    assert r.status_code == 200, r.text
    assert r.json()["solutions"] == []
    assert r.json()["undetermined"] == []


def test_portion_diagnosis_max_portions_cap(client):
    # 实测 5/6 + 1/6 → 唯一解要求 S1 投入 5 份
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    # 超过上限 → 无解
    r = diagnose(client, p1, measured((s1, 5, 6), (s2, 1, 6)), 4)
    assert r.status_code == 200, r.text
    assert r.json()["solutions"] == []
    # 恰好等于上限 → 接受
    r = diagnose(client, p1, measured((s1, 5, 6), (s2, 1, 6)), 5)
    assert r.status_code == 200, r.text
    assert r.json()["solutions"] == [
        {"ingredient_batch_id": s1, "ingredient_name": "S1",
         "original_portions": 1, "corrected_portions": 5},
    ]


def test_portion_diagnosis_solution_must_differ_from_original(client):
    # 实测与当前配方组成一致：解出的份数等于原值，不作为解返回
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    r = diagnose(client, p1, measured((s1, 1, 2), (s2, 1, 2)), 10)
    assert r.status_code == 200, r.text
    assert r.json()["solutions"] == []
    assert r.json()["undetermined"] == []


def test_portion_diagnosis_unlisted_sample_means_zero(client):
    # 未列出的 S3 视为 0：改变任何一项份数都无法让 S3 消失且保持 S1=S2=1/2
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1},
        {"batch_id": s2, "portions": 1},
        {"batch_id": s3, "portions": 1},
    ])
    r = diagnose(client, p1, measured((s1, 1, 2), (s2, 1, 2)), 10)
    assert r.status_code == 200, r.text
    assert r.json()["solutions"] == []
    assert r.json()["undetermined"] == []


def test_portion_diagnosis_does_not_modify_database(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2}, {"batch_id": s2, "portions": 1}])
    before_detail = client.get(f"/batches/{p1}").json()
    before_list = client.get("/batches").json()
    before_comp = fractions_of(client, p1)

    r = diagnose(client, p1, measured((s1, 1, 2), (s2, 1, 2)), 10)
    assert r.status_code == 200, r.text
    assert r.json()["solutions"] != []

    assert client.get(f"/batches/{p1}").json() == before_detail
    assert client.get("/batches").json() == before_list
    assert fractions_of(client, p1) == before_comp


def test_portion_diagnosis_duplicate_sample_rejected(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    r = diagnose(client, p1, [
        {"sample_id": s1, "numerator": 1, "denominator": 4},
        {"sample_id": s1, "numerator": 1, "denominator": 4},
        {"sample_id": s2, "numerator": 1, "denominator": 2},
    ], 10)
    assert r.status_code == 422
    assert "重复" in r.json()["detail"][0]["msg"]


def test_portion_diagnosis_sum_must_be_one(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    # 1/2 + 1/3 ≠ 1
    r = diagnose(client, p1, measured((s1, 1, 2), (s2, 1, 3)), 10)
    assert r.status_code == 422
    assert "之和" in r.json()["detail"][0]["msg"]
    # 空列表之和为 0，同样拒绝
    assert diagnose(client, p1, [], 10).status_code == 422


@pytest.mark.parametrize("numerator,denominator", [
    (-1, 2),    # 负分子
    (1, 0),     # 分母为 0
    (1.5, 2),   # 小数
    ("1", 2),   # 字符串
    (True, 2),  # 布尔
])
def test_portion_diagnosis_invalid_fraction_rejected(client, numerator, denominator):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    r = diagnose(client, p1, [
        {"sample_id": s1, "numerator": numerator, "denominator": denominator},
    ], 10)
    assert r.status_code == 422


@pytest.mark.parametrize("cap", [0, -1, 1.5, 2.0, "3", True])
def test_portion_diagnosis_invalid_max_portions_rejected(client, cap):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    r = diagnose(client, p1, measured((s1, 1, 2), (s2, 1, 2)), cap)
    assert r.status_code == 422


def test_portion_diagnosis_unknown_or_non_sample_measurement_rejected(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    # 未知样本 id → 400
    r = diagnose(client, p1, measured((999, 1, 1)), 10)
    assert r.status_code == 400
    assert "999" in r.json()["detail"]
    # 混样批次不是原始样本 → 400
    r = diagnose(client, p1, measured((p1, 1, 1)), 10)
    assert r.status_code == 400
    assert "原始样本" in r.json()["detail"]


def test_portion_diagnosis_target_must_be_pool(client):
    s1 = make_sample(client, "S1")
    r = diagnose(client, s1, measured((s1, 1, 1)), 10)
    assert r.status_code == 400
    assert "混样" in r.json()["detail"]


def test_portion_diagnosis_unknown_target_returns_404(client):
    s1 = make_sample(client, "S1")
    assert diagnose(client, 4242, measured((s1, 1, 1)), 10).status_code == 404


# --------------------------------------------------------------------------- #
# POST/GET /batches/{id}/inventory 原始样本库存
# --------------------------------------------------------------------------- #


def add_inventory(client, sample_id, portions):
    return client.post(f"/batches/{sample_id}/inventory", json={"portions": portions})


def get_inventory(client, sample_id):
    return client.get(f"/batches/{sample_id}/inventory")


def test_add_inventory_creates_and_accumulates_balance(client):
    s1 = make_sample(client, "S1")
    # 从未补入：余额 0，updated_at 为 null
    r = get_inventory(client, s1)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"sample_id": s1, "sample_name": "S1", "balance": 0, "updated_at": None}

    r = add_inventory(client, s1, 5)
    assert r.status_code == 200, r.text
    assert r.json()["balance"] == 5
    assert r.json()["sample_name"] == "S1"
    assert r.json()["updated_at"] is not None

    # 再次补入：累加到余额
    r = add_inventory(client, s1, 3)
    assert r.status_code == 200
    assert r.json()["balance"] == 8
    assert get_inventory(client, s1).json()["balance"] == 8


@pytest.mark.parametrize("portions", [0, -3, 1.5, 2.0, "2", True])
def test_add_inventory_rejects_non_positive_integer(client, portions):
    s1 = make_sample(client, "S1")
    assert add_inventory(client, s1, portions).status_code == 422
    assert get_inventory(client, s1).json()["balance"] == 0


def test_inventory_unknown_batch_returns_400(client):
    r = add_inventory(client, 4242, 1)
    assert r.status_code == 400
    assert "4242" in r.json()["detail"]
    r = get_inventory(client, 4242)
    assert r.status_code == 400
    assert "4242" in r.json()["detail"]


def test_inventory_only_for_samples(client):
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    r = add_inventory(client, p1, 5)
    assert r.status_code == 400
    assert "原始样本" in r.json()["detail"]
    r = get_inventory(client, p1)
    assert r.status_code == 400
    assert "原始样本" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# POST /batches/{id}/preparations + GET /preparations/{id}
# --------------------------------------------------------------------------- #


def prepare(client, batch_id, times):
    return client.post(f"/batches/{batch_id}/preparations", json={"times": times})


def test_prepare_sample_consumes_itself_and_returns_remaining(client):
    s1 = make_sample(client, "S1")
    add_inventory(client, s1, 10)
    r = prepare(client, s1, 3)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["batch_id"] == s1
    assert body["batch_name"] == "S1"
    assert body["times"] == 3
    assert "id" in body and "created_at" in body
    assert body["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 3}]
    assert body["remaining_inventory"] == [{
        "sample_id": s1, "sample_name": "S1", "balance": 7,
        "updated_at": get_inventory(client, s1).json()["updated_at"],
    }]
    assert get_inventory(client, s1).json()["balance"] == 7


def test_prepare_pool_multiplies_recipe_portions(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2}, {"batch_id": s2, "portions": 1}])
    add_inventory(client, s1, 100)
    add_inventory(client, s2, 100)
    # 制备 3 次：每次 2×S1 + 1×S2 → 消耗 6×S1、3×S2
    r = prepare(client, p1, 3)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 6},
        {"sample_id": s2, "sample_name": "S2", "portions": 3},
    ]
    assert [c["sample_id"] for c in body["remaining_inventory"]] == [s1, s2]
    assert {c["sample_id"]: c["balance"] for c in body["remaining_inventory"]} == {
        s1: 94, s2: 97}


def test_prepare_nested_recipe_multiplies_layer_by_layer(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # P1 = 1×S1 + 1×S2
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    # P2 = 2×P1 + 3×S2：制备一次消耗 2×S1 + (2+3)×S2 = 2×S1、5×S2
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 2}, {"batch_id": s2, "portions": 3}])
    add_inventory(client, s1, 100)
    add_inventory(client, s2, 100)
    r = prepare(client, p2, 2)
    assert r.status_code == 201, r.text
    assert r.json()["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 4},
        {"sample_id": s2, "sample_name": "S2", "portions": 10},
    ]


def test_prepare_merges_multi_path_requirements(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # P1 = 1×S1 + 1×S2；P2 = 1×P1 + 1×S1
    # S1 经 P1（1 份）与直接投入（1 份）两条路径 → 制备一次消耗 2×S1、1×S2
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s1, "portions": 1}])
    add_inventory(client, s1, 10)
    add_inventory(client, s2, 10)
    r = prepare(client, p2, 1)
    assert r.status_code == 201, r.text
    assert r.json()["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 2},
        {"sample_id": s2, "sample_name": "S2", "portions": 1},
    ]
    assert get_inventory(client, s1).json()["balance"] == 8
    assert get_inventory(client, s2).json()["balance"] == 9


def test_prepare_exact_balance_succeeds_and_leaves_zero(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2}, {"batch_id": s2, "portions": 1}])
    add_inventory(client, s1, 6)
    add_inventory(client, s2, 3)
    r = prepare(client, p1, 3)  # 恰好耗尽
    assert r.status_code == 201, r.text
    assert {c["sample_id"]: c["balance"] for c in r.json()["remaining_inventory"]} == {
        s1: 0, s2: 0}
    assert get_inventory(client, s1).json()["balance"] == 0


def test_prepare_insufficient_inventory_returns_409_with_shortages(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    s3 = make_sample(client, "S3")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2}, {"batch_id": s2, "portions": 1}])
    # P2 经 P1 消耗 S1/S2，另直接消耗 S3；制备 2 次需 4×S1、2×S2、2×S3
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s3, "portions": 1}])
    add_inventory(client, s1, 4)
    add_inventory(client, s2, 1)  # 缺 1
    # S3 从未补入：可用 0，缺 2

    r = prepare(client, p2, 2)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message"] == "原始样本库存不足"
    gaps = {x["sample_id"]: x for x in detail["shortages"]}
    assert set(gaps) == {s2, s3}  # S1 恰好够，不出现在缺口中
    assert gaps[s2] == {
        "sample_id": s2, "sample_name": "S2",
        "required": 2, "available": 1, "shortage": 1}
    assert gaps[s3] == {
        "sample_id": s3, "sample_name": "S3",
        "required": 2, "available": 0, "shortage": 2}


def test_prepare_conflict_changes_nothing(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2}, {"batch_id": s2, "portions": 1}])
    add_inventory(client, s1, 3)  # 制备 2 次需 4×S1，不足
    add_inventory(client, s2, 100)
    before_s1 = get_inventory(client, s1).json()
    before_s2 = get_inventory(client, s2).json()

    assert prepare(client, p1, 2).status_code == 409

    # 库存与制备记录均不变化
    assert get_inventory(client, s1).json() == before_s1
    assert get_inventory(client, s2).json() == before_s2
    assert client.get("/preparations/1").status_code == 404
    # 原余额仍可完成 1 次制备
    r = prepare(client, p1, 1)
    assert r.status_code == 201
    assert get_inventory(client, s1).json()["balance"] == 1


@pytest.mark.parametrize("times", [0, -3, 1.5, 2.0, "2", True])
def test_prepare_rejects_non_positive_integer_times(client, times):
    s1 = make_sample(client, "S1")
    assert prepare(client, s1, times).status_code == 422


def test_prepare_unknown_batch_returns_400(client):
    r = prepare(client, 4242, 1)
    assert r.status_code == 400
    assert "4242" in r.json()["detail"]


def test_get_preparation_by_id_returns_record_and_live_remaining(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    add_inventory(client, s1, 10)
    add_inventory(client, s2, 10)
    first = prepare(client, p1, 1)
    assert first.status_code == 201
    prep_id = first.json()["id"]
    add_inventory(client, s1, 5)  # 制备后再补库：GET 返回实时余额

    r = client.get(f"/preparations/{prep_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    # 消耗明细是历史快照，不随后续补库改变
    assert body["consumptions"] == first.json()["consumptions"]
    assert body["times"] == 1
    assert body["batch_id"] == p1
    assert body["batch_name"] == "P1"
    # 剩余库存为实时值（S1: 10-1+5=14，S2: 9）
    assert {c["sample_id"]: c["balance"] for c in body["remaining_inventory"]} == {
        s1: 14, s2: 9}
    # 重复读取结果一致
    assert client.get(f"/preparations/{prep_id}").json() == body


def test_get_unknown_preparation_returns_404(client):
    r = client.get("/preparations/4242")
    assert r.status_code == 404
    assert "4242" in r.json()["detail"]


def test_preparation_consumptions_are_persisted_individually(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    add_inventory(client, s1, 10)
    add_inventory(client, s2, 10)
    r1 = prepare(client, p1, 2)
    r2 = prepare(client, p1, 3)
    assert r1.status_code == 201 and r2.status_code == 201
    id1, id2 = r1.json()["id"], r2.json()["id"]
    assert id2 == id1 + 1
    rec1 = client.get(f"/preparations/{id1}").json()
    rec2 = client.get(f"/preparations/{id2}").json()
    assert [c["portions"] for c in rec1["consumptions"]] == [2, 2]
    assert [c["portions"] for c in rec2["consumptions"]] == [3, 3]
    # 累计扣减 5 份/样本
    assert get_inventory(client, s1).json()["balance"] == 5
    assert get_inventory(client, s2).json()["balance"] == 5


def test_concurrent_preparations_serialize_without_overselling(client):
    # 并发制备同一批次：库存只够部分请求成功，成功数 × 单耗不得超过库存；
    # 同一 SQLite 写事务串行化后，不允许出现超扣（余额为负）或记录与余额不一致
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    add_inventory(client, s1, 5)
    barrier = threading.Barrier(6)
    codes: list[int] = []
    lock = threading.Lock()

    def create():
        barrier.wait()
        r = prepare(client, p1, 1)
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=create) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(codes) == [201] * 5 + [409]
    assert get_inventory(client, s1).json()["balance"] == 0
    # 5 条成功记录各消耗 1 份
    list_resp = [client.get(f"/preparations/{i}").status_code for i in range(1, 7)]
    assert list_resp == [200] * 5 + [404]


# --------------------------------------------------------------------------- #
# POST /preparation-jobs 整单制备
# --------------------------------------------------------------------------- #


def prepare_job(client, items):
    return client.post("/preparation-jobs", json={"items": items})


def test_preparation_job_merges_duplicates_and_aggregates_consumption(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    # P1 = 1×S1 + 1×S2；P2 = 1×P1 + 1×S1（S1 经两条路径 → 每次 2×S1、1×S2）
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s1, "portions": 1}])
    add_inventory(client, s1, 100)
    add_inventory(client, s2, 100)

    # 清单乱序且 P2 重复出现（1+2=3 次）：响应按批次 id 升序、各一条记录
    r = prepare_job(client, [
        {"batch_id": p2, "times": 1},
        {"batch_id": p1, "times": 2},
        {"batch_id": p2, "times": 2},
    ])
    assert r.status_code == 201, r.text
    body = r.json()
    assert [(p["batch_id"], p["times"]) for p in body["preparations"]] == [
        (p1, 2), (p2, 3)]
    assert body["preparations"][0]["batch_name"] == "P1"
    assert body["preparations"][0]["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 2},
        {"sample_id": s2, "sample_name": "S2", "portions": 2},
    ]
    assert body["preparations"][1]["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 6},
        {"sample_id": s2, "sample_name": "S2", "portions": 3},
    ]
    # 整单消耗按样本汇总：S1 = 2×1 + 3×2 = 8，S2 = 2×1 + 3×1 = 5
    assert body["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 8},
        {"sample_id": s2, "sample_name": "S2", "portions": 5},
    ]
    assert get_inventory(client, s1).json()["balance"] == 92
    assert get_inventory(client, s2).json()["balance"] == 95

    # 每个批次各一条制备记录，可由现有查询接口读取
    ids = [p["preparation_id"] for p in body["preparations"]]
    assert len(set(ids)) == 2
    rec1 = client.get(f"/preparations/{ids[0]}")
    assert rec1.status_code == 200
    assert rec1.json()["batch_id"] == p1
    assert rec1.json()["times"] == 2
    assert rec1.json()["consumptions"] == body["preparations"][0]["consumptions"]
    rec2 = client.get(f"/preparations/{ids[1]}")
    assert rec2.status_code == 200
    assert rec2.json()["batch_id"] == p2
    assert rec2.json()["times"] == 3


def test_preparation_job_shared_sample_judged_on_total(client):
    # 共享样本必须按整单汇总需求核算：S1 余 5 份，P1、P2 各需 3 份，
    # 单项分别判断都“足量”，汇总后 6 > 5 必须 409
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 3}])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    add_inventory(client, s1, 5)
    add_inventory(client, s2, 10)
    before_s1 = get_inventory(client, s1).json()
    before_s2 = get_inventory(client, s2).json()

    r = prepare_job(client, [
        {"batch_id": p1, "times": 1},
        {"batch_id": p2, "times": 1},
    ])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["message"] == "原始样本库存不足"
    # 缺口按整单总需求核算：S1 总需 3+3=6、余额 5、缺口 1；S2 足量不出现
    assert detail["shortages"] == [{
        "sample_id": s1, "sample_name": "S1",
        "required": 6, "available": 5, "shortage": 1,
    }]

    # 整单不留记录且余额不变
    assert get_inventory(client, s1).json() == before_s1
    assert get_inventory(client, s2).json() == before_s2
    assert client.get("/preparations/1").status_code == 404
    # 补足后可原样重试成功
    add_inventory(client, s1, 1)
    assert prepare_job(client, [
        {"batch_id": p1, "times": 1},
        {"batch_id": p2, "times": 1},
    ]).status_code == 201
    assert get_inventory(client, s1).json()["balance"] == 0
    assert get_inventory(client, s2).json()["balance"] == 9


def test_preparation_job_sample_items_and_exact_balance(client):
    # 原始样本也可作为清单项（每次消耗自身 1 份）；恰好耗尽视为足量
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 2}, {"batch_id": s2, "portions": 1}])
    add_inventory(client, s1, 5)
    add_inventory(client, s2, 1)

    r = prepare_job(client, [
        {"batch_id": p1, "times": 1},   # 2×S1 + 1×S2
        {"batch_id": s1, "times": 3},   # 3×S1
    ])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 5},
        {"sample_id": s2, "sample_name": "S2", "portions": 1},
    ]
    assert get_inventory(client, s1).json()["balance"] == 0
    assert get_inventory(client, s2).json()["balance"] == 0


def test_preparation_job_empty_items_rejected(client):
    assert prepare_job(client, []).status_code == 422


@pytest.mark.parametrize("times", [0, -3, 1.5, 2.0, "2", True])
def test_preparation_job_rejects_non_positive_integer_times(client, times):
    s1 = make_sample(client, "S1")
    r = prepare_job(client, [{"batch_id": s1, "times": times}])
    assert r.status_code == 422


def test_preparation_job_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    r = prepare_job(client, [
        {"batch_id": s1, "times": 1},
        {"batch_id": 999, "times": 1},
    ])
    assert r.status_code == 400
    assert "999" in r.json()["detail"]
    # 校验失败不写库：已知样本也未被扣减（本就没补入，余额为 0）
    assert get_inventory(client, s1).json()["balance"] == 0
    assert client.get("/preparations/1").status_code == 404


def test_preparation_job_never_stocked_sample_counts_as_zero(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    add_inventory(client, s1, 10)  # S2 从未补入：可用 0

    r = prepare_job(client, [{"batch_id": p1, "times": 2}])
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["shortages"] == [{
        "sample_id": s2, "sample_name": "S2",
        "required": 2, "available": 0, "shortage": 2,
    }]
    assert get_inventory(client, s1).json()["balance"] == 10


def test_concurrent_preparation_jobs_serialize_without_overselling(client):
    # 并发整单：库存只够部分整单成功；同一写事务串行化后不允许超扣，
    # 失败的整单不留任何制备记录
    s1 = make_sample(client, "S1")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 1}])
    add_inventory(client, s1, 5)
    barrier = threading.Barrier(6)
    codes: list[int] = []
    lock = threading.Lock()

    def create():
        barrier.wait()
        r = prepare_job(client, [{"batch_id": p1, "times": 1}])
        with lock:
            codes.append(r.status_code)

    threads = [threading.Thread(target=create) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(codes) == [201] * 5 + [409]
    assert get_inventory(client, s1).json()["balance"] == 0
    list_resp = [client.get(f"/preparations/{i}").status_code for i in range(1, 7)]
    assert list_resp == [200] * 5 + [404]


# --------------------------------------------------------------------------- #
# POST /preparation-jobs/selection-preview 只读排产预演
# --------------------------------------------------------------------------- #


def selection_preview(client, items):
    return client.post("/preparation-jobs/selection-preview", json={"items": items})


def test_selection_preview_maximizes_order_count_then_times(client):
    # S1 余 5：订单1（P1×2 需 4）与订单2+3（S1×2 + S1×2 共 4）不可兼得（4+2>5），
    # 先比订单数（2 > 1）→ 选 {2,3}；订单4（S1×2）与 2、3 需求相同，
    # 订单数、次数之和均并列时取位置序列字典序最小者 → {2,3} 而非 {2,4}/{3,4}
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [{"batch_id": s1, "portions": 2}])
    add_inventory(client, s1, 5)

    r = selection_preview(client, [
        {"batch_id": p1, "times": 2},
        {"batch_id": s1, "times": 2},
        {"batch_id": s1, "times": 2},
        {"batch_id": s1, "times": 2},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["selected_positions"] == [2, 3]
    assert body["unselected_positions"] == [1, 4]
    assert body["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 4}]
    remaining = body["remaining_inventory"]
    assert [(x["sample_id"], x["balance"]) for x in remaining] == [(s1, 1)]
    assert remaining[0]["sample_name"] == "S1"
    assert remaining[0]["updated_at"] == get_inventory(client, s1).json()["updated_at"]


def test_selection_preview_prefers_larger_times_sum_on_count_tie(client):
    # 订单数并列（各 1 条）时比次数之和：{2}（4 次）优于 {1}（3 次）
    s1 = make_sample(client, "S1")
    add_inventory(client, s1, 4)

    r = selection_preview(client, [
        {"batch_id": s1, "times": 3},
        {"batch_id": s1, "times": 4},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["selected_positions"] == [2]
    assert body["unselected_positions"] == [1]
    assert body["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 4}]
    assert [x["balance"] for x in body["remaining_inventory"]] == [0]


def test_selection_preview_duplicate_batches_are_independent_orders(client):
    # 允许重复批次：不合并次数，各自按位置独立参与选型；S1 余 2 → 三选二，
    # 订单数与次数之和均并列，取位置序列字典序最小的 {1,2}
    s1 = make_sample(client, "S1")
    add_inventory(client, s1, 2)

    r = selection_preview(client, [
        {"batch_id": s1, "times": 1},
        {"batch_id": s1, "times": 1},
        {"batch_id": s1, "times": 1},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["selected_positions"] == [1, 2]
    assert body["unselected_positions"] == [3]
    assert body["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 2}]
    assert [x["balance"] for x in body["remaining_inventory"]] == [0]


def test_selection_preview_empty_subset_when_nothing_fits(client):
    # 库存不足时允许空集：未补入过库存的样本余额为 0，任何订单都不可行
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])

    r = selection_preview(client, [
        {"batch_id": p1, "times": 1},
        {"batch_id": s1, "times": 1},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["selected_positions"] == []
    assert body["unselected_positions"] == [1, 2]
    assert body["consumptions"] == []
    assert body["remaining_inventory"] == []


def test_selection_preview_nested_recipe_and_multi_sample_balance(client):
    # 嵌套配方逐层乘算：P2 每次消耗 2×S1 + 1×S2；S1 余 3、S2 余 5 时
    # P2×2（需 4×S1）不可行，S1×3 可行
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    p2 = make_pool(client, "P2", [
        {"batch_id": p1, "portions": 1}, {"batch_id": s1, "portions": 1}])
    add_inventory(client, s1, 3)
    add_inventory(client, s2, 5)

    r = selection_preview(client, [
        {"batch_id": p2, "times": 2},
        {"batch_id": s1, "times": 3},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["selected_positions"] == [2]
    assert body["unselected_positions"] == [1]
    assert body["consumptions"] == [
        {"sample_id": s1, "sample_name": "S1", "portions": 3}]
    assert [x["balance"] for x in body["remaining_inventory"]] == [0]


def test_selection_preview_is_readonly(client):
    s1 = make_sample(client, "S1")
    s2 = make_sample(client, "S2")
    p1 = make_pool(client, "P1", [
        {"batch_id": s1, "portions": 1}, {"batch_id": s2, "portions": 1}])
    add_inventory(client, s1, 5)
    add_inventory(client, s2, 5)
    before_s1 = get_inventory(client, s1).json()
    before_s2 = get_inventory(client, s2).json()
    before_batches = client.get("/batches").json()

    r = selection_preview(client, [{"batch_id": p1, "times": 2}])
    assert r.status_code == 200, r.text
    assert r.json()["selected_positions"] == [1]

    # 不扣减库存、不创建制备记录、不改动批次
    assert get_inventory(client, s1).json() == before_s1
    assert get_inventory(client, s2).json() == before_s2
    assert client.get("/preparations/1").status_code == 404
    assert client.get("/batches").json() == before_batches
    # 重复调用结果一致
    assert selection_preview(client, [{"batch_id": p1, "times": 2}]).json() == r.json()


def test_selection_preview_unknown_batch_rejected(client):
    s1 = make_sample(client, "S1")
    add_inventory(client, s1, 5)
    r = selection_preview(client, [
        {"batch_id": s1, "times": 1},
        {"batch_id": 999, "times": 1},
        {"batch_id": 999, "times": 2},  # 未知批次重复出现，detail 中去重
    ])
    assert r.status_code == 400
    assert "999" in r.json()["detail"]
    # 校验失败同样只读：余额不变、无制备记录
    assert get_inventory(client, s1).json()["balance"] == 5
    assert client.get("/preparations/1").status_code == 404


def test_selection_preview_items_size_limits(client):
    s1 = make_sample(client, "S1")
    assert selection_preview(client, []).status_code == 422
    items = [{"batch_id": s1, "times": 1}] * 13
    assert selection_preview(client, items).status_code == 422
    # 12 条恰好在上限内
    items = [{"batch_id": s1, "times": 1}] * 12
    assert selection_preview(client, items).status_code == 200


@pytest.mark.parametrize("times", [0, -3, 1.5, 2.0, "2", True])
def test_selection_preview_rejects_non_positive_integer_times(client, times):
    s1 = make_sample(client, "S1")
    r = selection_preview(client, [{"batch_id": s1, "times": times}])
    assert r.status_code == 422

