import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import urlsplit
import wave

from PIL import Image
import requests

import comfy.model_management
from comfy_api.latest import InputImpl, Types
import folder_paths


DEFAULT_URL = "http://127.0.0.1:18081"
RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
LOG = logging.getLogger(__name__)


def check_interrupt():
    comfy.model_management.throw_exception_if_processing_interrupted()


def local_url(value):
    root = value.strip().rstrip("/")
    parsed = urlsplit(root)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError("请填写本机 H3 服务根地址，例如 http://127.0.0.1:18081")
    return root


def response_json(response):
    try:
        data = response.json()
    except ValueError:
        raise RuntimeError(f"H3 返回非 JSON 内容（HTTP {response.status_code}）") from None
    if not 200 <= response.status_code < 300:
        detail = data.get("detail") if isinstance(data, dict) else None
        if isinstance(detail, dict):
            message = f"{detail.get('code', 'h3_error')}: {detail.get('message', '请求失败')}"
        elif isinstance(detail, list):
            message = "; ".join(str(item.get("msg", "参数无效")) for item in detail if isinstance(item, dict))
        else:
            message = str(detail or "请求失败")
        raise RuntimeError(f"H3 HTTP {response.status_code}: {message[:500]}")
    if not isinstance(data, dict):
        raise RuntimeError("H3 响应格式异常")
    return data


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            check_interrupt()
            digest.update(chunk)
    return digest.hexdigest()


def image_files(images, role, directory, offset=0):
    if images.ndim != 4 or images.shape[-1] not in (3, 4) or images.shape[0] == 0:
        raise ValueError("参考图片必须是非空 ComfyUI IMAGE")
    files = []
    for index, frame in enumerate(images):
        check_interrupt()
        path = directory / f"{role}-{offset + index}.png"
        pixels = frame.detach().clamp(0, 1).mul(255).round().to(device="cpu").numpy().astype("uint8")
        Image.fromarray(pixels).save(path)
        files.append((role, path, "image/png"))
    return files


def submit(root, prompt, duration, ratio, inference_steps, generation_id, files, channel):
    root = local_url(root)
    if not prompt.strip() or len(prompt) > 7000:
        raise ValueError("提示词须为 1–7000 个字符")
    if not 4 <= duration <= 15 or ratio not in RATIOS or inference_steps < 1:
        raise ValueError("H3 支持 4–15 秒、预设画幅和正整数推理步数")
    request = {
        "model": "MiniMax-H3", "content": [{"type": "text", "text": prompt}],
        "resolution": "768P", "duration": duration, "ratio": ratio,
        "inference_steps": inference_steps,
    }
    identity = {
        "version": 1, "root": root, "channel": channel, "request": request,
        "generation_id": generation_id,
        "media": [{"role": role, "sha256": file_digest(path)} for role, path, _ in files],
    }
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    record_path = Path(folder_paths.get_user_directory()) / "merlin_h3/jobs" / f"{fingerprint}.json"
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))
    else:
        for role, path, mime in files:
            check_interrupt()
            try:
                with path.open("rb") as handle, requests.post(
                    root + "/v2/uploads", files={"file": (path.name, handle, mime)},
                    timeout=(5, 120), allow_redirects=False,
                ) as response:
                    asset = response_json(response).get("url", "")
            except requests.RequestException:
                raise RuntimeError("H3 素材上传失败；请先启动 start-merlin-h3.command 并检查本地服务") from None
            if not isinstance(asset, str) or not re.fullmatch(r"asset://[^/?#]+", asset):
                raise RuntimeError("H3 上传响应缺少有效的 asset:// 地址")
            kind = {"reference_video": "video_url", "reference_audio": "audio_url"}.get(role, "image_url")
            request["content"].append({"type": kind, kind: {"url": asset}, "role": role})
        record = {
            "base_url": root, "created_at": time.time(), "channel": channel,
            "idempotency_key": "comfy-h3-" + fingerprint, "request": request,
        }
        # Persist the exact request before submitting: retrying must reuse asset IDs and the key.
        write_record(record_path, record)

    if not record.get("task_id"):
        if time.time() - record["created_at"] >= 23 * 3600:
            raise RuntimeError(
                f"H3 提交结果未确认且已超过安全重试窗口，请先核对 H3 任务列表。记录：{record_path}；"
                "确认后可用任务编号取回结果，或修改 generation_id 发起新任务。"
            )
        check_interrupt()
        try:
            with requests.post(
                root + "/v2/video_generation", json=record["request"],
                headers={"Idempotency-Key": record["idempotency_key"]},
                timeout=(5, 30), allow_redirects=False,
            ) as response:
                task_id = response_json(response).get("task_id")
        except requests.RequestException:
            raise RuntimeError(
                f"H3 提交未确认；请检查本地服务。保持素材、参数和 generation_id 不变后重试，"
                f"会沿用原请求与幂等键。记录：{record_path}"
            ) from None
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise RuntimeError(f"H3 创建响应缺少有效任务编号，请核对记录：{record_path}")
        record["task_id"] = task_id
        write_record(record_path, record)

    task = {"base_url": root, "task_id": record["task_id"]}
    LOG.info("Merlin H3 %s task_id=%s", channel, task["task_id"])
    return {"ui": {"text": [task["task_id"]], "h3_task": [task]}, "result": (task, task["task_id"])}


