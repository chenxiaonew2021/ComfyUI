import base64
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import quote, urlsplit
import uuid

import numpy as np
from PIL import Image, ImageOps
import requests
import torch

import comfy.model_management
import folder_paths


BASE_URL_ENV = "TEXT_TO_IMAGE_BASE_URL"
EDIT_URL_ENV = "REFERENCE_IMAGE_BASE_URL"
API_KEY_ENV = "IMAGE_GENERATION_API_KEY"
KEY_FILE = Path.home() / ".config/comfyui/image_generation_api_key"


def load_api_key():
    key = os.environ.get(API_KEY_ENV, "").strip()
    if not key and KEY_FILE.is_file():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        raise ValueError(f"请设置 {API_KEY_ENV}，或将服务密钥保存到 {KEY_FILE}")
    return key


def generation_url(base_url, edit=False):
    url = (base_url or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment):
        raise ValueError("base_url 必须是 HTTPS 图片服务地址，不要附带查询参数或密钥")
    operation = "edits" if edit else "generations"
    if url.endswith("/images/" + operation):
        return url
    return url + "/images/" + operation


def resolve_base_url(base_url, edit=False):
    env_name = EDIT_URL_ENV if edit else BASE_URL_ENV
    configured = os.environ.get(env_name, "").strip()
    if configured:
        return configured
    value = (base_url or "").strip()
    if not value:
        raise ValueError(f"请设置 {env_name}，或在节点的 base_url 中填写服务地址")
    return value


def safe_error(detail, api_key):
    return str(detail).replace(api_key, "[已隐藏]").replace(quote(api_key, safe=""), "[已隐藏]")[:400]


