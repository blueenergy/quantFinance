"""Holder-name taxonomy for the institutional-holdings summary.

The taxonomy is intentionally rule-based and auditable. It classifies the
``holder_name`` strings disclosed by Tushare ``top10_floatholders`` into the
categories the board asks about. It is not a full entity-resolution system.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

CATEGORY_LABELS = {
    "social_security": "社保",
    "pension": "养老金",
    "insurance": "险资",
    "foreign_bank": "外资投行",
    "foreign_asset_manager": "外资资管",
    "hksc": "香港中央结算",
}
ALL_CATEGORIES = tuple(CATEGORY_LABELS)

_ALIASES = {
    "social_security": "social_security",
    "社保": "social_security",
    "全国社保": "social_security",
    "pension": "pension",
    "养老金": "pension",
    "年金": "pension",
    "insurance": "insurance",
    "险资": "insurance",
    "保险": "insurance",
    "foreign_bank": "foreign_bank",
    "外资投行": "foreign_bank",
    "外资银行": "foreign_bank",
    "foreign_asset_manager": "foreign_asset_manager",
    "外资资管": "foreign_asset_manager",
    "hksc": "hksc",
    "香港中央结算": "hksc",
    "北向": "hksc",
}
_MULTI_ALIASES = {
    "foreign": ("foreign_bank", "foreign_asset_manager"),
    "外资": ("foreign_bank", "foreign_asset_manager"),
}

_SOCIAL_SECURITY = re.compile(r"社保基金|社会保障基金|全国社保基金")
_PENSION = re.compile(r"基本养老保险基金|养老金产品|职业年金|企业年金|年金计划")
_HKSC = re.compile(r"香港中央结算|HKSCC|HONG\s*KONG.*CLEARING", re.I)

# Names that look like foreign institutions but are domestic JV fund products.
_JUNK_FUND = re.compile(
    r"证券投资基金|基金管理有限公司|资产管理计划|基金资产管理|基金组合|"
    r"国投瑞银|工银瑞信|交银施罗德|汇丰晋信|上投摩根|摩根士丹利华鑫",
    re.I,
)

_FOREIGN_ASSET_MANAGER = re.compile(
    r"BLACKROCK|贝莱德|VANGUARD|先锋|FIDELITY|富达|SCHRODER|施罗德|"
    r"\bFIL\b|MSCI|MORGAN\s*STANLEY\s*INVESTMENT|摩根士丹利(国际)?投资管理|"
    r"瑞银(资产|基金)",
    re.I,
)

_FOREIGN_BANK = re.compile(
    r"GOLDMAN|高盛|MORGAN\s*STANLEY|摩根士丹利|摩根斯坦利|"
    r"J\.?\s*P\.?\s*MORGAN|JP\s*MORGAN|摩根大通|JPM|"
    r"\bUBS\b|瑞银|BARCLAYS?|BARCARLY|巴克莱|"
    r"CITI(GROUP|BANK|CORP|GLOBAL|SECUR)|\bCITI\b|花旗|"
    r"STANDARD\s*CHARTERED|渣打|SCB\s*HK|SC\s*BANK|"
    r"BNP|PARIBAS|法巴|巴黎银行|"
    r"DEUTSCHE|德意志|德银|DB\s*GLOBAL|"
    r"SOCIE?TE?\s*GENERALE|法兴|法国兴业|SG\s*SEC|"
    r"CREDIT\s*SUISSE|瑞信|"
    r"BANK\s*OF\s*AMERICA|BAML|美银|美国银行|"
    r"NOMURA|野村|HSBC|汇丰(?!金)",
    re.I,
)

_INSURANCE_EXCLUDE = re.compile(r"保险经纪|保险代理|保险销售|保险咨询")
_INSURANCE = re.compile(
    r"人寿保险|财产保险|健康保险|养老保险(股份)?有限公司|"
    r"保险[（(]?集团|保险(股份|有限)?公司|保险资产管理|保险产品|保险资金|财险|寿险|"
    r"中国人寿|中国平安保险|太平洋人寿|太平洋财产|新华人寿|新华保险|泰康人寿|泰康保险|"
    r"太平人寿|太平保险|中国人民保险|中国人保|大家人寿|大家保险|阳光保险|友邦|"
    r"中邮人寿|国华人寿|前海人寿|恒大人寿|富德生命人寿|交银人寿|工银安盛|建信人寿|"
    r"农银人寿|招商仁和|中信保诚|瑞众人寿|长城人寿|中华联合|华泰保险|安邦|和谐健康|"
    r"百年人寿|中意人寿|中英人寿|同方全球人寿|中荷人寿|中银保险|渤海人寿|珠江人寿|"
    r"利安人寿|东吴人寿|弘康人寿|信泰人寿|君康人寿|上海人寿|国联人寿|爱心人寿|"
    r"北京人寿|三峡人寿|海保人寿|瑞华健康|昆仑健康|复星联合健康|太保安联健康|"
    r"平安健康|平安养老保险|平安资产|国寿资产|人保资产|泰康资产|新华资产|"
    r"太平洋资产|太平资产|华泰资产|阳光资产|中再资产|民生通惠|永明资产"
)


def classify_holder(name: Optional[str]) -> Optional[str]:
    """Return one category key, or ``None`` when the holder is out of scope."""
    text = str(name or "").strip()
    if not text:
        return None
    if _SOCIAL_SECURITY.search(text):
        return "social_security"
    if _PENSION.search(text):
        return "pension"
    if _HKSC.search(text):
        return "hksc"
    # Domestic JV fund products can embed foreign names; exclude them before
    # the foreign patterns. Social-security / pension names are checked above
    # because they often contain "基金管理有限公司" as the trustee.
    if _JUNK_FUND.search(text):
        return None
    if _FOREIGN_ASSET_MANAGER.search(text):
        return "foreign_asset_manager"
    if _FOREIGN_BANK.search(text):
        return "foreign_bank"
    if _INSURANCE_EXCLUDE.search(text):
        return None
    if _INSURANCE.search(text):
        return "insurance"
    return None


def parse_categories(raw: Optional[str]) -> Sequence[str]:
    """Parse a comma-separated category filter; default ``all``."""
    if not raw or not str(raw).strip():
        return ALL_CATEGORIES
    tokens = [token.strip() for token in str(raw).split(",") if token.strip()]
    if not tokens or any(token.lower() in {"all", "*"} for token in tokens):
        return ALL_CATEGORIES
    selected = []
    for token in tokens:
        multi = _MULTI_ALIASES.get(token.lower(), _MULTI_ALIASES.get(token))
        if multi:
            for key in multi:
                if key not in selected:
                    selected.append(key)
            continue
        key = _ALIASES.get(token.lower(), _ALIASES.get(token))
        if key and key not in selected:
            selected.append(key)
    return tuple(selected) or ALL_CATEGORIES


def category_label(key: str) -> str:
    return CATEGORY_LABELS.get(key, key)


def labels_for(categories: Iterable[str]) -> dict:
    return {key: category_label(key) for key in categories}
