# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Utility functions for interacting with Gemini and Claude APIs, image processing, and PDF handling.
"""

import json
import asyncio
import base64
from io import BytesIO
from functools import partial
from ast import literal_eval
from typing import List, Dict, Any

import httpx
import aiofiles
from PIL import Image
from google import genai
from google.genai import types
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

import os
import logging

import yaml
from pathlib import Path

logger = logging.getLogger(__name__)

# Load config
config_path = Path(__file__).parent.parent / "configs" / "model_config.yaml"
model_config = {}
if config_path.exists():
    with open(config_path, "r") as f:
        model_config = yaml.safe_load(f) or {}

def get_config_val(section, key, env_var, default=""):
    val = os.getenv(env_var)
    if not val and section in model_config:
        val = model_config[section].get(key)
    return val or default

# Initialize clients lazily or with robust defaults
api_key = get_config_val("api_keys", "google_api_key", "GOOGLE_API_KEY", "")
if api_key:
    gemini_client = genai.Client(api_key=api_key)
else:
    gemini_client = None


anthropic_api_key = get_config_val("api_keys", "anthropic_api_key", "ANTHROPIC_API_KEY", "")
if anthropic_api_key:
    anthropic_client = AsyncAnthropic(api_key=anthropic_api_key)
else:
    anthropic_client = None

openai_api_key = get_config_val("api_keys", "openai_api_key", "OPENAI_API_KEY", "")
if openai_api_key:
    openai_client = AsyncOpenAI(api_key=openai_api_key)
else:
    openai_client = None

gpt_api_key = get_config_val("api_keys", "gpt_api_key", "GPT_API_KEY", "")
gpt_base_url = get_config_val("api_base_urls", "gpt_base_url", "GPT_BASE_URL", "https://api.openai.com/v1")
gpt_image_api_key = get_config_val("api_keys", "gpt_image_api_key", "GPT_IMAGE_API_KEY", "")
gpt_image_base_url = get_config_val("api_base_urls", "gpt_image_base_url", "GPT_IMAGE_BASE_URL", "")
if gpt_api_key:
    gpt_text_client = AsyncOpenAI(
        base_url=gpt_base_url.rstrip("/"),
        api_key=gpt_api_key,
    )
else:
    gpt_text_client = None

openrouter_api_key = get_config_val("api_keys", "openrouter_api_key", "OPENROUTER_API_KEY", "")
if openrouter_api_key:
    openrouter_client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=openrouter_api_key,
    )
else:
    openrouter_client = None


def _response_preview(text: str, limit: int = 300) -> str:
    return (text or "")[:limit].replace("\n", "\\n").replace("\r", "\\r")


def _log_gpt_image_response(url: str, model_name: str, status_code: int | str, content_type: str, body_text: str) -> None:
    message = (
        "[GPT Image] final image url="
        f"{url} image_model={model_name} status_code={status_code} "
        f"content_type={content_type or '-'} response_preview={_response_preview(body_text)}"
    )
    print(message, flush=True)
    logger.info(message)


def _is_html_response(content_type: str, body_text: str) -> bool:
    return "text/html" in (content_type or "").lower() or (body_text or "").lstrip().lower().startswith("<html")


async def _call_gpt_image_2_http_async(model_name: str, prompt: str, config: Dict[str, Any]) -> List[str]:
    size = config.get("size", "1536x1024")
    request_api_key = (
        (config.get("gpt_image_api_key") or "").strip()
        or (config.get("gpt_api_key") or "").strip()
        or gpt_image_api_key.strip()
        or gpt_api_key.strip()
    )
    request_base_url = (
        (config.get("gpt_image_base_url") or "").strip()
        or (config.get("gpt_base_url") or "").strip()
        or gpt_image_base_url.strip()
        or gpt_base_url.strip()
    ).rstrip("/")

    if not request_api_key:
        raise RuntimeError("GPT Image client was not initialized: missing GPT_IMAGE_API_KEY or GPT_API_KEY.")
    if not request_base_url:
        raise RuntimeError("GPT Image base URL is not configured: missing GPT_IMAGE_BASE_URL or GPT_BASE_URL.")

    url = f"{request_base_url}/images/generations"
    payload = {
        "model": "gpt-image-2",
        "prompt": prompt,
        "size": size,
    }
    headers = {
        "Authorization": f"Bearer {request_api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
        response = await client.post(url, headers=headers, json=payload)

    content_type = response.headers.get("content-type", "")
    body_text = response.text or ""
    _log_gpt_image_response(url, model_name, response.status_code, content_type, body_text)

    if response.status_code < 200 or response.status_code >= 300:
        return ["Error"]
    if _is_html_response(content_type, body_text):
        return ["Error"]
    if "json" not in content_type.lower():
        return ["Error"]

    try:
        data = response.json()
    except ValueError:
        return ["Error"]

    b64_json = None
    if isinstance(data, dict):
        items = data.get("data")
        if isinstance(items, list) and items:
            first_item = items[0]
            if isinstance(first_item, dict):
                b64_json = first_item.get("b64_json")

    if isinstance(b64_json, str) and b64_json.strip():
        return [b64_json.strip()]

    _log_gpt_image_response(url, model_name, response.status_code, content_type, body_text)
    return ["Error"]


def _convert_to_gemini_parts(contents: List[Dict[str, Any]]) -> List[types.Part]:
    """
    Convert a generic content list to a list of Gemini's genai.types.Part objects.
    """
    gemini_parts = []
    for item in contents:
        if item.get("type") == "text":
            gemini_parts.append(types.Part.from_text(text=item["text"]))
        elif item.get("type") == "image":
            source = item.get("source", {})
            if source.get("type") == "base64":
                gemini_parts.append(
                    types.Part.from_bytes(
                        data=base64.b64decode(source["data"]),
                        mime_type=source["media_type"],
                    )
                )
            elif "image_base64" in item:
                # Shorthand format used by planner_agent
                gemini_parts.append(
                    types.Part.from_bytes(
                        data=base64.b64decode(item["image_base64"]),
                        mime_type="image/jpeg",
                    )
                )
    return gemini_parts


async def call_gemini_with_retry_async(
    model_name, contents, config, max_attempts=5, retry_delay=5, error_context=""
):
    """
    ASYNC: Call Gemini API with asynchronous retry logic.
    """
    if gemini_client is None:
        raise RuntimeError(
            "Gemini client was not initialized: missing Google API key. "
            "Please set GOOGLE_API_KEY in environment, or configure api_keys.google_api_key in configs/model_config.yaml."
        )

    result_list = []
    target_candidate_count = config.candidate_count
    # Gemini API max candidate count is 8. We will call multiple times if needed.
    if config.candidate_count > 8:
        config.candidate_count = 8

    current_contents = contents
    for attempt in range(max_attempts):
        try:
            # Use global client
            client = gemini_client

            # Convert generic content list to Gemini's format right before the API call
            gemini_contents = _convert_to_gemini_parts(current_contents)
            response = await client.aio.models.generate_content(
                model=model_name, contents=gemini_contents, config=config
            )

            # If we are using Image Generation models to generate images
            if (
                "nanoviz" in model_name
                or "image" in model_name
            ):
                raw_response_list = []
                if not response.candidates or not response.candidates[0].content.parts:
                    print(
                        f"[Warning]: Failed to generate image, retrying in {retry_delay} seconds..."
                    )
                    await asyncio.sleep(retry_delay)
                    continue

                # In this mode, we can only have one candidate
                for part in response.candidates[0].content.parts:
                    if part.inline_data:
                        # Append base64 encoded image data to raw_response_list
                        raw_response_list.append(
                            base64.b64encode(part.inline_data.data).decode("utf-8")
                        )
                        break

            # Otherwise, for text generation models
            else:
                raw_response_list = [
                    part.text
                    for candidate in response.candidates
                    for part in candidate.content.parts
                    if part.text is not None
                ]
            result_list.extend([r for r in raw_response_list if r and r.strip() != ""])
            if len(result_list) >= target_candidate_count:
                result_list = result_list[:target_candidate_count]
                break

        except Exception as e:
            context_msg = f" for {error_context}" if error_context else ""
            
            # Exponential backoff (capped at 30s)
            current_delay = min(retry_delay * (2 ** attempt), 30)
            
            print(
                f"Attempt {attempt + 1} for model {model_name} failed{context_msg}: {e}. Retrying in {current_delay} seconds..."
            )

            if attempt < max_attempts - 1:
                await asyncio.sleep(current_delay)
            else:
                print(f"Error: All {max_attempts} attempts failed{context_msg}")
                result_list = ["Error"] * target_candidate_count

    if len(result_list) < target_candidate_count:
        result_list.extend(["Error"] * (target_candidate_count - len(result_list)))
    return result_list

def _convert_to_claude_format(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Converts the generic content list to Claude's API format.
    Currently, the formats are identical, so this acts as a pass-through
    for architectural consistency and future-proofing.

    Claude API's format:
    [
        {"type": "text", "text": "some text"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "..."}},
        ...
    ]
    """
    return contents


