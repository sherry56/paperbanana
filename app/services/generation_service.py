from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from agents.critic_agent import CriticAgent
from agents.planner_agent import PlannerAgent
from agents.polish_agent import PolishAgent
from agents.retriever_agent import RetrieverAgent
from agents.stylist_agent import StylistAgent
from agents.vanilla_agent import VanillaAgent
from agents.visualizer_agent import VisualizerAgent
from utils import config
from utils.generation_utils import GPT_DIRECT_MODE_ERROR, is_gpt_model_name
from utils.paperviz_processor import PaperVizProcessor

DEFAULT_MAX_CONCURRENT = 10


def create_sample_inputs(
    method_content: str,
    caption: str,
    aspect_ratio: str = "16:9",
    num_copies: int = 10,
    max_critic_rounds: int = 3,
) -> list[dict[str, Any]]:
    base_input = {
        "filename": "demo_input",
        "caption": caption,
        "content": method_content,
        "visual_intent": caption,
        "additional_info": {"rounded_ratio": aspect_ratio},
        "max_critic_rounds": max_critic_rounds,
    }
    out = []
    for i in range(num_copies):
        row = base_input.copy()
        row["filename"] = f"demo_input_candidate_{i}"
        row["candidate_id"] = i
        out.append(row)
    return out


async def process_parallel_candidates(
    data_list: list[dict[str, Any]],
    exp_mode: str,
    retrieval_setting: str,
    main_model_name: str,
    image_gen_model_name: str,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[dict[str, Any]]:
    if is_gpt_model_name(main_model_name) or is_gpt_model_name(image_gen_model_name):
        raise ValueError(GPT_DIRECT_MODE_ERROR)
    exp_config = config.ExpConfig(
        dataset_name="Demo",
        split_name="demo",
        exp_mode=exp_mode,
        retrieval_setting=retrieval_setting,
        main_model_name=main_model_name,
        image_gen_model_name=image_gen_model_name,
        work_dir=Path(__file__).resolve().parents[2],
    )
    processor = PaperVizProcessor(
        exp_config=exp_config,
        vanilla_agent=VanillaAgent(exp_config=exp_config),
        planner_agent=PlannerAgent(exp_config=exp_config),
        visualizer_agent=VisualizerAgent(exp_config=exp_config),
        stylist_agent=StylistAgent(exp_config=exp_config),
        critic_agent=CriticAgent(exp_config=exp_config),
        retriever_agent=RetrieverAgent(exp_config=exp_config),
        polish_agent=PolishAgent(exp_config=exp_config),
    )
    results: list[dict[str, Any]] = []
    async for result_data in processor.process_queries_batch(
        data_list, max_concurrent=max_concurrent, do_eval=False
    ):
        print(
            "[ResearchDrawing] generation_service result image fields: "
            f"candidate_id={result_data.get('candidate_id')} "
            f"eval_image_field={result_data.get('eval_image_field')} "
            f"image_keys={_result_image_keys(result_data)}",
            flush=True,
        )
        results.append(result_data)
    return results


def run_parallel_candidates_sync(
    data_list: list[dict[str, Any]],
    exp_mode: str,
    retrieval_setting: str,
    main_model_name: str,
    image_gen_model_name: str,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
) -> list[dict[str, Any]]:
    return asyncio.run(
        process_parallel_candidates(
            data_list=data_list,
            exp_mode=exp_mode,
            retrieval_setting=retrieval_setting,
            main_model_name=main_model_name,
            image_gen_model_name=image_gen_model_name,
            max_concurrent=max_concurrent,
        )
    )


def _result_image_keys(result: dict[str, Any]) -> list[str]:
    return sorted(
        key
        for key, value in result.items()
        if value
        and (
            "base64" in key
            or key.endswith("_b64")
            or key.endswith("_b64_json")
            or key in {"image", "image_base64"}
        )
    )


def _image_b64_from_key(result: dict[str, Any], key: str | None) -> str | None:
    if not key:
        return None
    value = result.get(key)
    return value if isinstance(value, str) and value.strip() else None


def base64_to_image(b64_str: str) -> Image.Image | None:
    if not b64_str:
        print("[ResearchDrawing] image base64 missing")
        return None
    try:
        if "," in b64_str:
            b64_str = b64_str.split(",")[1]
        b64_str = b64_str.strip()
        image_data = base64.b64decode(b64_str, validate=False)
        print(
            f"[ResearchDrawing] image base64 exists=True b64_len={len(b64_str)} "
            f"decoded_bytes_len={len(image_data)}",
            flush=True,
        )
        image = Image.open(BytesIO(image_data))
        image.load()
        return image
    except Exception as exc:
        print(
            f"[ResearchDrawing] image base64 decode/open failed: b64_len={len(b64_str) if b64_str else 0} "
            f"error={exc}",
            flush=True,
        )
        return None


def extract_final_diagram_b64_from_result(result: dict[str, Any], exp_mode: str) -> str | None:
    task_name = "diagram"
    eval_image_field = str(result.get("eval_image_field") or "").strip()
    b64 = _image_b64_from_key(result, eval_image_field)
    if b64:
        print(f"[ResearchDrawing] selected image field={eval_image_field}", flush=True)
        return b64

    for round_idx in range(3, -1, -1):
        image_key = f"target_{task_name}_critic_desc{round_idx}_base64_jpg"
        b64 = _image_b64_from_key(result, image_key)
        if b64:
            print(f"[ResearchDrawing] selected image field={image_key}", flush=True)
            return b64

    fallback_keys = [
        f"target_{task_name}_stylist_desc0_base64_jpg",
        f"target_{task_name}_desc0_base64_jpg",
        f"vanilla_{task_name}_base64_jpg",
        f"polished_{task_name}_base64_jpg",
        "image_base64",
        "image",
    ]
    for image_key in fallback_keys:
        b64 = _image_b64_from_key(result, image_key)
        if b64:
            print(f"[ResearchDrawing] selected image field={image_key}", flush=True)
            return b64
    return None


def result_image_to_png_bytes(result: dict[str, Any], exp_mode: str) -> bytes | None:
    print(
        "[ResearchDrawing] result_image_to_png_bytes detected "
        f"image_keys={_result_image_keys(result)} eval_image_field={result.get('eval_image_field')}",
        flush=True,
    )
    b64 = extract_final_diagram_b64_from_result(result, exp_mode)
    if not b64:
        print(
            "[ResearchDrawing] candidate image empty before decode: "
            f"exp_mode={exp_mode} result_keys={sorted(result.keys())} image_keys={_result_image_keys(result)}",
            flush=True,
        )
        return None
    img = base64_to_image(b64)
    if not img:
        print(
            "[ResearchDrawing] candidate image decode failed: "
            f"exp_mode={exp_mode} b64_exists=True b64_len={len(b64)} image_keys={_result_image_keys(result)}",
            flush=True,
        )
        return None
    buf = BytesIO()
    img.save(buf, format="PNG")
    print(f"[ResearchDrawing] candidate PNG bytes len={buf.tell()}", flush=True)
    return buf.getvalue()

