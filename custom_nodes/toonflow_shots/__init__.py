import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import av
from aiohttp import web
from PIL import Image

import folder_paths
import nodes
from comfy_api.latest import InputImpl, Types
from server import PromptServer

from .contract import ShotRequest

ROOT = Path(__file__).resolve().parent
DEFAULT_URL = "http://127.0.0.1:18081"


def h3():
    cls = nodes.NODE_CLASS_MAPPINGS.get("MerlinH3FL2VA")
    if cls is None:
        raise ValueError("请安装本项目的 ComfyUI/custom_nodes/merlin_h3 节点并重启 ComfyUI")
    return sys.modules[cls.__module__]


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = handle.name
    os.replace(temporary, path)


def record_path(request_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
        raise ValueError("request_id 无效")
    return Path(folder_paths.get_user_directory()) / "toonflow_shots/jobs" / f"{request_id}.json"


def reference_path(ref):
    root = Path(folder_paths.get_input_directory() if ref.storage == "input"
                else folder_paths.get_output_directory()).resolve()
    relative = Path(ref.file)
    target = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(root):
        raise ValueError(f"素材 {ref.id} 必须位于 ComfyUI {ref.storage} 目录内")
    if not target.is_file():
        raise ValueError(f"缺少素材 {ref.id}：{ref.storage}/{ref.file}")
    if digest(target) != ref.sha256:
        raise ValueError(f"素材 {ref.id} 内容已变化，请更新素材版本、sha256 和 request_id")
    return target


def prepare_request(data):
    request = ShotRequest.model_validate(data)
    labels, counters, files = {}, {"image": 0, "video": 0, "audio": 0}, []
    totals = {"video": 0.0, "audio": 0.0}
    for ref in request.references:
        target = reference_path(ref)
        if ref.type == "image":
            with Image.open(target) as im:
                if im.format not in ("PNG", "JPEG", "WEBP"):
                    raise ValueError(f"图片 {ref.id} 只支持 PNG/JPEG/WebP")
                mime = Image.MIME[im.format]
                im.verify()
        else:
            with av.open(str(target)) as container:
                streams = container.streams.video if ref.type == "video" else container.streams.audio
                if not streams:
                    raise ValueError(f"素材 {ref.id} 不含 {ref.type} 轨道")
                stream = streams[0]
                seconds = (float(stream.duration * stream.time_base) if stream.duration is not None
                           else float(container.duration or 0) / av.time_base)
                if not 2 <= seconds <= 15:
                    raise ValueError(f"素材 {ref.id} 时长须为 2–15 秒，当前 {seconds:.3f}")
                totals[ref.type] += seconds
            formats = {"video": {".mp4": "video/mp4"}, "audio": {".wav": "audio/wav", ".mp3": "audio/mpeg"}}
            if target.suffix.lower() not in formats[ref.type]:
                raise ValueError("视频参考须为 MP4，音频参考须为 WAV/MP3")
            mime = formats[ref.type][target.suffix.lower()]
        counters[ref.type] += 1
        label = {"image": "Picture", "video": "Video", "audio": "Audio"}[ref.type]
        labels[ref.id] = f"<{label} {counters[ref.type]}>"
        files.append((f"reference_{ref.type}", target, mime))
    if any(value > 15.001 for value in totals.values()):
        raise ValueError("视频、音频参考各类累计时长不能超过 15 秒")

    def replace_tags(text):
        def replace(match):
            kind = {"图": "image", "图片": "image", "视频": "video", "音频": "audio"}[match[1]]
            index = int(match[2])
            if not 1 <= index <= counters[kind]:
                raise ValueError(f"提示词引用 {match[0]} 没有对应素材")
            label = {"image": "Picture", "video": "Video", "audio": "Audio"}[kind]
            return f"<{label} {index}>"
        result = re.sub(r"@(图片|图|视频|音频)(\d+)", replace, text)
        for kind, index in re.findall(r"<(Picture|Video|Audio) (\d+)>", result):
            if not 1 <= int(index) <= counters[{"Picture": "image", "Video": "video", "Audio": "audio"}[kind]]:
                raise ValueError(f"提示词引用 <{kind} {index}> 没有对应素材")
        return result

    bible, shot, continuity = request.bible, request.shot, request.continuity
    parts = [f"生成一个 {request.generation.duration} 秒的短剧镜头。保持以下设定。",
             f"全剧视觉风格：{bible.style}", f"导演要求：{bible.director_notes}"]
    for ref in request.references:
        parts.append(f"参考 {labels[ref.id]}（{ref.id}，用途 {ref.purpose}）：{ref.description}")
    for entity in bible.entities:
        refs = "、".join(labels[item] for item in entity.reference_ids)
        parts.append(f"锁定 {entity.kind} {entity.name}（{entity.id}）：{entity.description}；"
                     f"服装：{entity.wardrobe}；声音：{entity.voice}；以 {refs} 为依据，保持身份和特征。")
    parts.extend([f"镜头内容：{replace_tags(shot.description)}",
                  f"镜头语言：景别 {shot.camera.shot_size}；角度 {shot.camera.angle}；"
                  f"运镜 {shot.camera.movement}；焦段 {shot.camera.lens}",
                  f"站位与动作：{shot.blocking}", f"声音与环境：{shot.sound}"])
    names = {entity.id: entity.name for entity in bible.entities}
    for line in shot.dialogue:
        parts.append(f"{line.kind}，{names[line.speaker_id]}（{line.speaker_id}），"
                     f"逐字台词：{line.text}；语气：{line.delivery}")
    if continuity.mode == "match_previous":
        parts.append(f"接续上一镜 {continuity.previous_shot_id}，以末帧 "
                     f"{labels[continuity.previous_last_frame_id]} 的人物位置、姿态和道具状态作为开场依据。")
    parts.extend([f"开场状态：{continuity.state_in}；目标收尾状态：{continuity.state_out}",
                  f"轴线：{continuity.axis}；画面方向：{continuity.screen_direction}；"
                  f"光线：{continuity.lighting}；时间：{continuity.time_of_day}",
                  f"避免：{bible.negative_prompt}"])
    prompt = "\n".join(parts)
    if len(prompt) > 7000:
        raise ValueError(f"合并提示词为 {len(prompt)} 字符，超过 H3 的 7000 上限；请精简，不会静默截断")
    warnings = ["参考与文本约束不能保证绝对一致；需人工审片后才可作为下一镜依据。"]
    if not bible.entities:
        warnings.append("未绑定角色/场景实体，当前仅有构图参考，不能视为已锁定角色身份。")
    if shot.dialogue and not any(ref.type == "audio" for ref in request.references):
        warnings.append("台词未提供音频参考，跨镜头音色尚未锁定。")
    if continuity.mode == "match_previous":
        warnings.append("Ref2VA 使用末帧作软参考，不保证生成首帧与上一镜末帧逐像素一致。")
    payload = request.model_dump()
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"request": payload, "input_hash": fingerprint, "prompt": prompt, "labels": labels,
            "files": files, "warnings": warnings}