def _convert_to_openai_format(contents: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Converts the generic content list (Claude format) to OpenAI's API format.
    
    Claude format:
    [
        {"type": "text", "text": "some text"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "..."}},
        ...
    ]
    
    OpenAI format:
    [
        {"type": "text", "text": "some text"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}},
        ...
    ]
    """
    openai_contents = []
    for item in contents:
        if item.get("type") == "text":
            openai_contents.append({"type": "text", "text": item["text"]})
        elif item.get("type") == "image":
            source = item.get("source", {})
            if source.get("type") == "base64":
                media_type = source.get("media_type", "image/jpeg")
                data = source.get("data", "")
                data_url = f"data:{media_type};base64,{data}"
                openai_contents.append({
                    "type": "image_url",
                    "image_url": {"url": data_url}
                })
            elif "image_base64" in item:
                # Shorthand format used by planner_agent
                data_url = f"data:image/jpeg;base64,{item['image_base64']}"
                openai_contents.append({
                    "type": "image_url",
                    "image_url": {"url": data_url}
                })
    return openai_contents


async def call_claude_with_retry_async(
    model_name, contents, config, max_attempts=5, retry_delay=30, error_context=""
):
    """
    ASYNC: Call Claude API with asynchronous retry logic.
    This version efficiently handles input size errors by validating and modifying
    the content list once before generating all candidates.
    """
    system_prompt = config["system_prompt"]
    temperature = config["temperature"]
    candidate_num = config["candidate_num"]
    max_output_tokens = config["max_output_tokens"]
    response_text_list = []

    # --- Preparation Phase ---
    # Convert to the Claude-specific format and perform an initial optimistic resize.
    current_contents = contents

    # --- Validation and Remediation Phase ---
    # We loop until we get a single successful response, proving the input is valid.
    # Note that this check is required because Claude only has 128k / 256k context windows.
    # For Gemini series that support 1M, we do not need this step.
    is_input_valid = False
    for attempt in range(max_attempts):
        try:
            claude_contents = _convert_to_claude_format(current_contents)
            # Attempt to generate the very first candidate.
            first_response = await anthropic_client.messages.create(
                model=model_name,
                max_tokens=max_output_tokens,
                temperature=temperature,
                messages=[{"role": "user", "content": claude_contents}],
                system=system_prompt,
            )
            response_text_list.append(first_response.content[0].text)
            is_input_valid = True
            break

        except Exception as e:
            error_str = str(e).lower()
            context_msg = f" for {error_context}" if error_context else ""
            print(
                f"Validation attempt {attempt + 1} failed{context_msg}: {error_str}. Retrying in {retry_delay} seconds..."
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(retry_delay)

    # --- Sampling Phase ---
    if not is_input_valid:
        print(
            f"Error: All {max_attempts} attempts failed to validate the input{context_msg}. Returning errors."
        )
        return ["Error"] * candidate_num

    # We already have 1 successful candidate, now generate the rest.
    remaining_candidates = candidate_num - 1
    if remaining_candidates > 0:
        print(
            f"Input validated. Now generating remaining {remaining_candidates} candidates..."
        )
        valid_claude_contents = _convert_to_claude_format(current_contents)
        tasks = [
            anthropic_client.messages.create(
                model=model_name,
                max_tokens=max_output_tokens,
                temperature=temperature,
                messages=[
                    {"role": "user", "content": valid_claude_contents}
                ],
                system=system_prompt,
            )
            for _ in range(remaining_candidates)
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                print(f"Error generating a subsequent candidate: {res}")
                response_text_list.append("Error")
            else:
                response_text_list.append(res.content[0].text)

    return response_text_list

async def call_openai_with_retry_async(
    model_name, contents, config, max_attempts=5, retry_delay=30, error_context=""
):
    """
    ASYNC: Call OpenAI API with asynchronous retry logic.
    This follows the same pattern as Claude's implementation.
    """
    system_prompt = config["system_prompt"]
    temperature = config["temperature"]
    candidate_num = config["candidate_num"]
    max_completion_tokens = config["max_completion_tokens"]
    response_text_list = []
    model_name = _normalize_gpt_text_model_id(model_name)
    text_client = gpt_text_client if model_name == "gpt-5.5" else openai_client
    if text_client is None:
        key_name = "GPT_API_KEY" if model_name == "gpt-5.5" else "OPENAI_API_KEY"
        raise RuntimeError(f"OpenAI text client was not initialized: missing {key_name}.")

    # --- Preparation Phase ---
    # Convert to the OpenAI-specific format
    current_contents = contents

    # --- Validation and Remediation Phase ---
    # We loop until we get a single successful response, proving the input is valid.
    is_input_valid = False
    for attempt in range(max_attempts):
        try:
            openai_contents = _convert_to_openai_format(current_contents)
            # Attempt to generate the very first candidate.
            first_response = await text_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": openai_contents}
                ],
                temperature=temperature,
                max_completion_tokens=max_completion_tokens,
            )
            # If we reach here, the input is valid.
            content = first_response.choices[0].message.content or ""
            if not content.strip():
                print(f"OpenAI returned empty content, retrying...")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(retry_delay)
                continue
            response_text_list.append(content)
            is_input_valid = True
            break  # Exit the validation loop

        except Exception as e:
            error_str = str(e).lower()
            context_msg = f" for {error_context}" if error_context else ""
            print(
                f"Validation attempt {attempt + 1} failed{context_msg}: {error_str}. Retrying in {retry_delay} seconds..."
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(retry_delay)

    # --- Sampling Phase ---
    if not is_input_valid:
        print(
            f"Error: All {max_attempts} attempts failed to validate the input{context_msg}. Returning errors."
        )
        return ["Error"] * candidate_num

    # We already have 1 successful candidate, now generate the rest.
    remaining_candidates = candidate_num - 1
    if remaining_candidates > 0:
        print(
            f"Input validated. Now generating remaining {remaining_candidates} candidates..."
        )
        valid_openai_contents = _convert_to_openai_format(current_contents)
        tasks = [
            text_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": valid_openai_contents}
                ],
                temperature=temperature,
                max_completion_tokens=max_completion_tokens,
            )
            for _ in range(remaining_candidates)
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                print(f"Error generating a subsequent candidate: {res}")
                response_text_list.append("Error")
            else:
                response_text_list.append(res.choices[0].message.content or "Error")

    return response_text_list


async def call_openai_image_generation_with_retry_async(
    model_name, prompt, config, max_attempts=5, retry_delay=30, error_context=""
):
    """
    ASYNC: Call OpenAI Image Generation API (GPT-Image) with asynchronous retry logic.
    """
    normalized_model_name = (model_name or "").strip()
    if normalized_model_name == "gpt-image-2":
        return await _call_gpt_image_2_http_async(normalized_model_name, prompt, config)

    size = config.get("size", "1536x1024")
    quality = config.get("quality", "high")
    background = config.get("background", "opaque")
    output_format = config.get("output_format", "png")
    request_api_key = (config.get("gpt_api_key") or "").strip()
    request_base_url = (config.get("gpt_base_url") or "").strip().rstrip("/") or gpt_base_url.rstrip("/")
    image_client = AsyncOpenAI(base_url=request_base_url, api_key=request_api_key) if request_api_key else gpt_text_client
    if image_client is None:
        raise RuntimeError(
            "GPT Image client was not initialized: missing GPT_API_KEY. "
            "Set GPT_API_KEY/GPT_BASE_URL, or configure api_keys.gpt_api_key."
        )
    
    # Base parameters for all models
    gen_params = {
        "model": model_name,
        "prompt": prompt,
        "n": 1,
        "size": size,
    }
    
    # Add GPT-Image specific parameters
    gen_params.update({
        "quality": quality,
        "background": background,
        "output_format": output_format,
    })

    for attempt in range(max_attempts):
        try:
            response = await image_client.images.generate(**gen_params)
            
            # OpenAI images.generate returns a list of images in response.data
            if response.data and response.data[0].b64_json:
                return [response.data[0].b64_json]
            else:
                print(f"[Warning]: Failed to generate image via OpenAI, no data returned.")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(retry_delay)
                continue

        except Exception as e:
            context_msg = f" for {error_context}" if error_context else ""
            print(
                f"Attempt {attempt + 1} for OpenAI image generation model {model_name} failed{context_msg}: {e}. Retrying in {retry_delay} seconds..."
            )

            if attempt < max_attempts - 1:
                await asyncio.sleep(retry_delay)
            else:
                print(f"Error: All {max_attempts} attempts failed{context_msg}")
                return ["Error"]

    return ["Error"]


async def call_openrouter_with_retry_async(
    model_name, contents, config, max_attempts=5, retry_delay=30, error_context=""
):
    """
    ASYNC: Call OpenRouter API (OpenAI-compatible) with asynchronous retry logic.
    """
    if openrouter_client is None:
        raise RuntimeError(
            "OpenRouter client was not initialized: missing API key. "
            "Please set OPENROUTER_API_KEY in environment, or configure "
            "api_keys.openrouter_api_key in configs/model_config.yaml."
        )

    model_name = _to_openrouter_model_id(model_name)
    system_prompt = config["system_prompt"]
    temperature = config["temperature"]
    candidate_num = config["candidate_num"]
    max_completion_tokens = config["max_completion_tokens"]
    response_text_list = []

    current_contents = contents

    is_input_valid = False
    for attempt in range(max_attempts):
        try:
            openai_contents = _convert_to_openai_format(current_contents)
            first_response = await openrouter_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": openai_contents},
                ],
                temperature=temperature,
                max_completion_tokens=max_completion_tokens,
            )
            content = first_response.choices[0].message.content or ""
            if not content.strip():
                print(f"OpenRouter returned empty content, retrying...")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(retry_delay)
                continue
            response_text_list.append(content)
            is_input_valid = True
            break

        except Exception as e:
            error_str = str(e).lower()
            context_msg = f" for {error_context}" if error_context else ""
            current_delay = min(retry_delay * (2 ** attempt), 60)
            print(
                f"OpenRouter attempt {attempt + 1} failed{context_msg}: {error_str}. "
                f"Retrying in {current_delay}s..."
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(current_delay)

    if not is_input_valid:
        context_msg = f" for {error_context}" if error_context else ""
        print(f"Error: All {max_attempts} OpenRouter attempts failed{context_msg}.")
        return ["Error"] * candidate_num

    remaining_candidates = candidate_num - 1
    if remaining_candidates > 0:
        valid_openai_contents = _convert_to_openai_format(current_contents)
        tasks = [
            openrouter_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": valid_openai_contents},
                ],
                temperature=temperature,
                max_completion_tokens=max_completion_tokens,
            )
            for _ in range(remaining_candidates)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                print(f"Error generating a subsequent OpenRouter candidate: {res}")
                response_text_list.append("Error")
            else:
                response_text_list.append(res.choices[0].message.content or "Error")

    return response_text_list


async def call_openrouter_image_generation_with_retry_async(
    model_name, contents, config, max_attempts=5, retry_delay=30, error_context=""
):
    """
    ASYNC: Call OpenRouter image generation via direct httpx POST to avoid
    openai SDK issues with extra_body dropping the model field.
    Images are returned in choices[0].message.content as inline_data or
    in choices[0].message.images as data URLs.
    """
    if not openrouter_api_key:
        raise RuntimeError(
            "OpenRouter client was not initialized: missing API key."
        )

    system_prompt = config.get("system_prompt", "")
    temperature = config.get("temperature", 1.0)
    aspect_ratio = config.get("aspect_ratio", "1:1")
    image_size = config.get("image_size", "1k")

    model_name = _to_openrouter_model_id(model_name)
    openai_contents = _convert_to_openai_format(contents)

    image_config = {}
    if aspect_ratio:
        image_config["aspect_ratio"] = aspect_ratio
    if image_size:
        image_config["image_size"] = image_size

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": openai_contents},
        ],
        "temperature": temperature,
        "modalities": ["image", "text"],
    }
    if image_config:
        payload["image_config"] = image_config

    headers = {
        "Authorization": f"Bearer {openrouter_api_key}",
        "Content-Type": "application/json",
    }

    def _content_summary(value):
        if isinstance(value, list):
            return [
                sorted(item.keys()) if isinstance(item, dict) else type(item).__name__
                for item in value[:5]
            ]
        if isinstance(value, str):
            return {"type": "str", "length": len(value), "starts_data_image": value.startswith("data:image")}
        return type(value).__name__

    def _image_url_from_dict(item):
        if not isinstance(item, dict):
            return str(item) if item else ""
        image_url = item.get("image_url")
        if isinstance(image_url, dict):
            return str(image_url.get("url") or "")
        if isinstance(image_url, str):
            return image_url
        source = item.get("source")
        if isinstance(source, dict):
            return str(source.get("data") or source.get("url") or "")
        for key in ("url", "data", "b64_json", "base64", "base64_data"):
            if item.get(key):
                return str(item.get(key))
        inline_data = item.get("inline_data")
        if isinstance(inline_data, dict):
            return str(inline_data.get("data") or "")
        return ""

    def _strip_data_url(value):
        raw = (value or "").strip()
        if raw.startswith("data:image") and "," in raw:
            return raw.split(",", 1)[1].strip()
        return raw

    def _extract_image_b64(message):
        if not isinstance(message, dict):
            return ""

        images = message.get("images")
        if isinstance(images, list):
            for img_item in images:
                b64_data = _strip_data_url(_image_url_from_dict(img_item))
                if b64_data:
                    return b64_data

        content = message.get("content")
        if isinstance(content, str):
            b64_data = _strip_data_url(content)
            if content.startswith("data:image") and b64_data:
                return b64_data

        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                inline_data = part.get("inline_data")
                if isinstance(inline_data, dict) and inline_data.get("data"):
                    return str(inline_data.get("data"))
                b64_data = _strip_data_url(_image_url_from_dict(part))
                if b64_data and (str(part.get("type", "")).lower().find("image") >= 0 or b64_data.startswith("iVBOR")):
                    return b64_data
                if part.get("type") in {"image_url", "output_image"}:
                    b64_data = _strip_data_url(_image_url_from_dict(part))
                    if b64_data:
                        return b64_data
        return ""

    print(
        "[OpenRouter image] request summary: "
        + json.dumps(
            {
                "model": payload.get("model"),
                "modalities": payload.get("modalities"),
                "image_config": payload.get("image_config", {}),
                "temperature": payload.get("temperature"),
                "user_content_summary": _content_summary(openai_contents),
            },
            ensure_ascii=False,
        )
    )

    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
            resp.raise_for_status()
            data = resp.json()

            choices = data.get("choices", [])
            summary = {
                "top_keys": sorted(data.keys()) if isinstance(data, dict) else [],
                "choice_count": len(choices) if isinstance(choices, list) else 0,
            }
            if not choices:
                print(f"[OpenRouter image] response summary: {json.dumps(summary, ensure_ascii=False)}")
                print("[Warning]: OpenRouter image generation returned no choices, retrying...")
                if attempt < max_attempts - 1:
                    await asyncio.sleep(retry_delay)
                continue

            message = choices[0].get("message", {})
            summary["message_keys"] = sorted(message.keys()) if isinstance(message, dict) else []
            if isinstance(message, dict):
                summary["content_summary"] = _content_summary(message.get("content"))
                imgs = message.get("images")
                summary["images_count"] = len(imgs) if isinstance(imgs, list) else 0
                if isinstance(imgs, list) and imgs:
                    summary["first_image_keys"] = sorted(imgs[0].keys()) if isinstance(imgs[0], dict) else type(imgs[0]).__name__
            print(f"[OpenRouter image] response summary: {json.dumps(summary, ensure_ascii=False)}")

            b64_data = _extract_image_b64(message)
            if b64_data:
                return [b64_data]

            print(f"[Warning]: OpenRouter image generation returned no images, retrying...")
            if attempt < max_attempts - 1:
                await asyncio.sleep(retry_delay)
            continue

        except httpx.HTTPStatusError as e:
            context_msg = f" for {error_context}" if error_context else ""
            current_delay = min(retry_delay * (2 ** attempt), 60)
            print(
                f"OpenRouter image gen attempt {attempt + 1} failed{context_msg}: "
                f"HTTP {e.response.status_code} - {e.response.text}. "
                f"Retrying in {current_delay}s..."
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(current_delay)
            else:
                print(f"Error: All {max_attempts} attempts failed{context_msg}")
                return ["Error"]
        except Exception as e:
            context_msg = f" for {error_context}" if error_context else ""
            current_delay = min(retry_delay * (2 ** attempt), 60)
            print(
                f"OpenRouter image gen attempt {attempt + 1} failed{context_msg}: {e}. "
                f"Retrying in {current_delay}s..."
            )
            if attempt < max_attempts - 1:
                await asyncio.sleep(current_delay)
            else:
                print(f"Error: All {max_attempts} attempts failed{context_msg}")
                return ["Error"]

    return ["Error"]


def _to_openrouter_model_id(model_name: str) -> str:
    """Convert a bare model name to OpenRouter format (provider/model).

    OpenRouter requires model IDs like 'google/gemini-3-pro-preview'.
    Strip optional 'openrouter/' UI prefix so the API receives 'provider/model'.
    If the name already contains '/', assume it's already qualified (after strip).
    Otherwise, prefix with 'google/' for Gemini models.
    """
    if model_name.startswith("openrouter/"):
        model_name = model_name[len("openrouter/") :]
    if "/" in model_name:
        return model_name
    if model_name.startswith("gemini"):
        return f"google/{model_name}"
    return model_name


def _normalize_gpt_text_model_id(model_name: str) -> str:
    normalized = (model_name or "").strip()
    if normalized in {"gpt5.5", "openrouter/openai/gpt-5.5"}:
        return "gpt-5.5"
    return normalized


async def call_model_with_retry_async(
    model_name, contents, config, max_attempts=5, retry_delay=5, error_context=""
):
    """
    Unified router that dispatches to the correct provider based on model_name.

    Routing rules:
      1. Explicit prefix overrides: "openrouter/" -> OpenRouter, "claude-" -> Anthropic,
         "gpt-"/"o1-"/"o3-"/"o4-" -> OpenAI
      2. No prefix: auto-detect based on which API key is configured.
         Priority: OpenRouter > Gemini > Anthropic > OpenAI
    """
    # Explicit provider prefix overrides auto-detection
    model_name = _normalize_gpt_text_model_id(model_name)
    if model_name.startswith("openrouter/"):
        provider = "openrouter"
        actual_model = model_name[len("openrouter/"):]
    elif model_name.startswith("claude-"):
        provider = "anthropic"
        actual_model = model_name
    elif any(model_name.startswith(p) for p in ("gpt-", "o1-", "o3-", "o4-")):
        provider = "openai"
        actual_model = model_name
    else:
        # Auto-detect provider based on which API key is configured
        actual_model = model_name
        if openrouter_client is not None:
            provider = "openrouter"
            actual_model = _to_openrouter_model_id(model_name)
        elif gemini_client is not None:
            provider = "gemini"
        elif anthropic_client is not None:
            provider = "anthropic"
        elif openai_client is not None or gpt_text_client is not None:
            provider = "openai"
        else:
            raise RuntimeError(
                "No API client available. Please configure at least one API key "
                "in configs/model_config.yaml or via environment variables."
            )

    if provider == "gemini":
        return await call_gemini_with_retry_async(
            model_name=actual_model,
            contents=contents,
            config=config,
            max_attempts=max_attempts,
            retry_delay=retry_delay,
            error_context=error_context,
        )

    # Convert Gemini GenerateContentConfig -> dict for OpenAI/Claude/OpenRouter
    cfg_dict = {
        "system_prompt": config.system_instruction if hasattr(config, "system_instruction") else "",
        "temperature": config.temperature if hasattr(config, "temperature") else 1.0,
        "candidate_num": config.candidate_count if hasattr(config, "candidate_count") else 1,
        "max_completion_tokens": config.max_output_tokens if hasattr(config, "max_output_tokens") else 50000,
    }

    call_fn = {
        "openrouter": call_openrouter_with_retry_async,
        "anthropic": call_claude_with_retry_async,
        "openai": call_openai_with_retry_async,
    }[provider]

    return await call_fn(
        model_name=actual_model,
        contents=contents,
        config=cfg_dict,
        max_attempts=max_attempts,
        retry_delay=retry_delay,
        error_context=error_context,
    )
