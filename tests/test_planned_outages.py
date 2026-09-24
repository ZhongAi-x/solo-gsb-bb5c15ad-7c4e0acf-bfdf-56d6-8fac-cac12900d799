"""设备计划停报模块测试。

覆盖：
- 按设备登记 / 列出计划停报时段（半开 [start,end)、窗口网格对齐、end>start、404/422）；
- GET /devices/{id}/missing-windows 的 exclude_planned 参数：默认 false 行为不变，
  true 时先按原口径求缺报再合并扣除重叠计划区间（重叠不重复扣、端点触碰不算重叠、
  计划超出查询范围裁剪、未封存窗口不被误扣、迟到填空后再扣除）；
- 区间运算为段级算术（大跨度登记不逐窗遍历，结果仍正确拆分）。
"""


def make_device(client, device_id="dev-1", window_size_seconds=60, allowed_lateness_seconds=30):
    r = client.put(
        f"/devices/{device_id}/config",
        json={
            "window_size_seconds": window_size_seconds,
            "allowed_lateness_seconds": allowed_lateness_seconds,
        },
    )
    assert r.status_code == 200, r.text


def ingest(client, device_id, event_id, ts, value=1.0):
    r = client.post(
        "/ingest",
        json={"device_id": device_id, "events": [
            {"event_id": event_id, "event_time": ts, "value": value}
        ]},
    )
    assert r.status_code == 200, r.text
    return r


def register(client, device_id, start, end, reason=None):
    body = {"start": start, "end": end}
    if reason is not None:
        body["reason"] = reason
    return client.post(f"/devices/{device_id}/planned-outages", json=body)


def list_outages(client, device_id, **params):
    return client.get(f"/devices/{device_id}/planned-outages", params=params)


def missing(client, device_id, start, end, exclude_planned=None):
    params = {"start": start, "end": end}
    if exclude_planned is not None:
        params["exclude_planned"] = str(exclude_planned).lower()
    return client.get(f"/devices/{device_id}/missing-windows", params=params)


def seed_gap(client, device_id="dev-1"):
    """窗口记录：00:00、00:03、00:04；水位线 00:04:00（lateness=30s）。

    封存条件覆盖到窗口起点 00:02，故原始缺报段为 [00:01, 00:03)（2 个窗口）。
    """
    make_device(client, device_id)
    ingest(client, device_id, "e1", "2026-09-22T00:00:10Z")
    ingest(client, device_id, "e2", "2026-09-22T00:03:10Z")
    ingest(client, device_id, "e3", "2026-09-22T00:04:00Z")


# ---------- 登记接口 ----------

def test_register_planned_outage_returns_201(client):
    make_device(client)
    r = register(
        client, "dev-1",
        "2026-09-22T00:01:00Z", "2026-09-22T00:03:00Z",
        reason="  scheduled maintenance ",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] == 1
    assert body["device_id"] == "dev-1"
    assert body["start"] == "2026-09-22T00:01:00Z"
    assert body["end"] == "2026-09-22T00:03:00Z"
    assert body["reason"] == "scheduled maintenance"  # 备注去首尾空白
    assert body["created_at"]


def test_register_without_reason_stores_null(client):
    make_device(client)
    r = register(client, "dev-1", "2026-09-22T00:01:00Z", "2026-09-22T00:03:00Z")
    assert r.status_code == 201
    assert r.json()["reason"] is None
    # 纯空白备注同样记 null
    r2 = register(
        client, "dev-1", "2026-09-22T00:04:00Z", "2026-09-22T00:05:00Z", reason="   "
    )
    assert r2.status_code == 201
    assert r2.json()["reason"] is None


def test_register_on_missing_device_is_404(client):
    r = register(client, "ghost", "2026-09-22T00:01:00Z", "2026-09-22T00:03:00Z")
    assert r.status_code == 404


def test_register_end_not_after_start_is_422(client):
    make_device(client)
    assert register(
        client, "dev-1", "2026-09-22T00:03:00Z", "2026-09-22T00:03:00Z"
    ).status_code == 422
    assert register(
        client, "dev-1", "2026-09-22T00:04:00Z", "2026-09-22T00:03:00Z"
    ).status_code == 422