def api_workflow(request):
    graph = json.loads((ROOT / "api/shot-video.json").read_text(encoding="utf-8"))
    graph["1"]["inputs"]["request_json"] = json.dumps(request, ensure_ascii=False)
    return graph


class ShortDramaPrepare:
    @classmethod
    def INPUT_TYPES(cls):
        example = (ROOT / "example_inputs/shot.json").read_text(encoding="utf-8")
        return {"required": {"request_json": ("STRING", {"multiline": True, "default": example})}}

    RETURN_TYPES = ("SHORT_DRAMA_SHOT", "STRING")
    RETURN_NAMES = ("shot", "编排提示词")
    FUNCTION = "prepare"
    CATEGORY = "Toonflow/Short Drama"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def prepare(self, request_json):
        shot = prepare_request(json.loads(request_json))
        path = record_path(shot["request"]["request_id"])
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["input_hash"] != shot["input_hash"]:
                raise ValueError("相同 request_id 不得更换参数或素材；新版本请使用新 request_id 和 take")
            # Resume with the exact compiled prompt even if the compiler has since changed.
            shot["prompt"] = record["prompt"]
        else:
            atomic_json(path, {k: v for k, v in shot.items() if k != "files"})
        return {"ui": {"text": [shot["prompt"], *shot["warnings"]]}, "result": (shot, shot["prompt"])}


class ShortDramaSubmit:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"shot": ("SHORT_DRAMA_SHOT",), "base_url": ("STRING", {"default": DEFAULT_URL})}}

    RETURN_TYPES = ("MERLIN_H3_TASK", "STRING")
    RETURN_NAMES = ("task", "task_id")
    FUNCTION = "submit"
    CATEGORY = "Toonflow/Short Drama"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def submit(self, shot, base_url):
        request = shot["request"]
        verified = prepare_request(request)
        shot["files"] = verified["files"]
        module = h3()
        base_url = module.local_url(base_url)
        path = record_path(request["request_id"])
        record = json.loads(path.read_text(encoding="utf-8"))
        if record.get("base_url", base_url) != base_url:
            raise ValueError("该 request_id 已绑定其他 H3 服务，请勿更换服务后重提")
        record["base_url"] = base_url
        atomic_json(path, record)
        if record.get("task"):
            task = record["task"]
            return {"ui": {"h3_task": [task]}, "result": (task, task["task_id"])}
        generation = request["generation"]
        result = module.submit(base_url, shot["prompt"], generation["duration"], generation["ratio"],
                               generation["inference_steps"], request["request_id"], shot["files"], "Ref2VA")
        record["task"] = result["result"][0]
        atomic_json(path, record)
        return result


