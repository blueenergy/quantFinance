"""热点题材树：树组装、事件去重、申万名还原、事件窗口过滤。"""
from datetime import datetime, timedelta

import pytest

from services import hot_topic_tree as htt


def _node(name, l1="电子", heat=10.0, limit_up=5, **extra):
    node = {
        "name": name,
        "l1": l1,
        "heat": heat,
        "limit_up_count": limit_up,
        "max_streak": 1,
        "broken_count": 0,
        "pct_change": None,
        "net_amount": None,
        "leader": None,
        "facts": [],
    }
    node.update(extra)
    return node


# ── 申万名还原 ────────────────────────────────────────────────────

@pytest.mark.unit
def test_resolve_l1_prefers_exact_over_prefix():
    """「半导体」必须命中申万 L2 半导体，不能落到 L3 半导体材料。"""
    exact = {
        "半导体": "电子",
        "半导体材料": "电子",
        "半导体设备": "电子",
        "光学光电子": "电子",
    }
    full, l1 = htt._resolve_l1("半导体", exact)
    assert full == "半导体"
    assert l1 == "电子"


@pytest.mark.unit
def test_resolve_l1_falls_back_to_prefix_for_truncated_names():
    """涨停天梯的 sector 是 4 字截断的，需要前缀还原。"""
    exact = {"光学光电子": "电子", "计算机设备": "计算机"}
    full, l1 = htt._resolve_l1("光学光电", exact)
    assert full == "光学光电子"
    assert l1 == "电子"

    full2, l12 = htt._resolve_l1("计算机设", exact)
    assert full2 == "计算机设备"
    assert l12 == "计算机"


@pytest.mark.unit
def test_resolve_l1_unknown_returns_empty_l1():
    full, l1 = htt._resolve_l1("不存在的板块", {})
    assert full == "不存在的板块"
    assert l1 == ""


# ── 事件去重 ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_attach_events_is_globally_deduplicated():
    """一条事件只挂给热度最高的叶子，不能在多个叶子上重复出现。"""
    leaves = [
        _node("半导体", heat=30.0),
        _node("汽车零部件", l1="汽车", heat=20.0),
    ]
    events = [{"date": "12-01", "title": "黄仁勋12月1日出席美国国会听证会", "importance": 4}]

    htt._attach_events(leaves, {}, events)

    assert len(leaves[0]["events"]) == 1
    assert leaves[0]["events"][0]["title"].startswith("黄仁勋")
    # 第二个叶子热度低，事件已被第一个叶子占用
    assert leaves[1]["events"] == []


@pytest.mark.unit
def test_attach_events_uses_branch_alias_when_leaf_has_none():
    """叶子没命中别名时，用父层别名兜底。"""
    leaves = [_node("某个新概念", l1="国防军工", heat=10.0)]
    events = [{"date": "12-07", "title": "第十六届中国航展将于12月7日至13日举办", "importance": 3}]

    htt._attach_events(leaves, {}, events)

    assert len(leaves[0]["events"]) == 1


@pytest.mark.unit
def test_attach_events_respects_max_per_leaf():
    leaves = [_node("半导体", heat=30.0)]
    events = [
        {"date": "12-01", "title": "黄仁勋12月1日出席美国国会听证会", "importance": 4},
        {"date": "12-10", "title": "台积电公布11月销售额", "importance": 3},
        {"date": "12-06", "title": "摩尔线程(688795.SH) 限售解禁", "importance": 4},
    ]

    htt._attach_events(leaves, {}, events, max_events_per_leaf=2)

    assert len(leaves[0]["events"]) == 2


# ── 树组装 ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_assemble_limits_leaves_per_branch_and_branch_count():
    nodes = []
    for i in range(5):
        nodes.append(_node(f"电子概念{i}", l1="电子", heat=50.0 - i))
    for i in range(5):
        nodes.append(_node(f"计算机概念{i}", l1="计算机", heat=40.0 - i))
    for i in range(5):
        nodes.append(_node(f"军工概念{i}", l1="国防军工", heat=30.0 - i))
    # 分支数超过上限时必须被裁掉（这里给 8 个分支 > MAX_BRANCHES）
    for l1, base in (("电力设备", 20.0), ("有色金属", 19.0), ("通信", 18.0), ("汽车", 17.0)):
        for i in range(4):
            nodes.append(_node(f"{l1}概念{i}", l1=l1, heat=base - i))

    tree = htt._assemble(nodes, [], {}, {"mode": "intraday", "trade_date": "20261127"})

    assert len(tree["branches"]) == htt.MAX_BRANCHES
    # 热度最高的分支必须保留
    assert tree["branches"][0]["name"] == "电子"
    for branch in tree["branches"]:
        assert len(branch["children"]) <= htt.MAX_LEAVES_PER_BRANCH


@pytest.mark.unit
def test_assemble_orders_branches_by_heat():
    nodes = [
        _node("冷门", l1="环保", heat=1.0),
        _node("热门", l1="电子", heat=99.0),
    ]
    tree = htt._assemble(nodes, [], {}, {})
    assert tree["branches"][0]["name"] == "电子"


@pytest.mark.unit
def test_assemble_keeps_l1_even_when_missing():
    tree = htt._assemble([_node("未知", l1="", heat=1.0)], [], {}, {})
    assert tree["branches"][0]["name"] == "未分类"


# ── 事件窗口 ──────────────────────────────────────────────────────

@pytest.mark.unit
def test_event_date_str_handles_datetime_and_string():
    assert htt._event_date_str(datetime(2026, 12, 1, 9, 30)) == "12-01"
    assert htt._event_date_str("2026-12-01 16:00:00") == "12-01"
    assert htt._event_date_str("20261201") == "12-01"


@pytest.mark.unit
def test_event_sort_date_handles_both_types():
    assert htt._event_sort_date(datetime(2026, 12, 1)) == "2026-12-01"
    assert htt._event_sort_date("2026-12-01 16:00:00") == "2026-12-01"
    assert htt._event_sort_date("20261201") == "2026-12-01"


# ── 数值健壮性 ────────────────────────────────────────────────────

@pytest.mark.unit
def test_num_handles_nan_and_none():
    assert htt._num(None) == 0.0
    assert htt._num("abc") == 0.0
    assert htt._num(float("nan")) == 0.0
    assert htt._num(float("inf")) == 0.0
    assert htt._num("3.5") == 3.5


@pytest.mark.unit
def test_int_handles_bad_input():
    assert htt._int(None) == 0
    assert htt._int("7") == 7
    assert htt._int(float("nan")) == 0


# ── 技术性概念过滤 ────────────────────────────────────────────────

@pytest.mark.unit
def test_technical_concepts_are_filtered():
    """技术性/统计性板块不是题材，留着会占满热度榜。"""
    for name in ("昨日连板", "历史新高", "破增发价股", "融资融券", "HS300_", "大盘股"):
        assert any(p in name for p in htt.TECH_CONCEPT_PAT), name

    for name in ("半导体概念", "固态电池", "充电桩", "商业航天", "钙钛矿电池"):
        assert not any(p in name for p in htt.TECH_CONCEPT_PAT), name