def test_register_misaligned_to_device_window_grid_is_422(client):
    make_device(client)
    # 60s 网格：秒级偏移非法
    assert register(
        client, "dev-1", "2026-09-22T00:01:30Z", "2026-09-22T00:03:00Z"
    ).status_code == 422
    assert register(
        client, "dev-1", "2026-09-22T00:01:00Z", "2026-09-22T00:03:30Z"
    ).status_code == 422
    # 非法登记不得落库
    assert list_outages(client, "dev-1").json() == []


def test_register_alignment_uses_device_window_size(client):
    make_device(client, "dev-big", window_size_seconds=300)
    # 对齐 60s 但不对齐 300s
    assert register(
        client, "dev-big", "2026-09-22T00:01:00Z", "2026-09-22T00:06:00Z"
    ).status_code == 422
    assert register(
        client, "dev-big", "2026-09-22T00:00:00Z", "2026-09-22T00:05:00Z"
    ).status_code == 201


def test_register_allows_overlapping_and_touching_registrations(client):
    make_device(client)
    assert register(
        client, "dev-1", "2026-09-22T00:01:00Z", "2026-09-22T00:05:00Z"
    ).status_code == 201
    # 重叠登记允许（各自留痕，扣除时合并去重）
    assert register(
        client, "dev-1", "2026-09-22T00:03:00Z", "2026-09-22T00:07:00Z"
    ).status_code == 201
    # 端点相接的登记也允许
    assert register(
        client, "dev-1", "2026-09-22T00:05:00Z", "2026-09-22T00:06:00Z"
    ).status_code == 201
    rows = list_outages(client, "dev-1").json()
    assert [(r["start"], r["end"]) for r in rows] == [
        ("2026-09-22T00:01:00Z", "2026-09-22T00:05:00Z"),
        ("2026-09-22T00:03:00Z", "2026-09-22T00:07:00Z"),
        ("2026-09-22T00:05:00Z", "2026-09-22T00:06:00Z"),
    ]
    assert [r["id"] for r in rows] == [1, 2, 3]


# ---------- 列出接口 ----------

def test_list_outages_empty_for_unknown_range_and_missing_device(client):
    seed_gap(client)
    assert list_outages(client, "dev-1").json() == []
    assert list_outages(client, "ghost").status_code == 404


def test_list_outages_filter_by_intersection_half_open(client):
    make_device(client)
    register(client, "dev-1", "2026-09-22T00:01:00Z", "2026-09-22T00:03:00Z")
    register(client, "dev-1", "2026-09-22T00:05:00Z", "2026-09-22T00:07:00Z")
    register(client, "dev-1", "2026-09-22T00:09:00Z", "2026-09-22T00:10:00Z")

    # 与 [00:03, 00:09) 严格相交：端点相接（[...,00:03) 与 [00:09,...)）不算相交
    rows = list_outages(
        client, "dev-1",
        start="2026-09-22T00:03:00Z", end="2026-09-22T00:09:00Z",
    ).json()
    assert [(r["start"], r["end"]) for r in rows] == [
        ("2026-09-22T00:05:00Z", "2026-09-22T00:07:00Z"),
    ]

    # 部分相交也入选（裁剪由缺报扣除负责，列表只做相交过滤）
    rows = list_outages(
        client, "dev-1",
        start="2026-09-22T00:02:00Z", end="2026-09-22T00:06:00Z",
    ).json()
    assert [(r["start"], r["end"]) for r in rows] == [
        ("2026-09-22T00:01:00Z", "2026-09-22T00:03:00Z"),
        ("2026-09-22T00:05:00Z", "2026-09-22T00:07:00Z"),
    ]

    # 单侧过滤
    rows = list_outages(client, "dev-1", start="2026-09-22T00:05:00Z").json()
    assert [r["start"] for r in rows] == [
        "2026-09-22T00:05:00Z", "2026-09-22T00:09:00Z"
    ]
    rows = list_outages(client, "dev-1", end="2026-09-22T00:05:00Z").json()
    assert [r["start"] for r in rows] == ["2026-09-22T00:01:00Z"]


def test_list_outages_inverted_filter_is_422(client):
    seed_gap(client)
    assert list_outages(
        client, "dev-1",
        start="2026-09-22T01:00:00Z", end="2026-09-22T00:00:00Z",
    ).status_code == 422


# ---------- exclude_planned 缺报扣除 ----------