def saved_file(path, root):
    return {"filename": path.name, "subfolder": str(path.relative_to(root).parent), "type": "output", "sha256": digest(path)}


class ShortDramaResult:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"video": ("VIDEO",), "shot": ("SHORT_DRAMA_SHOT",), "task": ("MERLIN_H3_TASK",)}}

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "result_json")
    FUNCTION = "save"
    CATEGORY = "Toonflow/Short Drama"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def save(self, video, shot, task):
        request = shot["request"]
        root = Path(folder_paths.get_output_directory())
        directory = root / "ToonflowShots" / request["request_id"]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "video.mp4"
        temporary = directory / "video.partial.mp4"
        video.save_to(str(temporary), format=Types.VideoContainer.MP4, codec=Types.VideoCodec.AUTO)
        os.replace(temporary, target)
        first, last = directory / "first-frame.png", directory / "last-frame.png"
        count, last_frame = 0, None
        with av.open(str(target)) as container:
            stream = container.streams.video[0]
            fps = float(stream.average_rate or 0)
            has_audio = bool(container.streams.audio)
            duration = float(container.duration or 0) / av.time_base
            for frame in container.decode(video=0):
                h3().check_interrupt()
                if count == 0:
                    frame.to_image().save(first)
                last_frame = frame
                count += 1
            if last_frame is None:
                raise ValueError("生成文件中没有可解码的视频帧")
            last_frame.to_image().save(last)
            width, height = last_frame.width, last_frame.height
        warnings = list(shot["warnings"])
        if abs(duration - request["generation"]["duration"]) > 0.25:
            warnings.append("实际时长与请求相差超过 0.25 秒，请在剪辑前检查。")
        ratio_a, ratio_b = map(int, request["generation"]["ratio"].split(":"))
        if abs(width / height - ratio_a / ratio_b) > 0.03:
            warnings.append("实际画幅与请求不一致，请审片。")
        if not has_audio:
            warnings.append("返回视频不含音轨，请检查对白与音效。")
        result = {
            "schema_version": "1.0", "status": "succeeded", "review_status": "required",
            **{k: request[k] for k in ("request_id", "project_id", "episode_id", "shot_id", "take")},
            "input_hash": shot["input_hash"], "h3_task_id": task["task_id"],
            "video": {**saved_file(target, root), "duration": duration, "fps": fps, "width": width,
                      "height": height, "frames": count, "has_audio": has_audio, "bytes": target.stat().st_size},
            "first_frame": saved_file(first, root), "last_frame": saved_file(last, root),
            "bible_version": request["bible"]["version"],
            "entity_versions": {entity["id"]: entity["version"] for entity in request["bible"]["entities"]},
            "requested_state_out": request["continuity"]["state_out"], "warnings": warnings,
        }
        atomic_json(directory / "result.json", result)
        path = record_path(request["request_id"])
        record = json.loads(path.read_text(encoding="utf-8"))
        record["result"] = result
        atomic_json(path, record)
        return {"ui": {"images": [result["video"]], "animated": [True], "short_drama": [result]},
                "result": (InputImpl.VideoFromFile(str(target)), json.dumps(result, ensure_ascii=False, indent=2))}


@PromptServer.instance.routes.post("/short-drama/prepare")
async def prepare_api(request):
    try:
        shot = await asyncio.to_thread(prepare_request, await request.json())
        path = record_path(shot["request"]["request_id"])
        if path.exists() and json.loads(path.read_text())["input_hash"] != shot["input_hash"]:
            return web.json_response({"error": "request_id 已绑定其他输入"}, status=409)
        return web.json_response({"request": shot["request"], "input_hash": shot["input_hash"],
                                  "prompt": api_workflow(shot["request"]), "warnings": shot["warnings"]})
    except (ValueError, OSError, av.error.FFmpegError) as error:
        return web.json_response({"error": str(error)[:2000]}, status=400)


@PromptServer.instance.routes.get("/short-drama/jobs/{request_id}")
async def get_job(request):
    try:
        path = record_path(request.match_info["request_id"])
    except ValueError as error:
        return web.json_response({"error": str(error)}, status=400)
    if not path.exists():
        return web.json_response({"status": "not_started"}, status=404)
    record = json.loads(path.read_text(encoding="utf-8"))
    return web.json_response({"status": "succeeded" if record.get("result") else "submitted" if record.get("task") else "prepared",
                              "input_hash": record["input_hash"], "task": record.get("task"), "result": record.get("result")})


NODE_CLASS_MAPPINGS = {"ShortDramaPrepare": ShortDramaPrepare, "ShortDramaSubmit": ShortDramaSubmit, "ShortDramaResult": ShortDramaResult}
NODE_DISPLAY_NAME_MAPPINGS = {
    "ShortDramaPrepare": "短剧 · 分镜参数与一致性编排", "ShortDramaSubmit": "短剧 · 提交参考视频生成",
    "ShortDramaResult": "短剧 · 视频与连续性结果",
}
