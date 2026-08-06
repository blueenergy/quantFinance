"""Versioned universe presets for screening."""

from __future__ import annotations

from typing import Dict, List

from .schemas import UniversePresetMeta

# 申万 2021 L2 行业白名单 — 科技硬件相关板块（版本化，可审计）
# 名称须与 sw_index_member.l2_name 完全一致：无 L3 细分的 L2 带「Ⅱ」后缀（U+2161），
# 「仪器仪表」是 通用设备 下的 L3，已被覆盖，不再单列。
TECH_HARDWARE_V1_L2: List[str] = [
    "半导体",
    "电子化学品Ⅱ",
    "光学光电子",
    "其他电子Ⅱ",
    "元件",
    "消费电子",
    "通信设备",
    "计算机设备",
    "自动化设备",
    "专用设备",
    "通用设备",
    "电机Ⅱ",
    "汽车零部件",
]

UNIVERSE_PRESETS: Dict[str, dict] = {
    "tech_hardware_v1": {
        "id": "tech_hardware_v1",
        "label": "科技硬件",
        "version": "1.1.0",
        "description": "申万2021 L2：半导体、电子、通信、计算机设备、自动化等科技硬件板块",
        "universe_type": "sw_l2",
        "sw_l2_names": TECH_HARDWARE_V1_L2,
    },
}

DEFAULT_PRICE_PRESET = "tech_hardware_v1"
DEFAULT_PRICE_START = "20260401"


def list_universe_presets() -> List[UniversePresetMeta]:
    out: List[UniversePresetMeta] = []
    for preset in UNIVERSE_PRESETS.values():
        l2 = preset.get("sw_l2_names") or []
        hint = f"{len(l2)} 个申万 L2 行业" if l2 else None
        out.append(
            UniversePresetMeta(
                id=preset["id"],
                label=preset["label"],
                version=preset["version"],
                description=preset["description"],
                universe_type=preset.get("universe_type", "sw_l2"),
                members_hint=hint,
            )
        )
    return out


def get_preset_l2_names(preset_id: str) -> List[str]:
    preset = UNIVERSE_PRESETS.get(preset_id)
    if not preset:
        raise KeyError(f"unknown universe preset: {preset_id}")
    return list(preset.get("sw_l2_names") or [])