def test_exclude_planned_default_false_keeps_original_segments(client):
    seed_gap(client)
    register(client, "dev-1", "2026-09-22T00:01:00Z", "2026-09-22T00:03:00Z")
    # 不带参数 / false：计划停报不影响缺报结果
    expected = [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:03:00Z", "missing_windows": 2}
    ]
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z"
    ).json() == expected
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z",
        exclude_planned=False,
    ).json() == expected


def test_exclude_planned_removes_fully_covered_gap(client):
    seed_gap(client)
    register(client, "dev-1", "2026-09-22T00:01:00Z", "2026-09-22T00:03:00Z")
    r = missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z",
        exclude_planned=True,
    )
    assert r.status_code == 200, r.text
    assert r.json() == []


def test_exclude_planned_splits_gap_around_registration(client):
    """计划区间落在缺报段中间：扣除后剩余左右两段连续缺报。"""
    seed_gap(client)
    # 原始缺报段 [00:01, 00:03)；登记只覆盖 00:02 一个窗口
    register(client, "dev-1", "2026-09-22T00:02:00Z", "2026-09-22T00:03:00Z")
    r = missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z",
        exclude_planned=True,
    )
    assert r.json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:02:00Z", "missing_windows": 1}
    ]

    # 登记只覆盖第一个窗口
    register(client, "dev-1", "2026-09-22T00:00:30Z", "2026-09-22T00:02:00Z")  # 非法：未对齐
    assert register(
        client, "dev-1", "2026-09-22T00:01:00Z", "2026-09-22T00:02:00Z"
    ).status_code == 201
    # 此时 00:01 与 00:02 均被覆盖（两份登记在 00:02 端点相接）
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z",
        exclude_planned=True,
    ).json() == []


def test_exclude_planned_splits_into_two_segments(client):
    """一份缺报段被中间的计划区间切成两段（两侧都有剩余）。"""
    make_device(client)
    # 窗口：00:00 与 00:10；水位线 00:10:00，封存覆盖到 00:08
    ingest(client, "dev-1", "e1", "2026-09-22T00:00:10Z")
    ingest(client, "dev-1", "e2", "2026-09-22T00:10:00Z")
    # 原始缺报：[00:01, 00:09) 共 8 个窗口
    register(client, "dev-1", "2026-09-22T00:04:00Z", "2026-09-22T00:06:00Z",
             reason="rack power off")
    r = missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:12:00Z",
        exclude_planned=True,
    )
    assert r.json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:04:00Z", "missing_windows": 3},
        {"start": "2026-09-22T00:06:00Z", "end": "2026-09-22T00:09:00Z", "missing_windows": 3},
    ]


def test_overlapping_registrations_are_merged_and_deducted_once(client):
    """多份互相重叠的登记：合并后扣除，重叠窗口不重复扣减，窗口计数正确。"""
    make_device(client)
    ingest(client, "dev-1", "e1", "2026-09-22T00:00:10Z")
    ingest(client, "dev-1", "e2", "2026-09-22T00:10:00Z")
    # 原始缺报 [00:01, 00:09) 共 8 窗；两份登记重叠覆盖 00:03..00:06
    register(client, "dev-1", "2026-09-22T00:03:00Z", "2026-09-22T00:06:00Z")
    register(client, "dev-1", "2026-09-22T00:04:00Z", "2026-09-22T00:05:00Z")
    r = missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:12:00Z",
        exclude_planned=True,
    )
    assert r.json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:03:00Z", "missing_windows": 2},
        {"start": "2026-09-22T00:06:00Z", "end": "2026-09-22T00:09:00Z", "missing_windows": 3},
    ]


def test_touching_boundaries_are_not_overlap(client):
    """半开区间端点触碰不算重叠：计划 [00:03,00:04) 与缺报段 [00:01,00:03) 不相交。"""
    seed_gap(client)
    # 计划紧贴缺报段右端
    register(client, "dev-1", "2026-09-22T00:03:00Z", "2026-09-22T00:04:00Z")
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z",
        exclude_planned=True,
    ).json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:03:00Z", "missing_windows": 2}
    ]
    # 计划紧贴缺报段左端
    register(client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:01:00Z")
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z",
        exclude_planned=True,
    ).json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:03:00Z", "missing_windows": 2}
    ]