def generation_inputs():
    return {
        "prompt": ("STRING", {"multiline": True, "default": "镜头缓慢推进，人物自然转头，保持角色外观一致，电影质感，环境声自然。"}),
        "base_url": ("STRING", {"default": DEFAULT_URL}),
        "duration": ("INT", {"default": 4, "min": 4, "max": 15}),
        "ratio": (RATIOS, {"default": "16:9"}),
        "inference_steps": ("INT", {"default": 5, "min": 1, "max": 2147483647,
                                   "tooltip": "默认 5 步便于快速试跑；正式生成可手动提高步数。"}),
        "generation_id": ("INT", {
            "default": 0, "min": 0, "max": 9007199254740991,
            "tooltip": "相同素材、参数和编号复用已有任务。想重新生成时手动修改编号；这不是模型随机种子。",
        }),
    }


class MerlinH3FL2VA:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": generation_inputs(), "optional": {"first_frame": ("IMAGE",), "last_frame": ("IMAGE",)}}

    RETURN_TYPES = ("MERLIN_H3_TASK", "STRING")
    RETURN_NAMES = ("task", "task_id")
    FUNCTION = "generate"
    CATEGORY = "Merlin H3/video"
    OUTPUT_NODE = True
    DESCRIPTION = "提交 H3 首尾帧视频任务。首帧、尾帧均可选；无图时使用 T2VA。固定 768P，包含原生音频。task 接到‘H3 获取视频’。"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def generate(self, prompt, base_url, duration, ratio, inference_steps, generation_id, first_frame=None, last_frame=None):
        for frame in (first_frame, last_frame):
            if frame is not None and len(frame) != 1:
                raise ValueError("每个首尾帧输入只能有一张图片；请先从 IMAGE batch 中选择一张")
        with tempfile.TemporaryDirectory(prefix="comfy-h3-") as temporary:
            files = []
            for role, frame in (("first_frame", first_frame), ("last_frame", last_frame)):
                if frame is not None:
                    files.extend(image_files(frame, role, Path(temporary)))
            return submit(base_url, prompt, duration, ratio, inference_steps, generation_id, files, "FL2VA")


