import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

import comfy.model_management
from comfy_api.latest import InputImpl, Types
import folder_paths


CATEGORY = "Short Drama/短剧生成"
RATIOS = ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]
IMAGE_SIZES = {
    "16:9": "1280x720", "9:16": "720x1280", "1:1": "1024x1024",
    "4:3": "1152x864", "3:4": "864x1152", "21:9": "1680x720",
}
SHOT_TEXT_FIELDS = ("scene", "action", "dialogue", "narration", "camera", "first_frame", "last_frame", "video_prompt")


def object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def script_schema():
    character = object_schema({field: {"type": "string"} for field in ("name", "appearance", "voice")})
    shot = object_schema({
        "shot_id": {"type": "integer"}, "duration_seconds": {"type": "integer"},
        **{field: {"type": "string"} for field in SHOT_TEXT_FIELDS},
    })
    return object_schema({
        "title": {"type": "string"}, "logline": {"type": "string"},
        "complete_script": {"type": "string"}, "visual_bible": {"type": "string"},
        "characters": {"type": "array", "items": character},
        "shots": {"type": "array", "items": shot},
    })


class ShortDramaBrief:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "direction": ("STRING", {"multiline": True, "default": "一位侦探在雨夜巷口遇见失散多年的妹妹。悬疑开场，温情反转，结尾留下一个小悬念。"}),
            "project_name": ("STRING", {"default": "我的短剧"}),
            "visual_style": ("STRING", {"multiline": True, "default": "写实电影感，自然表演，克制运镜，统一人物、服装、光线与色调。"}),
            "shot_count": ("INT", {"default": 3, "min": 1, "max": 100}),
            "shot_seconds": ("INT", {"default": 5, "min": 4, "max": 15}),
            "ratio": (RATIOS, {"default": "16:9"}),
        }}

    RETURN_TYPES = ("STRING", "STRING", "SHORT_DRAMA_SPEC")
    RETURN_NAMES = ("剧本任务", "JSON结构", "创作规格")
    FUNCTION = "prepare"
    CATEGORY = CATEGORY
    DESCRIPTION = "把大致方向转成完整剧本和分镜的写作要求。修改这里的镜头数量后，下游自动逐镜处理，不需要复制节点。"

    def prepare(self, direction, project_name, visual_style, shot_count, shot_seconds, ratio):
        if not direction.strip():
            raise ValueError("请填写故事方向、人物、情绪或想表达的主题")
        if shot_count < 1 or not 4 <= shot_seconds <= 15 or ratio not in IMAGE_SIZES:
            raise ValueError("镜头数量至少为 1，H3 每镜支持 4–15 秒整数，请选择有效画幅")
        spec = {"project_name": project_name, "visual_style": visual_style,
                "shot_count": shot_count, "shot_seconds": shot_seconds, "ratio": ratio}
        prompt = f"""你是短剧编剧与分镜导演。将用户的大致方向完善为可以逐镜生成的视频剧本。

用户方向：
{direction}

固定制作要求：{shot_count} 个镜头，每镜 {shot_seconds} 秒，总计划时长 {shot_count * shot_seconds} 秒，画幅 {ratio}。
视觉风格：{visual_style}

先完善人物动机、冲突、起承转合和结尾，再把完整剧本准确拆成指定数量的镜头。
严格按 JSON Schema 输出，不要省略字段，不要 Markdown 围栏。
- complete_script 是完整、连贯、可阅读的中文剧本，包含场景、动作和必要的台词，不是只有摘要。
- visual_bible 固定美术风格、时代、场景和光线；characters 固定每个人的年龄、脸型、发型、服装、标志性特征和声音。无人物题材可用空数组。
- shots 恰好 {shot_count} 项，shot_id 从 1 连续编号，duration_seconds 均为 {shot_seconds}。
- scene 是时空场景，action 是该镜头内可拍摄的动作，camera 是景别与单个连续运镜。
- first_frame 和 last_frame 分别描述同一镜头动作开始前、结束后的两个静态画面，保持人物、道具与空间关系一致。每张都是单个画面，不能是拼贴、多格分镜或前后对照图。
- video_prompt 描述这两个状态之间能在 {shot_seconds} 秒内完成的动作、镜头、表情、环境声；不要一个镜头塞进多个转场或多个地点。
- dialogue 只写实际要说的短台词并标明说话人；narration 只写确有必要的口播。没有则为空字符串。台词加口播必须能在该镜头时长内自然说完。
- 同一个角色跨镜头不得换脸、换衣服、改变年龄。相邻镜头的道具、朝向、动作和情绪要连续。
- 所有视觉提示词用中文，具体可见、可生成。不要让画面出现字幕、水印、分镜编号或说明文字。
- 不声称已有配音、字幕时间戳或完成视频；这里只输出创作方案。
"""
        return prompt, json.dumps(script_schema(), ensure_ascii=False), spec