def test_planned_outage_outside_query_range_does_not_affect_result(client):
    """只扣除查询范围内重叠的计划：范围外（含相接）的登记不影响结果。"""
    seed_gap(client)
    # 查询 [00:00, 00:03)：登记在范围之后且与查询端点相接
    register(client, "dev-1", "2026-09-22T00:03:00Z", "2026-09-22T00:09:00Z")
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:03:00Z",
        exclude_planned=True,
    ).json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:03:00Z", "missing_windows": 2}
    ]


def test_planned_outage_extends_beyond_query_and_gap(client):
    """计划区间远大于缺报段/查询范围：缺报段被整体扣除，计划不产生"负窗口"。"""
    seed_gap(client)
    register(client, "dev-1", "2026-09-21T00:00:00Z", "2026-09-23T00:00:00Z")
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z",
        exclude_planned=True,
    ).json() == []


def test_planned_outage_clipped_to_query_half_open(client):
    """查询半开边界裁剪：计划只覆盖查询上界之外时不扣；恰好覆盖末窗时扣除。"""
    seed_gap(client)
    # 缺报 [00:01,00:03)；查询 end=00:02，计划 [00:02,00:04) 与查询范围端点相接
    register(client, "dev-1", "2026-09-22T00:02:00Z", "2026-09-22T00:04:00Z")
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:02:00Z",
        exclude_planned=True,
    ).json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:02:00Z", "missing_windows": 1}
    ]
    # 查询包含 00:02 后，该窗被扣除
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:03:00Z",
        exclude_planned=True,
    ).json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:02:00Z", "missing_windows": 1}
    ]


def test_unsealed_windows_are_not_deducted(client):
    """水位线尚未越过封存条件的窗口不在原始缺报段中，扣除后同样不会出现。"""
    make_device(client)
    ingest(client, "dev-1", "e1", "2026-09-22T00:00:10Z")
    ingest(client, "dev-1", "e2", "2026-09-22T00:03:10Z")
    # 登记覆盖 00:01..00:09（含尚未封存的 00:02 及以后）
    register(client, "dev-1", "2026-09-22T00:01:00Z", "2026-09-22T00:10:00Z")
    # 原始口径只报 [00:01,00:02)；被计划完全覆盖 => 空；未封存窗口绝不可能被"扣出来"
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T01:00:00Z",
        exclude_planned=True,
    ).json() == []
    # 登记只覆盖未封存区域时，已封存的缺报窗 00:01 仍须照常报出
    register(client, "dev-1", "2026-09-22T00:02:00Z", "2026-09-22T01:00:00Z")
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T01:00:00Z",
        exclude_planned=True,
    ).json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:02:00Z", "missing_windows": 1}
    ]


def test_exclude_planned_with_multiple_gap_segments(client):
    """多个原始缺报段与多份计划交错：逐段裁剪，窗口计数正确。"""
    make_device(client)
    # 窗口 00:00、00:03、00:04、00:10；水位线 00:10:00，封存覆盖到 00:08
    ingest(client, "dev-1", "e1", "2026-09-22T00:00:10Z")
    ingest(client, "dev-1", "e2", "2026-09-22T00:03:10Z")
    ingest(client, "dev-1", "e3", "2026-09-22T00:04:00Z")
    ingest(client, "dev-1", "e4", "2026-09-22T00:10:00Z")
    # 原始缺报：[00:01,00:03) 2 窗、[00:05,00:09) 4 窗
    register(client, "dev-1", "2026-09-22T00:02:00Z", "2026-09-22T00:06:00Z")
    r = missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:12:00Z",
        exclude_planned=True,
    )
    assert r.json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:02:00Z", "missing_windows": 1},
        {"start": "2026-09-22T00:06:00Z", "end": "2026-09-22T00:09:00Z", "missing_windows": 3},
    ]