class MerlinH3Ref2VA(MerlinH3FL2VA):
    @classmethod
    def INPUT_TYPES(cls):
        optional = {f"reference_image_{index}": ("IMAGE",) for index in range(1, 9)}
        optional["reference_video"] = ("VIDEO",)
        # Keep existing socket positions so saved workflows retain their connections.
        optional["reference_image_9"] = ("IMAGE",)
        optional.update({f"reference_video_{index}": ("VIDEO",) for index in range(2, 4)})
        optional.update({f"reference_audio_{index}": ("AUDIO",) for index in range(1, 4)})
        return {"required": generation_inputs(), "optional": optional}

    DESCRIPTION = "H3 全参考生成：最多 9 张图片、3 段视频、3 段音频，合计最多 12 个，可混用；至少一张图片或一段视频。视频、音频每段 2–15 秒，各类累计不超过 15 秒。提示词用 <Picture 1> / <Video 1> / <Audio 1> 引用各类素材。"

    def generate(self, prompt, base_url, duration, ratio, inference_steps, generation_id, reference_video=None, **kwargs):
        batches = [kwargs[f"reference_image_{index}"] for index in range(1, 10)
                   if kwargs.get(f"reference_image_{index}") is not None]
        count = sum(len(batch) for batch in batches)
        videos = [video for video in (reference_video, kwargs.get("reference_video_2"),
                                      kwargs.get("reference_video_3")) if video is not None]
        audios = [kwargs[f"reference_audio_{index}"] for index in range(1, 4)
                  if kwargs.get(f"reference_audio_{index}") is not None]
        if count + len(videos) == 0:
            raise ValueError("Ref2VA 至少需要一张参考图片或一段参考视频，音频不能单独使用")
        if count > 9 or len(videos) > 3 or len(audios) > 3 or count + len(videos) + len(audios) > 12:
            raise ValueError("Ref2VA 最多 9 张图片、3 段视频、3 段音频，所有素材合计最多 12 个")
        video_durations = [video.get_duration() for video in videos]
        audio_durations = []
        for audio in audios:
            waveform, sample_rate = audio["waveform"], audio["sample_rate"]
            if waveform.ndim != 3 or waveform.shape[0] != 1 or waveform.shape[1] < 1 or sample_rate <= 0:
                raise ValueError("每个音频端口需要一个有效的 AUDIO，不能包含多个 batch")
            audio_durations.append(waveform.shape[-1] / sample_rate)
        for label, durations in (("视频", video_durations), ("音频", audio_durations)):
            if any(not 2 <= duration <= 15 for duration in durations) or sum(durations) > 15:
                raise ValueError(f"参考{label}每段须为 2–15 秒，全部参考{label}总时长不超过 15 秒")
        with tempfile.TemporaryDirectory(prefix="comfy-h3-") as temporary:
            directory = Path(temporary)
            files = []
            for batch in batches:
                files.extend(image_files(batch, "reference_image", directory, len(files)))
            for index, video in enumerate(videos):
                check_interrupt()
                path = directory / f"reference-video-{index}.mp4"
                video.save_to(str(path), format=Types.VideoContainer.MP4, codec=Types.VideoCodec.AUTO)
                files.append(("reference_video", path, "video/mp4"))
            for index, audio in enumerate(audios):
                check_interrupt()
                path = directory / f"reference-audio-{index}.wav"
                waveform = audio["waveform"][0].detach().float().clamp(-1, 1)
                pcm = waveform.mul(32767).round().to(device="cpu").numpy().astype("<i2")
                with wave.open(str(path), "wb") as handle:
                    handle.setnchannels(pcm.shape[0])
                    handle.setsampwidth(2)
                    handle.setframerate(audio["sample_rate"])
                    handle.writeframes(pcm.T.tobytes())
                if path.stat().st_size > 15 * 1024 * 1024:
                    raise ValueError("参考音频转为 WAV 后超过 15 MiB，请先降低采样率或声道数")
                files.append(("reference_audio", path, "audio/wav"))
            return submit(base_url, prompt, duration, ratio, inference_steps, generation_id, files, "Ref2VA")


