# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""
Commercial-style usage tiers: fixed parameter bundles for cost / quality tradeoffs.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

# Admin-only: full manual control (same as historical demo sidebar)
CUSTOM_TIER_KEY = "_custom"

TierId = Literal["budget"]

TIER_ORDER: List[str] = ["budget"]

# 系统内置仅保留「最低成本」一档；管理员可在后台覆盖该档的模板参数（tier_templates_json）。
TIERS: Dict[str, Dict[str, Any]] = {
    "budget": {
        "label": "最低成本（仅生成）",
        "blurb": (
            "单候选 + 2 轮 Critic，适合先出草图；OpenRouter Flash 模型。"
            "建议单次约 **$0.05–$0.20**。"
        ),
        "exp_mode": "demo_planner_critic",
        "retrieval_setting": "random",
        "num_candidates": 1,
        "aspect_ratio": "16:9",
        "max_critic_rounds": 2,
        "main_model_name": "openrouter/google/gemini-2.5-flash",
        "image_gen_model_name": "openrouter/google/gemini-2.5-flash-image-preview",
        "max_refine_resolution": "2K",
    },
}


def normalize_legacy_tier_mode(mode: str) -> str:
    """旧数据中 tier:standard 等已废弃档位，统一视为 tier:budget。"""
    m = (mode or "").strip()
    if not m.startswith("tier:"):
        return m
    tid = m[5:]
    if tid in TIER_ORDER:
        return m
    return "tier:budget"


def tier_label(tier_id: str) -> str:
    if tier_id == CUSTOM_TIER_KEY:
        return "自定义（管理员）"
    t = TIERS.get(tier_id) or {}
    return str(t.get("label", tier_id))


def refine_resolution_choices(tier_id: Optional[str]) -> List[str]:
    """Refine tab: which resolutions user may pick."""
    if not tier_id or tier_id == CUSTOM_TIER_KEY:
        return ["2K", "4K"]
    cap = TIERS.get(tier_id, {}).get("max_refine_resolution", "2K")
    if cap == "4K":
        return ["2K", "4K"]
    return ["2K"]


def flatten_tier_for_demo(tier_id: str) -> Dict[str, Any]:
    if tier_id == CUSTOM_TIER_KEY or tier_id not in TIERS:
        raise KeyError(tier_id)
    cfg = TIERS[tier_id]
    return {
        "exp_mode": cfg["exp_mode"],
        "retrieval_setting": cfg["retrieval_setting"],
        "num_candidates": int(cfg["num_candidates"]),
        "aspect_ratio": cfg["aspect_ratio"],
        "max_critic_rounds": int(cfg["max_critic_rounds"]),
        "main_model_name": cfg["main_model_name"],
        "image_gen_model_name": cfg["image_gen_model_name"],
    }


VALID_EXP_MODES = frozenset({"demo_full", "demo_planner_critic"})
VALID_RETRIEVAL = frozenset({"auto", "manual", "random", "none"})
VALID_ASPECT = frozenset({"21:9", "16:9", "3:2"})
VALID_REFINE_CAP = frozenset({"2K", "4K"})


def normalize_pipeline_config(raw: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Validate user-submitted pipeline bundle. Returns (config, error_message).
    """
    if not isinstance(raw, dict):
        return None, "配置格式无效。"

    exp_mode = str(raw.get("exp_mode", "")).strip()
    retrieval_setting = str(raw.get("retrieval_setting", "")).strip()
    aspect_ratio = str(raw.get("aspect_ratio", "")).strip()
    main_model_name = str(raw.get("main_model_name", "")).strip()
    image_gen_model_name = str(raw.get("image_gen_model_name", "")).strip()
    max_refine_resolution = str(raw.get("max_refine_resolution", "2K")).strip().upper()
    if max_refine_resolution == "4K":
        max_refine_resolution = "4K"
    else:
        max_refine_resolution = "2K"

    try:
        num_candidates = int(raw.get("num_candidates", 1))
        max_critic_rounds = int(raw.get("max_critic_rounds", 2))
    except (TypeError, ValueError):
        return None, "候选数或 Critic 轮数必须为整数。"

    if exp_mode not in VALID_EXP_MODES:
        return None, "流水线模式无效。"
    if retrieval_setting not in VALID_RETRIEVAL:
        return None, "检索方式无效。"
    if aspect_ratio not in VALID_ASPECT:
        return None, "宽高比无效。"
    if not main_model_name or len(main_model_name) > 200:
        return None, "主模型名称不能为空或过长。"
    if not image_gen_model_name or len(image_gen_model_name) > 200:
        return None, "生图模型名称不能为空或过长。"
    if not (1 <= num_candidates <= 20):
        return None, "并行候选数须在 1–20 之间。"
    if not (1 <= max_critic_rounds <= 5):
        return None, "Critic 轮数须在 1–5 之间。"
    if max_refine_resolution not in VALID_REFINE_CAP:
        return None, "精修分辨率上限无效。"

    return (
        {
            "exp_mode": exp_mode,
            "retrieval_setting": retrieval_setting,
            "num_candidates": num_candidates,
            "aspect_ratio": aspect_ratio,
            "max_critic_rounds": max_critic_rounds,
            "main_model_name": main_model_name,
            "image_gen_model_name": image_gen_model_name,
            "max_refine_resolution": max_refine_resolution,
        },
        None,
    )


def refine_resolution_choices_from_config(cfg: Optional[Dict[str, Any]]) -> List[str]:
    if not cfg:
        return ["2K", "4K"]
    cap = cfg.get("max_refine_resolution", "2K")
    if cap == "4K":
        return ["2K", "4K"]
    return ["2K"]
