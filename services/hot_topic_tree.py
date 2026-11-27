"""
热点题材树：把当日市场结构组织成「父层(申万一级) → 叶子(题材) → 催化事件」的树。

两种口径，同一套输出契约（``branches[]``），前端只认契约不认口径：

============  ==========================  ==========================  ================
口径          父层                        叶子                        数据源
============  ==========================  ==========================  ================
``intraday``  申万一级（由涨停聚类反查）   申万二级/三级（涨停聚类）    本地 Mongo，实时算
``eod``       申万一级（概念成分股众数）   东财概念板块                 Tushare，收盘预生成
============  ==========================  ==========================  ================

设计约束：
- **API 路径永不调用 Tushare**。EOD 树由定时任务预生成落库，接口只读；
  EOD 缺失时降级到 intraday 并在响应里标明降级原因。
- 热度分是**描述性**的（今天哪块在动），不是预测信号；权重未经回测，见
  ``docs`` 注释。
- 催化事件按**全局去重**：一条事件只挂给热度最高的那个叶子，避免「黄仁勋听证会」
  在多个叶子上重复出现。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("quantFinance.hot_topic_tree")

# ── 树形状参数 ────────────────────────────────────────────────────
MAX_BRANCHES = 7            # 最多几个申万一级分支
MAX_LEAVES_PER_BRANCH = 2   # 每个分支最多几个叶子
MAX_EOD_CANDIDATES = 36     # EOD 口径要拉成分股的概念数上限（每个概念一次 Tushare 调用）
LEAF_CONTAINMENT_THRESHOLD = 0.5   # 叶子去重：被已选概念覆盖超过这个比例就跳过

# ── 热度权重（描述性，非预测；未回测，改动前先想清楚）──────────────
HEAT_LIMIT_UP = 2.0     # 每只涨停
HEAT_PCT = 0.8          # 板块涨幅每 1 个百分点
HEAT_NET = 0.35         # 资金净流入每 1 亿
HEAT_STREAK = 1.2       # 每 1 连板
HEAT_BREADTH = 0.03     # 涨跌家数差每 1 家


def _num(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
        if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
            return default
        return f
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    return int(_num(value, default))


# ── 技术性/统计性概念过滤（东财概念里混着大量这类）──────────────────
# 「昨日连板」「历史新高」「破增发价股」「融资融券」「HS300_」这类不是题材，
# 留着会把热度榜前排全占掉。
TECH_CONCEPT_PAT = (
    "昨日", "历史新", "破增发", "破净", "破发", "融资融券", "含一字", "一字",
    "中证", "上证", "深证", "沪深", "创业板", "科创", "北证", "样本股", "成分股",
    "MSCI", "标普", "富时", "沪股通", "深股通", "转债", "次新", "ST", "预盈",
    "HS300", "恒生", "上证50", "中证500", "深成500", "指数",
    # 东财把指数/基金类板块也放在「概念板块」里，靠名字剔除（数量有限、可枚举）。
    # 别改成按当日涨跌家数设阈值——那样会在普涨日误杀真题材。
    "普尔", "茅指数", "宁组合", "央企", "行业龙头",
    # 东财把「长江三角」这类地域指数也归在概念里（moneyflow 的 content_type
    # 只标了「XX板块」那批），只能按名字剔除。
    "三角", "京津冀", "环渤海", "成渝", "中原",
    # 市值/风格类
    "小盘", "中盘", "微盘",
    "预亏", "预增", "预减", "业绩预", "高送转", "举牌", "股权", "回购", "增持",
    "减持", "解禁", "创新高", "创新低", "尾盘", "早盘", "盘中", "开盘", "主力",
    "游资", "重仓", "基金", "社保", "QFII", "汇金", "证金", "券商金股", "保险新进",
    "北向", "低价", "高价", "超跌", "AH股", "B股", "GDR", "参股", "要约", "摘牌",
    "退市", "微盘", "新股", "IPO", "独角兽", "同花顺", "问财", "百元", "摘帽",
    "预热", "大盘", "权重", "风格", "龙头股", "红利", "周期股", "高成长", "指数",
    "亏损", "摘星", "股权激励", "分拆", "借壳", "贬值", "仙股", "东方财富", "板块",
    "题材", "概念股", "贬值受益",
)

# ── 题材 → 事件关键词（人工别名表）────────────────────────────────
# 只列「同一件事」的显式映射；匹配不上的叶子退回盘面事实。
EVENT_ALIAS: Dict[str, List[str]] = {
    "通信技术": ["AI对话", "人工智能", "黄仁勋", "台积电"],
    "5G概念": ["AI对话", "人工智能", "黄仁勋", "6G"],
    "CPO概念": ["黄仁勋", "台积电", "AI对话"],
    "光通信模块": ["黄仁勋", "台积电", "AI对话"],
    "PCB": ["黄仁勋", "台积电", "摩尔线程"],
    "智算中心（AIDC）": ["AI对话", "黄仁勋", "数据中心"],
    "数据中心": ["AI对话", "黄仁勋", "台积电"],
    "算力概念": ["AI对话", "黄仁勋", "算力"],
    "算力租赁": ["AI对话", "黄仁勋"],
    "边缘计算": ["AI对话"],
    "华为概念": ["AI对话", "黄仁勋"],
    "华为昇腾": ["AI对话", "黄仁勋", "昇腾"],
    "英伟达概念": ["黄仁勋", "AI对话"],
    "商业航天": ["航展", "航天"],
    "卫星互联网": ["航展", "卫星", "航天"],
    "空间站概念": ["航展", "航天"],
    "央国企改革": ["证监会", "对外投资", "自贸区", "发改委"],
    "黄金概念": ["PMI", "工业企业利润"],
    "金属铜": ["PMI", "工业企业利润"],
    "金属镍": ["PMI", "工业企业利润"],
    "煤化工概念": ["工业企业利润", "PMI", "OPEC"],
    "钙钛矿电池": ["多晶硅", "光伏", "PMI"],
    "农业种植": ["农产品", "PMI", "自贸区"],
    "预制菜概念": ["农产品", "自贸区"],
    "海洋经济": ["OPEC", "自贸区"],
    "节能环保": ["PMI", "工业企业利润"],
    "汽车电子": ["特斯拉", "机器人", "PICO"],
    "智能驾驶": ["特斯拉", "机器人", "小米"],
    "水利建设": ["水利", "发改委", "对外投资"],
    "半导体": ["AI对话", "黄仁勋", "台积电", "摩尔线程"],
    "IT服务Ⅱ": ["AI对话", "黄仁勋"],
    "软件开发": ["AI对话", "黄仁勋"],
    "计算机设备": ["AI对话", "黄仁勋", "台积电"],
    "通信设备": ["AI对话", "黄仁勋", "台积电"],
    "消费电子": ["PICO", "AI对话", "机器人"],
    "光学光电子": ["PICO", "台积电"],
    "元件": ["AI对话", "台积电", "黄仁勋"],
    "汽车零部件": ["特斯拉", "机器人", "Optimus", "小米"],
    "通用设备": ["机器人", "特斯拉", "Optimus"],
    "自动化设备": ["机器人", "特斯拉", "Optimus"],
    "专用设备": ["机器人", "特斯拉", "水利", "发改委"],
    "军工电子Ⅱ": ["航展", "航天"],
    "化学原料": ["工业企业利润", "PMI", "OPEC"],
    "化学制品": ["工业企业利润", "PMI"],
    "工业金属": ["PMI", "工业企业利润", "OPEC"],
    "贵金属": ["PMI", "工业企业利润"],
    "光伏设备": ["多晶硅", "光伏", "PMI"],
    "电子化学品": ["台积电", "摩尔线程"],
    # 东财概念名（EOD 口径的叶子）——比父层兜底精确，避免张冠李戴
    "半导体概念": ["AI对话", "黄仁勋", "台积电", "摩尔线程"],
    "华为概念": ["AI对话", "黄仁勋", "昇腾", "WAIAC", "人工智能"],
    "人工智能": ["AI对话", "WAIAC", "人工智能", "黄仁勋"],
    "光通信模块": ["黄仁勋", "台积电", "AI对话"],
    "专精特新": ["机器人", "特斯拉", "Optimus", "制造业"],
    "机器人概念": ["机器人", "特斯拉", "Optimus"],
    "新能源车": ["特斯拉", "小米", "机器人"],
    "电池技术": ["PMI", "工业企业利润", "固态电池"],
    "新材料": ["PMI", "工业企业利润", "半导体"],
    "商业航天": ["航展", "航天"],
    "CPO概念": ["黄仁勋", "台积电", "AI对话"],
    "固态电池": ["PMI", "工业企业利润"],
    "充电桩": ["PMI", "工业企业利润", "充电"],
}

# 父层兜底别名（叶子没命中时用父层的）
BRANCH_EVENT_ALIAS: Dict[str, List[str]] = {
    "电子": ["AI对话", "人工智能", "黄仁勋", "台积电", "摩尔线程", "PICO", "机器人"],
    "计算机": ["AI对话", "人工智能", "黄仁勋", "算力", "数据中心"],
    "通信": ["AI对话", "黄仁勋", "台积电"],
    "国防军工": ["航展", "航天", "机器人"],
    "机械设备": ["机器人", "特斯拉", "Optimus", "PICO"],
    "汽车": ["特斯拉", "机器人", "Optimus", "小米"],
    "电力设备": ["多晶硅", "光伏", "PMI"],
    "有色金属": ["PMI", "工业企业利润", "OPEC"],
    "基础化工": ["工业企业利润", "PMI", "OPEC"],
    "石油石化": ["OPEC", "PMI"],
    "煤炭": ["OPEC", "PMI"],
    "农林牧渔": ["农产品", "自贸区", "PMI"],
    "传媒": ["AI对话", "PICO"],
    "环保": ["PMI", "工业企业利润"],
    "家用电器": ["机器人", "特斯拉"],
    "轻工制造": ["机器人", "PICO"],
    "医药生物": ["PMI", "工业企业利润"],
    "食品饮料": ["PMI", "工业企业利润"],
    "非银金融": ["证监会", "发改委", "对外投资", "自贸区"],
    "银行": ["PMI", "工业企业利润"],
    "建筑装饰": ["证监会", "发改委", "对外投资"],
    "商贸零售": ["自贸区", "PMI"],
    "公用事业": ["OPEC", "PMI"],
    "交通运输": ["OPEC", "自贸区"],
}


# ── 通用：树组装 ───────────────────────────────────────────────────

def _attach_events(
    leaves: List[Dict[str, Any]],
    branches_by_l1: Dict[str, List[Dict[str, Any]]],
    events: List[Dict[str, Any]],
    max_events_per_leaf: int = 2,
) -> None:
    """给叶子挂催化事件，**全局去重**：一条事件只挂给热度最高的那个叶子。

    ``leaves`` 必须是按热度降序排列的（先到先得）。
    """
    used: set = set()
    for leaf in leaves:
        keys = EVENT_ALIAS.get(leaf["name"]) or BRANCH_EVENT_ALIAS.get(leaf.get("l1") or "")
        if not keys:
            continue
        hits = []
        seen_here: set = set()
        for ev in events:
            title = ev.get("title") or ""
            if not title or title in used or title in seen_here:
                continue
            if any(k and k in title for k in keys):
                hits.append(ev)
                seen_here.add(title)
        hits.sort(key=lambda e: (e.get("date") or "", -_int(e.get("importance"))))
        picked = hits[:max_events_per_leaf]
        for ev in picked:
            used.add(ev.get("title") or "")
        leaf["events"] = [
            {"date": e.get("date") or "", "title": e.get("title") or ""} for e in picked
        ]


def _assemble(
    nodes: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    stats: Dict[str, Any],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    """把扁平叶子列表组装成 父层 -> 叶子 的树，并挂事件。

    ``nodes`` 已按热度降序；每支最多 ``MAX_LEAVES_PER_BRANCH`` 个叶子，
    最多 ``MAX_BRANCHES`` 个分支。
    """
    by_l1: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        l1 = node.get("l1") or "未分类"
        if len(by_l1[l1]) >= MAX_LEAVES_PER_BRANCH:
            continue
        by_l1[l1].append(node)

    # 分支顺序 = 该分支下热度最高的叶子的排名
    order = sorted(
        by_l1.items(),
        key=lambda kv: max(_num(n.get("heat")) for n in kv[1]),
        reverse=True,
    )[:MAX_BRANCHES]

    flat_leaves: List[Dict[str, Any]] = []
    for _, items in order:
        flat_leaves.extend(items)
    _attach_events(flat_leaves, by_l1, events)

    branches = []
    for l1, items in order:
        branches.append({
            "name": l1,
            "heat": round(max(_num(n.get("heat")) for n in items), 2),
            "children": [
                {
                    "name": n["name"],
                    "heat": round(_num(n.get("heat")), 2),
                    "limit_up_count": _int(n.get("limit_up_count")),
                    "max_streak": _int(n.get("max_streak")),
                    "broken_count": _int(n.get("broken_count")),
                    "pct_change": (round(_num(n["pct_change"]), 2) if n.get("pct_change") is not None else None),
                    "net_amount": (round(_num(n["net_amount"]), 2) if n.get("net_amount") is not None else None),
                    "leader": n.get("leader"),
                    "events": n.get("events") or [],
                    "facts": n.get("facts") or [],
                }
                for n in items
            ],
        })

    return {
        "branches": branches,
        "stats": stats,
        "meta": meta,
    }


# ── 盘中口径 ───────────────────────────────────────────────────────

_SW_MEMBER_CACHE: Dict[str, Any] = {"at": 0.0, "map": {}}
_SW_CACHE_TTL = 3600.0


def _sw_sector_to_l1() -> Dict[str, str]:
    """申万行业名(可能被截断到 4 字) -> 申万一级行业名。

    数据源 ``quant_data.sw_index_member``（每只股票的 l1/l2/l3 名）。
    涨停天梯的 sector 字段是 4 字截断的申万名，用前缀匹配还原。
    """
    now = time.time()
    if now - _SW_MEMBER_CACHE["at"] < _SW_CACHE_TTL and _SW_MEMBER_CACHE["map"]:
        return _SW_MEMBER_CACHE["map"]

    from db import get_data_db

    mapping: Dict[str, Counter] = defaultdict(Counter)
    projection = {"_id": 0, "l1_name": 1, "l2_name": 1, "l3_name": 1}
    for doc in get_data_db()["sw_index_member"].find({}, projection):
        l1 = doc.get("l1_name") or ""
        if not l1:
            continue
        for key in (doc.get("l2_name"), doc.get("l3_name")):
            if key:
                mapping[key][l1] += 1

    result: Dict[str, str] = {}
    for name, counter in mapping.items():
        result[name] = counter.most_common(1)[0][0]
    _SW_MEMBER_CACHE["at"] = now
    _SW_MEMBER_CACHE["map"] = result
    return result


def _resolve_l1(sector: str, exact: Dict[str, str]) -> Tuple[str, str]:
    """截断名 -> (全名, 申万一级)。精确命中优先，否则前缀匹配。

    「半导体」应命中申万 L2「半导体」，而不是 L3「半导体材料」。
    """
    if sector in exact:
        return sector, exact[sector]
    prefix_hits = sorted(
        (name for name in exact if name.startswith(sector)),
        key=len,
        reverse=True,
    )
    if prefix_hits:
        full = prefix_hits[0]
        return full, exact[full]
    return sector, ""


def build_intraday_tree(trade_date: str) -> Optional[Dict[str, Any]]:
    """盘中口径：全部来自 quant_data 本地集合，无需外部调用。

    叶子 = 涨停聚类出的申万行业；热度 = 涨停家数 + 最高连板 + 炸板 + 涨跌广度。
    """
    from db import get_data_db

    data = get_data_db()
    ladder = data["limit_up_ladder"].find_one({"date": trade_date})
    if not ladder:
        return None

    indicators = ladder.get("indicators") or {}
    ranking = ladder.get("sector_ranking") or []
    if not ranking:
        return None

    # 涨停/炸板明细
    limit_docs = list(data["daily_limit_up"].find({"date": trade_date}))
    by_sector: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    broken_by_sector: Counter = Counter()
    for doc in limit_docs:
        sector = doc.get("limit_reason") or doc.get("primary_sector") or ""
        status = doc.get("status") or doc.get("limit_up_status") or ""
        if status == "broken":
            broken_by_sector[sector] += 1
        else:
            by_sector[sector].append(doc)

    exact = _sw_sector_to_l1()

    nodes: List[Dict[str, Any]] = []
    for row in ranking:
        sector = row.get("sector") or ""
        if not sector:
            continue
        full_name, l1 = _resolve_l1(sector, exact)
        stocks = by_sector.get(sector, [])
        max_streak = max([_int(s.get("streak")) for s in stocks] or [0])
        count = _int(row.get("count"), len(stocks))
        leader = None
        if stocks:
            lead = sorted(
                stocks,
                key=lambda s: (-_int(s.get("streak")), -_num(s.get("seal_amount"))),
            )[0]
            leader = {
                "name": lead.get("name") or "",
                "symbol": lead.get("symbol") or "",
                "streak": _int(lead.get("streak")),
                "seal_amount": round(_num(lead.get("seal_amount")) / 1e8, 2),
                "limit_up_time": lead.get("limit_up_time") or "",
            }
        broken = broken_by_sector.get(sector, 0)
        heat = (
            count * HEAT_LIMIT_UP
            + max_streak * HEAT_STREAK
            - broken * 0.5
        )
        nodes.append({
            "name": full_name,
            "l1": l1 or "未分类",
            "heat": heat,
            "limit_up_count": count,
            "max_streak": max_streak,
            "broken_count": broken,
            "pct_change": None,
            "net_amount": None,
            "leader": leader,
            "facts": [
                f"{count}家涨停·最高{max(max_streak, 1)}板",
                (f"领涨 {leader['name']}（{leader['streak']}板 封单{leader['seal_amount']:.2f}亿）" if leader else ""),
                (f"炸板{broken}家" if broken else ""),
            ],
        })

    nodes.sort(key=lambda n: -n["heat"])
    stats = {
        "total_limit_up": _int(indicators.get("total_limit_up")),
        "max_streak": _int(indicators.get("max_streak")),
        "sentiment_score": _int(indicators.get("sentiment_score")),
        "promotion_rate_1to2": indicators.get("promotion_rate_1to2"),
        "high_tier_count": _int(indicators.get("high_tier_count")),
    }
    meta = {
        "mode": "intraday",
        "trade_date": trade_date,
        "data_source": "quant_data.limit_up_ladder / daily_limit_up / sw_index_member",
        "note": "盘中口径，申万行业聚类，随行情滚动变化",
    }
    return _assemble(nodes, load_events(trade_date), stats, meta)


# ── 收盘口径（Tushare）─────────────────────────────────────────────

class _RateLimiter:
    def __init__(self, per_sec: float = 3.0):
        self.interval = 1.0 / max(per_sec, 0.1)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.time()
            delta = now - self._last
            if delta < self.interval:
                time.sleep(self.interval - delta)
            self._last = time.time()


def _get_pro():
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        return None
    try:
        import tushare as ts
        ts.set_token(token)
        return ts.pro_api()
    except Exception as exc:  # pragma: no cover - 环境问题
        log.warning("[HotTopicTree] tushare 初始化失败: %s", exc)
        return None


def build_eod_tree(trade_date: str) -> Optional[Dict[str, Any]]:
    """收盘口径：东财概念板块 + 资金流 + 涨停，按概念成分股归到申万一级。

    **只在定时任务里调用**（30+ 次 Tushare 请求，耗时几十秒）。
    """
    pro = _get_pro()
    if pro is None:
        return None

    limiter = _RateLimiter(3.0)
    try:
        idx_df = pro.dc_index(trade_date=trade_date, idx_type="概念板块")
        limiter.wait()
        if idx_df is None or idx_df.empty:
            log.info("[HotTopicTree] dc_index 无 %s 数据", trade_date)
            return None
        mf_df = pro.moneyflow_ind_dc(trade_date=trade_date)
        limiter.wait()
        lu_df = pro.limit_list_d(trade_date=trade_date)
        limiter.wait()
        member_df = pro.index_member_all()
        limiter.wait()
    except Exception as exc:
        log.warning("[HotTopicTree] Tushare 拉取失败: %s", exc)
        return None

    sw_of: Dict[str, str] = {}
    for _, row in member_df.iterrows():
        sw_of[row["ts_code"]] = row.get("l1_name") or ""

    limit_up: Dict[str, Dict[str, Any]] = {}
    for _, row in lu_df.iterrows():
        limit_up[row["ts_code"]] = {
            "name": row.get("name") or "",
            "industry": row.get("industry") or "",
            "fd_amount": _num(row.get("fd_amount")) / 1e8,
            "open_times": _int(row.get("open_times")),
            "limit_times": _int(row.get("limit_times")),
            "first_time": str(row.get("first_time") or ""),
            "last_time": str(row.get("last_time") or ""),
            "turnover_ratio": _num(row.get("turnover_ratio")),
        }

    mf_of: Dict[str, Dict[str, float]] = {}
    regional_codes: set = set()
    if mf_df is not None and not mf_df.empty:
        for _, row in mf_df.iterrows():
            code = row["ts_code"]
            mf_of[code] = {
                "net_amount": _num(row.get("net_amount")) / 1e8,
                "net_rate": _num(row.get("net_amount_rate")),
            }
            # 地域板块（长江三角/广东板块…）不是题材，靠 moneyflow 的 content_type 精确剔除
            if (row.get("content_type") or "") == "地域":
                regional_codes.add(code)

    concepts: List[Dict[str, Any]] = []
    for _, row in idx_df.iterrows():
        name = row.get("name") or ""
        code = row["ts_code"]
        if code in regional_codes:
            continue
        if any(p in name for p in TECH_CONCEPT_PAT):
            continue
        flow = mf_of.get(code, {})
        pct = _num(row.get("pct_change"))
        up_num = _int(row.get("up_num"))
        down_num = _int(row.get("down_num"))
        breadth = up_num - down_num
        net = flow.get("net_amount", 0.0)
        concepts.append({
            "code": code,
            "name": name,
            "pct_change": pct,
            "net_amount": net,
            "limit_up_count": 0,
            "up_num": up_num,
            "down_num": down_num,
            "breadth": breadth,
            "max_streak": 0,
            "leader": None,
            "l1": "",
            # 初筛：涨幅 + 广度 + 资金
            "heat_pre": pct + breadth * 0.06 + (net if net > 0 else net * 0.3),
        })
    concepts.sort(key=lambda c: -c["heat_pre"])

    for concept in concepts[:MAX_EOD_CANDIDATES]:
        try:
            mem_df = pro.dc_member(ts_code=concept["code"], trade_date=trade_date)
            limiter.wait()
        except Exception as exc:
            log.warning("[HotTopicTree] dc_member(%s) 失败: %s", concept["name"], exc)
            continue
        members = list(mem_df["con_code"]) if (mem_df is not None and not mem_df.empty) else []
        hits = [s for s in members if s in limit_up]
        concept["member_count"] = len(members)
        concept["limit_up_count"] = len(hits)
        concept["hit_symbols"] = hits
        l1_counter = Counter(sw_of.get(s, "") for s in members if sw_of.get(s))
        concept["l1"] = l1_counter.most_common(1)[0][0] if l1_counter else ""
        concept["max_streak"] = max([limit_up[s]["limit_times"] for s in hits] or [0])
        if hits:
            lead = sorted(
                hits,
                key=lambda s: (-limit_up[s]["limit_times"], -limit_up[s]["fd_amount"]),
            )[0]
            concept["leader"] = {
                "name": limit_up[lead]["name"],
                "symbol": lead,
                "streak": limit_up[lead]["limit_times"],
                "seal_amount": round(limit_up[lead]["fd_amount"], 2),
                "limit_up_time": limit_up[lead]["first_time"],
            }

    # 叶子去重：东财概念高度重叠（"先进封装" 73% 的涨停股都在"高带宽内存"里、
    # "CPO概念" 60% 在"光通信模块"里）。判据是**两两包含度**：如果某个已选概念
    # 已经覆盖了本概念 50% 以上的涨停股，说明本概念没有新信息，跳过。
    #
    # 不用「并集」做分母——普涨日大概念（如华为概念 135 只）会把全市场涨停覆盖掉，
    # 并集口径会把所有其他主线误杀。
    picked_codes: List[Dict[str, Any]] = []
    picked_sets: List[set] = []
    for concept in concepts[:MAX_EOD_CANDIDATES]:
        if concept["limit_up_count"] <= 0:
            continue
        hits = set(concept["hit_symbols"])
        if not hits:
            continue
        redundant = any(
            len(hits & picked) / len(hits) > LEAF_CONTAINMENT_THRESHOLD
            for picked in picked_sets
        )
        if redundant:
            continue
        picked_codes.append(concept)
        picked_sets.append(hits)

    nodes: List[Dict[str, Any]] = []
    for concept in picked_codes:
        leader = concept["leader"]
        heat = (
            concept["limit_up_count"] * HEAT_LIMIT_UP
            + concept["pct_change"] * HEAT_PCT
            + max(concept["net_amount"], 0) * HEAT_NET
            + concept["max_streak"] * HEAT_STREAK
            + concept["breadth"] * HEAT_BREADTH
        )
        nodes.append({
            "name": concept["name"],
            "l1": concept["l1"] or "未分类",
            "heat": heat,
            "limit_up_count": concept["limit_up_count"],
            "max_streak": concept["max_streak"],
            "broken_count": 0,
            "pct_change": concept["pct_change"],
            "net_amount": concept["net_amount"],
            "leader": leader,
            "facts": [
                f"{concept['limit_up_count']}家涨停·最高{max(concept['max_streak'], 1)}板",
                (f"领涨 {leader['name']}（{leader['streak']}板 封单{leader['seal_amount']:.2f}亿）" if leader else ""),
                f"板块{concept['pct_change']:+.1f}%·净流入{concept['net_amount']:+.0f}亿",
            ],
        })
    nodes.sort(key=lambda n: -n["heat"])

    stats = {
        "total_limit_up": len(limit_up),
        "max_streak": max([_int(v["limit_times"]) for v in limit_up.values()] or [0]),
    }
    meta = {
        "mode": "eod",
        "trade_date": trade_date,
        "data_source": "Tushare dc_index / dc_member / moneyflow_ind_dc / limit_list_d",
        "note": "收盘定稿口径，东方财富概念板块",
    }
    return _assemble(nodes, load_events(trade_date), stats, meta)


# ── 事件 ───────────────────────────────────────────────────────────

def _event_date_str(value: Any) -> str:
    """事件时间 -> ``MM-DD``。``event_time`` 存的是 datetime，也可能是字符串。"""
    if isinstance(value, datetime):
        return value.strftime("%m-%d")
    text = str(value or "")
    if len(text) >= 10 and text[4] == "-":
        return text[5:10]
    if len(text) >= 8 and text.isdigit():
        return f"{text[4:6]}-{text[6:8]}"
    return text[:10]


def _event_sort_date(value: Any) -> str:
    """事件时间 -> ``YYYY-MM-DD``，用于排序与窗口过滤。"""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    text = str(value or "")
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    if len(text) >= 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return ""


def load_events(trade_date: str) -> List[Dict[str, Any]]:
    """当日附近的催化事件：市场事件日历（CN）+ 收盘分析 event_watch。

    窗口 = 交易日前 3 天 ~ 后 14 天（与「本周 + 未来两周」的日历口径一致）。
    事件时间在库里是 **datetime**，字符串操作数做不了类型匹配，
    所以这里取全量再在 Python 里过滤，避免 ``$gte`` 打空。
    """
    events: List[Dict[str, Any]] = []
    try:
        from db import get_data_db, get_analyzer_db

        try:
            base = datetime.strptime(trade_date, "%Y%m%d").date()
        except (TypeError, ValueError):
            base = datetime.now().date()
        window_start = (base - timedelta(days=3)).strftime("%Y-%m-%d")
        window_end = (base + timedelta(days=14)).strftime("%Y-%m-%d")

        data = get_data_db()
        for doc in data["market_events"].find(
            {"importance": {"$gte": 3}, "region": {"$in": ["CN", "中国"]}}
        ):
            day = _event_sort_date(doc.get("event_time"))
            if not day or not (window_start <= day <= window_end):
                continue
            events.append({
                "date": _event_date_str(doc.get("event_time")),
                "title": doc.get("title") or "",
                "importance": _int(doc.get("importance")),
            })

        analysis_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
        doc = get_analyzer_db()["market_analysis_cache"].find_one(
            {"analysis_date": analysis_date}, sort=[("created_at", -1)]
        )
        if doc:
            for raw in (doc.get("analysis_result") or {}).get("event_watch") or []:
                date_part, _, rest = str(raw).partition(" ")
                title = rest.split("｜")[0].strip()
                if title:
                    events.append({"date": date_part, "title": title, "importance": 3})
    except Exception as exc:
        log.warning("[HotTopicTree] 事件加载失败: %s", exc)

    events.sort(key=lambda e: (e.get("date") or "", -_int(e.get("importance"))))
    return events


# ── 存取 ───────────────────────────────────────────────────────────

def collection():
    from db import get_db_collections

    return get_db_collections()["hot_topic_tree"]


def save_tree(tree: Dict[str, Any]) -> None:
    """落库。intraday 按快照时刻存多份，eod 每天一份。"""
    if not tree:
        return
    meta = tree.get("meta") or {}
    mode = meta.get("mode") or "intraday"
    trade_date = meta.get("trade_date") or ""
    now = datetime.now()
    doc = {
        "mode": mode,
        "trade_date": trade_date,
        "generated_at": now,
        "branches": tree.get("branches") or [],
        "stats": tree.get("stats") or {},
        "meta": meta,
    }
    if mode == "eod":
        collection().replace_one(
            {"mode": "eod", "trade_date": trade_date}, doc, upsert=True
        )
    else:
        # 同一天同一分钟只留一份（重试/重复触发时覆盖）
        collection().replace_one(
            {
                "mode": "intraday",
                "trade_date": trade_date,
                "snapshot_minute": now.strftime("%H%M"),
            },
            {**doc, "snapshot_minute": now.strftime("%H%M")},
            upsert=True,
        )
    log.info(
        "[HotTopicTree] 已保存 %s %s（%d 分支）",
        mode, trade_date, len(doc["branches"]),
    )


def load_tree(mode: str, trade_date: str) -> Optional[Dict[str, Any]]:
    query = {"mode": mode, "trade_date": trade_date}
    doc = collection().find_one(query, sort=[("generated_at", -1)])
    if not doc:
        return None
    doc.pop("_id", None)
    doc["generated_at"] = doc["generated_at"].isoformat() if doc.get("generated_at") else None
    return doc


def load_latest_available(mode: str, on_or_before: str) -> Optional[Dict[str, Any]]:
    """最近一份「日期 <= on_or_before」的快照。

    用于选了非交易日 / 当天还没生成时的回退（与 ``ladder_daily`` 的 is_fallback 同思路）。
    """
    doc = collection().find_one(
        {"mode": mode, "trade_date": {"$lte": on_or_before}},
        sort=[("trade_date", -1), ("generated_at", -1)],
    )
    if not doc:
        return None
    doc.pop("_id", None)
    doc["generated_at"] = doc["generated_at"].isoformat() if doc.get("generated_at") else None
    doc["is_fallback"] = True
    return doc


def list_snapshots(trade_date: str) -> List[Dict[str, Any]]:
    """列出当日已有的快照（盘中各时点 + 收盘定稿），用于前端切换与比对。"""
    rows = []
    for doc in collection().find({"trade_date": trade_date}).sort("generated_at", 1):
        rows.append({
            "mode": doc.get("mode"),
            "snapshot_minute": doc.get("snapshot_minute"),
            "generated_at": doc.get("generated_at").isoformat() if doc.get("generated_at") else None,
            "branch_count": len(doc.get("branches") or []),
            "total_limit_up": (doc.get("stats") or {}).get("total_limit_up"),
        })
    return rows


__all__ = [
    "build_intraday_tree",
    "build_eod_tree",
    "load_events",
    "save_tree",
    "load_tree",
    "load_latest_available",
    "list_snapshots",
    "collection",
]