def download_video(root, result_path, task_id):
    if not isinstance(result_path, str) or not re.fullmatch(r"/results/[A-Za-z0-9_-]+", result_path):
        raise RuntimeError(f"H3 成功任务返回了无效的视频地址。task_id={task_id}")
    cache_dir = Path(folder_paths.get_temp_directory()) / "merlin_h3"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(f"{root}/{task_id}".encode()).hexdigest()
    destination = cache_dir / f"{key}.mp4"
    if not destination.is_file():
        temporary = cache_dir / f"{key}.part"
        try:
            with requests.get(root + result_path, stream=True, timeout=(5, 60), allow_redirects=False) as response:
                if response.status_code != 200:
                    raise RuntimeError(f"H3 MP4 下载失败（HTTP {response.status_code}）。task_id={task_id}")
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        check_interrupt()
                        handle.write(chunk)
            video = InputImpl.VideoFromFile(str(temporary))
            video.get_dimensions()
            os.replace(temporary, destination)
        except requests.RequestException:
            raise RuntimeError(f"H3 视频已生成，但下载中断；可用原 task_id 重取。task_id={task_id}") from None
        finally:
            temporary.unlink(missing_ok=True)
    return InputImpl.VideoFromFile(str(destination))


class MerlinH3GetVideo:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "task_id": ("STRING", {"default": "", "tooltip": "单独取回结果时填写；连接 task 后使用连接中的编号。"}),
                "base_url": ("STRING", {"default": DEFAULT_URL}),
                "wait_minutes": ("INT", {"default": 120, "min": 1, "max": 1440}),
            },
            "optional": {"task": ("MERLIN_H3_TASK",)},
        }

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "task_id")
    FUNCTION = "get_video"
    CATEGORY = "Merlin H3/video"
    DESCRIPTION = "等待已提交的 H3 任务，下载含音频的 MP4，输出原生 VIDEO 接到 Save Video。取消本地等待不会取消远端任务。"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def get_video(self, task_id, base_url, wait_minutes, task=None):
        if task is not None:
            task_id, base_url = task["task_id"], task["base_url"]
        root = local_url(base_url)
        task_id = task_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", task_id):
            raise ValueError("请连接提交节点的 task 输出，或填写有效的 H3 task_id")
        deadline = time.monotonic() + wait_minutes * 60
        last_state = None
        while time.monotonic() < deadline:
            check_interrupt()
            try:
                with requests.get(root + "/v2/query/video_generation/" + task_id, timeout=(5, 30), allow_redirects=False) as response:
                    if response.status_code in (408, 429) or response.status_code >= 500:
                        current = None
                    else:
                        current = response_json(response).get("task")
                        if not isinstance(current, dict):
                            raise RuntimeError(f"H3 查询响应缺少 task。task_id={task_id}")
            except requests.RequestException:
                current = None
            if current is not None:
                status = current.get("status")
                if status == "succeeded":
                    path = (current.get("content") or {}).get("url")
                    return (download_video(root, path, task_id), task_id)
                if status in ("failed", "cancelled"):
                    error = (current.get("error") or {}).get("message", status)
                    raise RuntimeError(f"H3 任务 {status}: {error}。task_id={task_id}")
                if status not in ("queued", "running"):
                    raise RuntimeError(f"H3 返回未知任务状态 {status}。task_id={task_id}")
                state = (status, current.get("queue_position"), (current.get("waiting_reason") or {}).get("message"))
            else:
                state = ("查询暂时中断，继续查询原任务", None, None)
            if state != last_state:
                LOG.info("Merlin H3 task_id=%s status=%s queue_position=%s reason=%s", task_id, *state)
                last_state = state
            for _ in range(5):
                check_interrupt()
                if time.monotonic() >= deadline:
                    break
                time.sleep(1)
        raise RuntimeError(f"H3 等待超时；远端任务仍可能继续。使用‘H3 获取视频’继续查询。task_id={task_id}")


NODE_CLASS_MAPPINGS = {
    "MerlinH3FL2VA": MerlinH3FL2VA,
    "MerlinH3Ref2VA": MerlinH3Ref2VA,
    "MerlinH3GetVideo": MerlinH3GetVideo,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "MerlinH3FL2VA": "Merlin H3 · FL2VA 提交",
    "MerlinH3Ref2VA": "Merlin H3 · Ref2VA 提交",
    "MerlinH3GetVideo": "Merlin H3 · 获取视频",
}