def validate_script(data, spec):
    if not isinstance(data, dict):
        raise ValueError("剧本 JSON 必须是对象")
    for field in ("title", "logline", "complete_script", "visual_bible"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f"剧本缺少非空文本字段：{field}")
    characters = data.get("characters")
    if not isinstance(characters, list):
        raise ValueError("characters 必须是数组")
    for character in characters:
        if not isinstance(character, dict) or any(not isinstance(character.get(key), str) for key in ("name", "appearance", "voice")):
            raise ValueError("每个角色需要 name、appearance、voice 文本字段")
    shots = data.get("shots")
    if not isinstance(shots, list) or len(shots) != spec["shot_count"]:
        raise ValueError(f"剧本必须包含 {spec['shot_count']} 个镜头；可在 edited_script 中修改 JSON，或调整创作规格")
    for index, shot in enumerate(shots, 1):
        if not isinstance(shot, dict) or type(shot.get("shot_id")) is not int or shot["shot_id"] != index:
            raise ValueError("shot_id 必须从 1 按镜头顺序连续编号")
        duration = shot.get("duration_seconds")
        if type(duration) is not int or not 4 <= duration <= 15:
            raise ValueError(f"镜头 {index} 的时长必须是 4–15 秒整数")
        for field in SHOT_TEXT_FIELDS:
            if not isinstance(shot.get(field), str) or (field not in {"dialogue", "narration"} and not shot[field].strip()):
                raise ValueError(f"镜头 {index} 缺少文本字段：{field}")


class ShortDramaShots:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "spec": ("SHORT_DRAMA_SPEC",),
            "script_json": ("STRING", {"forceInput": True, "lazy": True}),
            "video_mode": (["FL2VA", "Ref2VA"], {"default": "FL2VA"}),
        }, "optional": {
            "edited_script": ("STRING", {"multiline": True, "default": "",
                "tooltip": "可粘贴修改后的完整剧本 JSON。非空时跳过上游 Codex，直接用本稿生成。"}),
        }}

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING", "INT", RATIOS, "STRING",
                    "STRING", "STRING", "STRING", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("人物设定提示词", "首帧/分镜提示词", "尾帧提示词", "视频提示词", "逐镜时长", "视频画幅", "图片尺寸",
                    "人物图保存名", "首帧保存名", "尾帧保存名", "逐镜视频保存名", "剧本保存名", "成片保存名", "最终剧本JSON")
    OUTPUT_IS_LIST = (False, True, True, True, True, False, False, False, True, True, True, False, False, False)
    FUNCTION = "expand"
    CATEGORY = CATEGORY
    DESCRIPTION = "校验剧本并按镜头顺序输出列表，下游生图/生视频节点会自动逐镜执行。可用 edited_script 覆盖并跳过 Codex。"

    def check_lazy_status(self, spec, video_mode, script_json=None, edited_script=""):
        return ["script_json"] if not edited_script.strip() and script_json is None else []

    def expand(self, spec, video_mode, script_json=None, edited_script=""):
        if video_mode not in {"FL2VA", "Ref2VA"}:
            raise ValueError("请选择 FL2VA 或 Ref2VA")
        try:
            data = json.loads(edited_script if edited_script.strip() else script_json)
        except (json.JSONDecodeError, TypeError):
            raise ValueError("剧本不是有效 JSON；请粘贴完整对象，不要包含 Markdown 围栏") from None
        validate_script(data, spec)
        canonical = json.dumps(data, ensure_ascii=False, indent=2)
        project = re.sub(r"[^\w-]+", "_", spec["project_name"], flags=re.UNICODE).strip("_") or "drama"
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
        prefix = f"ShortDrama/{project[:60]}/{digest}"
        characters = "\n".join(f"{item['name']}：{item['appearance']}；声音：{item['voice']}" for item in data["characters"])
        visual = f"视觉风格：{spec['visual_style']}。视觉设定：{data['visual_bible']}。\n人物设定：\n{characters}"
        identity = (f"为以下短剧绘制统一人物与美术设定参考图。{visual}\n"
                    "每个角色清楚展示脸部、发型、服装及标志性特征，角色不要混淆。"
                    "同一角色只有一个固定造型；无人物则绘制核心场景与道具的视觉参考。不要标题、水印或大段文字。")
        first, last, video = [], [], []
        for shot in data["shots"]:
            continuity = f"{visual}\n场景：{shot['scene']}。景别与摄影：{shot['camera']}。"
            first.append(f"参考输入图的人物外观、服装和美术风格，生成一个独立的电影画面。\n{continuity}\n"
                         f"本镜头的开始状态：{shot['first_frame']}\n"
                         "只画单个场景中的这一刻；不要复制参考图的设定图排版，不要拼贴、字幕、水印。")
            last.append(f"参考输入的首帧，生成同一镜头动作结束时的尾帧。\n{continuity}\n"
                        f"开始状态：{shot['first_frame']}\n结束状态：{shot['last_frame']}\n"
                        "保持同一人物、服装、道具、光线与空间关系，仅改变动作、表情和必要的构图。单幅画面，无文字。")
            reference = ("以输入首帧为起点，以输入尾帧为终点，连贯地完成中间动作，不切换场景。"
                         if video_mode == "FL2VA" else
                         "以 <Picture 1> 为该镜头的场景与构图参考，<Picture 2> 为人物外观与服装参考。"
                         "不要复制人物设定图排版，生成单个连续镜头。")
            audio = f"角色声音：{characters}\n"
            if shot["dialogue"].strip():
                audio += f"台词（按说话人自然表达）：{shot['dialogue']}。\n"
            if shot["narration"].strip():
                audio += f"中文旁白：{shot['narration']}。\n"
            if not shot["dialogue"].strip() and not shot["narration"].strip():
                audio += "无对白、无旁白，只保留适合场景的环境声。\n"
            prompt = (f"{reference}\n{continuity}\n动作：{shot['action']}。\n"
                      f"{shot['video_prompt']}\n{audio}时长 {shot['duration_seconds']} 秒。保持角色一致，不加字幕、水印。")
            if len(prompt) > 7000:
                raise ValueError(f"镜头 {shot['shot_id']} 的视频提示词超过 H3 7000 字符上限；请精简角色设定或分镜")
            video.append(prompt)
        ids = [f"shot-{shot['shot_id']:03}" for shot in data["shots"]]
        return (
            identity, first, last, video, [shot["duration_seconds"] for shot in data["shots"]], spec["ratio"], IMAGE_SIZES[spec["ratio"]],
            prefix + "/images/cast", [f"{prefix}/images/{i}-first" for i in ids], [f"{prefix}/images/{i}-last" for i in ids],
            [f"{prefix}/{video_mode}/{i}" for i in ids], prefix + "/script", prefix + f"/{video_mode}/final", canonical,
        )