class TextToImageGenerate:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt": ("STRING", {"multiline": True, "default": "一名侦探站在雨夜巷口，中景，电影灯光，写实摄影"}),
                "base_url": ("STRING", {"default": "", "tooltip": "可由 TEXT_TO_IMAGE_BASE_URL 环境变量提供；留空时必须设置环境变量。"}),
                "model": ("STRING", {"default": "gpt-image-2"}),
                "size": ("STRING", {"default": "1024x1024", "tooltip": "实际生成尺寸，例如 1024x1024、1280x720、2048x1152；auto 时省略此参数。"}),
                "quality": (["auto", "low", "medium", "high"], {"default": "low"}),
                "n": ("INT", {"default": 1, "min": 1, "max": 10}),
                "timeout_seconds": ("INT", {"default": 300, "min": 30, "max": 1800, "step": 30}),
            },
            "optional": {
                "reuse_result": ("BOOLEAN", {"default": False, "tooltip": "复用相同提示词、参数和参考图的已保存图片，便于续跑工作流。"}),
                "generation_id": ("INT", {"default": 0, "min": 0, "max": 9007199254740991,
                    "tooltip": "开启复用后，修改编号生成新图；不是模型随机种子。"}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("images",)
    FUNCTION = "generate"
    CATEGORY = "image/generation"
    DESCRIPTION = "调用图片服务生成图片。密钥不写入工作流。默认重新请求，可开启结果复用；不自动重试。"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def generate(self, prompt, base_url, model, size, quality, n, timeout_seconds,
                 reuse_result=False, generation_id=0, reference_images=None):
        if not prompt.strip() or not model.strip():
            raise ValueError("请填写提示词和模型名")
        references = []
        if reference_images is not None:
            for frame in reference_images:
                comfy.model_management.throw_exception_if_processing_interrupted()
                pixels = frame.detach().clamp(0, 1).mul(255).round().to(device="cpu").numpy().astype("uint8")
                buffer = io.BytesIO()
                Image.fromarray(pixels).save(buffer, format="PNG")
                references.append(buffer.getvalue())
        endpoint = generation_url(resolve_base_url(base_url, edit=bool(references)), edit=bool(references))
        cache_directory = None
        if reuse_result:
            identity = [1, endpoint, prompt, model.strip(), size.strip(), quality, n, generation_id,
                        [hashlib.sha256(data).hexdigest() for data in references]]
            key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            cache_directory = Path(folder_paths.get_user_directory()) / "text_to_image/cache" / key
            manifest = cache_directory / "complete.json"
            if manifest.is_file():
                count = json.loads(manifest.read_text(encoding="utf-8"))["count"]
                images = []
                for index in range(count):
                    with Image.open(cache_directory / f"{index}.png") as cached:
                        images.append(torch.from_numpy(np.array(cached.convert("RGB"), dtype=np.float32) / 255.0))
                return (torch.stack(images),)
        api_key = load_api_key()
        log_id = "comfyui-" + uuid.uuid4().hex
        body = {"model": model.strip(), "prompt": prompt, "n": n}
        if size.strip() and size.strip() != "auto":
            body["size"] = size.strip()
        if quality != "auto":
            body["quality"] = quality

        comfy.model_management.throw_exception_if_processing_interrupted()
        try:
            payload = {"json": body}
            if references:
                payload = {
                    "data": body,
                    "files": [("image[]", (f"reference-{index}.png", data, "image/png"))
                              for index, data in enumerate(references, 1)],
                }
            response = requests.post(endpoint, params={"ak": api_key}, headers={"X-TT-LOGID": log_id},
                                     timeout=(15, timeout_seconds), allow_redirects=False, **payload)
        except requests.Timeout:
            raise RuntimeError(f"图片请求超时，上游可能仍在执行；未自动重试。logID={log_id}") from None
        except requests.RequestException:
            raise RuntimeError(f"无法连接图片服务，请检查网络。logID={log_id}") from None

        comfy.model_management.throw_exception_if_processing_interrupted()
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(f"图片服务返回非 JSON 内容（HTTP {response.status_code}）。logID={log_id}") from None
        if not isinstance(payload, dict):
            raise RuntimeError(f"图片服务返回结构异常。logID={log_id}")
        if not 200 <= response.status_code < 300 or payload.get("error"):
            error = payload.get("error")
            detail = error.get("message") if isinstance(error, dict) else error
            detail = detail or payload.get("message") or "请求失败"
            raise RuntimeError(f"图片服务 HTTP {response.status_code}: {safe_error(detail, api_key)}。logID={log_id}")

        items = payload.get("data")
        if not isinstance(items, list) or not items:
            raise RuntimeError(f"图片服务响应缺少 data 图片列表。logID={log_id}")
        images = []
        if cache_directory is not None:
            cache_directory.mkdir(parents=True, exist_ok=True)
        for item in items:
            comfy.model_management.throw_exception_if_processing_interrupted()
            if not isinstance(item, dict):
                raise RuntimeError(f"图片服务条目格式异常。logID={log_id}")
            if item.get("b64_json"):
                encoded = item["b64_json"]
                if encoded.startswith("data:"):
                    encoded = encoded.split(",", 1)[1]
                image_bytes = base64.b64decode(encoded)
            elif item.get("url"):
                image_url = item["url"]
                if urlsplit(image_url).scheme not in ("https", "http"):
                    raise RuntimeError("图片服务返回的图片地址必须是 HTTP(S) URL")
                # Do not forward the AK to image storage or include signed URLs in errors.
                try:
                    image_response = requests.get(image_url, timeout=(15, 60))
                    image_response.raise_for_status()
                except requests.RequestException:
                    raise RuntimeError(f"图片服务已生成图片，但下载失败。logID={log_id}") from None
                image_bytes = image_response.content
            else:
                raise RuntimeError(f"图片服务响应缺少 b64_json 或图片 URL。logID={log_id}")
            with Image.open(io.BytesIO(image_bytes)) as image:
                rgb = ImageOps.exif_transpose(image).convert("RGB")
                pixels = np.array(rgb, dtype=np.float32) / 255.0
                if cache_directory is not None:
                    rgb.save(cache_directory / f"{len(images)}.png")
            images.append(torch.from_numpy(pixels))

        comfy.model_management.throw_exception_if_processing_interrupted()
        result = torch.stack(images)
        if cache_directory is not None:
            with tempfile.NamedTemporaryFile("w", dir=cache_directory, delete=False) as handle:
                json.dump({"count": len(images)}, handle)
                temporary = Path(handle.name)
            os.replace(temporary, cache_directory / "complete.json")
        return (result,)


class ReferenceImageGenerate(TextToImageGenerate):
    @classmethod
    def INPUT_TYPES(cls):
        inputs = super().INPUT_TYPES()
        inputs["required"] = {"reference_images": ("IMAGE",), **inputs["required"]}
        inputs["required"]["base_url"] = ("STRING", {"default": "", "tooltip": "可由 REFERENCE_IMAGE_BASE_URL 环境变量提供；留空时必须设置环境变量。"})
        return inputs

    DESCRIPTION = "参考图片生成新画面。通过图片服务的 multipart 接口保留人物外观；IMAGE batch 可提供多张参考图。支持结果复用。"

    def generate(self, reference_images, **kwargs):
        if reference_images.ndim != 4 or len(reference_images) == 0:
            raise ValueError("请连接至少一张参考图片")
        return super().generate(reference_images=reference_images, **kwargs)


NODE_CLASS_MAPPINGS = {"TextToImageGenerate": TextToImageGenerate, "ReferenceImageGenerate": ReferenceImageGenerate}
NODE_DISPLAY_NAME_MAPPINGS = {
    "TextToImageGenerate": "文生图", "ReferenceImageGenerate": "参考图生图",
}