def test_exclude_planned_after_late_event_fills_window(client):
    """迟到补报先缩短/拆分缺报段，再在剩余段上扣除计划区间。"""
    seed_gap(client)
    ingest(client, "dev-1", "e4", "2026-09-22T00:10:00Z")
    # 原始缺报：[00:01,00:03) 2 窗、[00:05,00:09) 4 窗
    register(client, "dev-1", "2026-09-22T00:06:00Z", "2026-09-22T00:08:00Z")
    r = missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:12:00Z",
        exclude_planned=True,
    )
    assert r.json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:03:00Z", "missing_windows": 2},
        {"start": "2026-09-22T00:05:00Z", "end": "2026-09-22T00:06:00Z", "missing_windows": 1},
        {"start": "2026-09-22T00:08:00Z", "end": "2026-09-22T00:09:00Z", "missing_windows": 1},
    ]

    # 迟到事件填入 00:06（计划区间起点窗）：该窗已非缺报，计划只继续扣除 00:07
    ingest(client, "dev-1", "e5", "2026-09-22T00:06:15Z", value=9.0)
    r = missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:12:00Z",
        exclude_planned=True,
    )
    assert r.json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:03:00Z", "missing_windows": 2},
        {"start": "2026-09-22T00:05:00Z", "end": "2026-09-22T00:06:00Z", "missing_windows": 1},
        {"start": "2026-09-22T00:08:00Z", "end": "2026-09-22T00:09:00Z", "missing_windows": 1},
    ]


def test_exclude_planned_is_isolated_per_device(client):
    seed_gap(client, "dev-1")
    seed_gap(client, "dev-2")
    register(client, "dev-1", "2026-09-22T00:01:00Z", "2026-09-22T00:03:00Z")
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z",
        exclude_planned=True,
    ).json() == []
    # dev-2 无任何登记，结果不变
    assert missing(
        client, "dev-2", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z",
        exclude_planned=True,
    ).json() == [
        {"start": "2026-09-22T00:01:00Z", "end": "2026-09-22T00:03:00Z", "missing_windows": 2}
    ]


def test_exclude_planned_empty_when_device_has_no_data(client):
    make_device(client)
    register(client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T01:00:00Z")
    assert missing(
        client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T02:00:00Z",
        exclude_planned=True,
    ).json() == []


def test_exclude_planned_still_validates_range(client):
    seed_gap(client)
    # exclude_planned 不改变原有 422/404 口径
    assert missing(
        client, "dev-1", "2026-09-22T01:00:00Z", "2026-09-22T00:00:00Z",
        exclude_planned=True,
    ).status_code == 422
    assert missing(
        client, "dev-1", "2026-09-22T00:00:30Z", "2026-09-22T01:00:00Z",
        exclude_planned=True,
    ).status_code == 422
    assert missing(
        client, "ghost", "2026-09-22T00:00:00Z", "2026-09-22T01:00:00Z",
        exclude_planned=True,
    ).status_code == 404


def test_exclude_planned_does_not_mutate_stored_data(client):
    """缺报查询（含扣除路径）纯只读：登记与窗口聚合均不被修改。"""
    seed_gap(client)
    register(client, "dev-1", "2026-09-22T00:01:00Z", "2026-09-22T00:03:00Z")
    before_outages = list_outages(client, "dev-1").json()
    before_windows = client.get("/devices/dev-1/windows").json()
    for _ in range(3):
        missing(
            client, "dev-1", "2026-09-22T00:00:00Z", "2026-09-22T00:10:00Z",
            exclude_planned=True,
        )
    assert list_outages(client, "dev-1").json() == before_outages
    assert client.get("/devices/dev-1/windows").json() == before_windows


def test_exclude_planned_large_span_uses_segment_arithmetic(client):
    """跨度极大的登记也只做段级运算：不逐窗遍历，结果与窗口计数仍正确。"""
    make_device(client, window_size_seconds=60)
    ingest(client, "dev-1", "e1", "2026-01-01T00:00:10Z")
    # 水位线推进到 30 天后：理论上中间有 ~43200 个空窗
    ingest(client, "dev-1", "e2", "2026-01-31T00:00:00Z")
    # 登记覆盖中间绝大部分，只留 [01-02, 01-03) 一天未登记
    register(client, "dev-1", "2026-01-01T00:01:00Z", "2026-01-02T00:00:00Z")
    register(client, "dev-1", "2026-01-03T00:00:00Z", "2026-01-31T00:00:00Z")
    r = missing(
        client, "dev-1", "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z",
        exclude_planned=True,
    )
    assert r.status_code == 200
    segments = r.json()
    assert segments == [
        {
            "start": "2026-01-02T00:00:00Z",
            "end": "2026-01-03T00:00:00Z",
            "missing_windows": 1440,
        }
    ]