def run_process(command, timeout=3600):
    comfy.model_management.throw_exception_if_processing_interrupted()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    try:
        while True:
            comfy.model_management.throw_exception_if_processing_interrupted()
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{Path(command[0]).name} 超时")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError(f"{Path(command[0]).name} 失败：{stderr.decode(errors='replace')[-1800:]}")
        return stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


class ShortDramaJoin:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"videos": ("VIDEO",), "fps": ("INT", {"default": 24, "min": 1, "max": 120})}}

    INPUT_IS_LIST = True
    RETURN_TYPES = ("VIDEO",)
    RETURN_NAMES = ("成片",)
    FUNCTION = "join"
    CATEGORY = CATEGORY
    DESCRIPTION = "按分镜顺序硬切拼接所有视频，统一尺寸与帧率，保留原生声音。缺少音轨的片段补静音，不生成配音或字幕。"

    def join(self, videos, fps):
        if not videos:
            raise ValueError("没有可拼接的视频")
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise RuntimeError("请安装 FFmpeg 并确保 ComfyUI 的 PATH 包含 ffmpeg 和 ffprobe")
        rate = fps[0]
        width, height = videos[0].get_dimensions()
        width, height = width + width % 2, height + height % 2
        root = Path(folder_paths.get_temp_directory()) / "short_drama"
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="join-", dir=root))
        destination = directory / "combined.mp4"
        complete = False
        try:
            with tempfile.TemporaryDirectory(prefix="parts-", dir=directory) as temporary:
                work = Path(temporary)
                normalized = []
                for index, video in enumerate(videos):
                    comfy.model_management.throw_exception_if_processing_interrupted()
                    source = work / f"source-{index:04}.mp4"
                    video.save_to(str(source), format=Types.VideoContainer.MP4, codec=Types.VideoCodec.AUTO)
                    metadata = json.loads(run_process([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(source)], timeout=30))
                    duration = video.get_duration()
                    if not math.isfinite(duration) or duration <= 0:
                        raise ValueError(f"镜头 {index + 1} 的实际视频时长无效")
                    has_audio = any(stream.get("codec_type") == "audio" for stream in metadata["streams"])
                    output = work / f"part-{index:04}.mp4"
                    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source)]
                    if not has_audio:
                        command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
                    command += [
                        "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0",
                        "-vf", f"setpts=PTS-STARTPTS,fps={rate},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                                 f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p",
                        "-af", "aresample=48000:async=1:first_pts=0,apad", "-t", str(duration),
                        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", "2",
                        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k",
                        "-video_track_timescale", "90000", str(output),
                    ]
                    run_process(command)
                    normalized.append(output)
                concat = work / "concat.txt"
                concat.write_text("\n".join(f"file '{path.name}'" for path in normalized), encoding="utf-8")
                run_process([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                             "-f", "concat", "-safe", "1", "-i", str(concat), "-c", "copy",
                             "-movflags", "+faststart", str(destination)])
            complete = True
            return (InputImpl.VideoFromFile(str(destination)),)
        finally:
            if not complete:
                shutil.rmtree(directory, ignore_errors=True)


NODE_CLASS_MAPPINGS = {"ShortDramaBrief": ShortDramaBrief, "ShortDramaShots": ShortDramaShots, "ShortDramaJoin": ShortDramaJoin}
NODE_DISPLAY_NAME_MAPPINGS = {
    "ShortDramaBrief": "短剧 · 创作方向", "ShortDramaShots": "短剧 · 分镜展开", "ShortDramaJoin": "短剧 · 按镜头拼接",
}
