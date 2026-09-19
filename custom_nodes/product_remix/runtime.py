"""Local media operations shared by the product-remix nodes."""
from copy import deepcopy
from functools import wraps
import importlib.util
import json
import math
from pathlib import Path
import re
import time

import numpy as np
import torch


ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "product_remix_narration", ROOT.parent / "narration_remix" / "__init__.py")
narration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(narration)
probe = narration.probe
run = narration.run_process
input_path = narration.input_path
executable = narration.executable
FONT = "/System/Library/Fonts/STHeiti Medium.ttc"
SAMPLE_RATE = 48000


def dump(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def snapshot(value):
    if isinstance(value, dict):
        return {k: snapshot(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [snapshot(v) for v in value]
    if isinstance(value, torch.Tensor):
        return {"tensor_shape": list(value.shape)}
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return {"type": type(value).__name__}


def stage(number, name):
    def decorate(function):
        @wraps(function)
        def execute(self, context, *args, **kwargs):
            path = Path(context["run_dir"]) / f"{number:02}-{name}.json"
            started = time.monotonic()
            entry = {"stage": number, "name": name, "status": "running"}
            dump(path, entry)
            try:
                result = function(self, context, *args, **kwargs)
                values = result["result"] if isinstance(result, dict) else result
                status = "skipped" if values and isinstance(values[0], dict) and values[0].get("skipped") else "success"
                entry.update(status=status, seconds=round(time.monotonic() - started, 3), output=snapshot(values))
                dump(path, entry)
                return result
            except Exception as error:
                entry.update(status="failed", error=str(error), seconds=round(time.monotonic() - started, 3))
                dump(path, entry)
                raise
        return execute
    return decorate


def number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{label} 必须是 >= {minimum} 的有限数值")
    return value


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} 必须是非空字符串")
    return value.strip()


def video_info(path):
    info = probe(path)
    stream = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if stream is None:
        raise ValueError(f"文件没有视频流：{Path(path).name}")
    duration = float(stream.get("duration") or info["format"]["duration"])
    number(duration, "视频时长", 0.001)
    return {"duration": duration, "width": stream["width"], "height": stream["height"],
            "has_audio": any(s["codec_type"] == "audio" for s in info["streams"])}


def pcm(path, start=0, duration=None, speed=1):
    command = [executable("ffmpeg"), "-v", "error", "-nostdin", "-ss", str(start)]
    if duration is not None:
        command += ["-t", str(duration)]
    command += ["-i", str(path), "-vn"]
    # Even atempo=1 uses a time-stretch algorithm and can discard/change samples.
    if speed != 1:
        command += ["-af", f"atempo={speed}"]
    command += ["-f", "f32le", "-ar", str(SAMPLE_RATE), "-ac", "2", "pipe:1"]
    values = np.frombuffer(run(command), dtype="<f4").copy()
    if values.size == 0 or not np.isfinite(values).all():
        raise ValueError(f"音频没有有效采样：{Path(path).name}")
    return torch.from_numpy(values.reshape(-1, 2).T.copy())


def audio(data):
    return {"waveform": data.unsqueeze(0), "sample_rate": SAMPLE_RATE}


def save_pcm(data, path):
    narration.write_audio(audio(data), path)


def speech(context, line, name):
    settings = context["spec"].get("voice", {})
    rate = number(settings.get("rate", 200), "语速", 1)
    voice = text(settings.get("name", "Tingting"), "音色")
    directory = Path(context["run_dir"])
    source = directory / f"{name}.txt"
    output = directory / f"{name}.aiff"
    source.write_text(line, encoding="utf-8")
    run([executable("say"), "-v", voice, "-r", str(rate), "-f", str(source), "-o", str(output)], timeout=120)
    return pcm(output)


def scene_boundaries(path, start, end, threshold):
    log = run([executable("ffmpeg"), "-hide_banner", "-nostdin", "-ss", str(start),
               "-t", str(end - start), "-i", str(path), "-an", "-vf",
               f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"], capture_stderr=True)
    changes = [start + float(x) for x in re.findall(r"pts_time:([\d.]+)", log.decode(errors="replace"))]
    boundaries = [start]
    for change in changes:
        if change - boundaries[-1] >= 0.8 and end - change >= 0.8:
            boundaries.append(change)
    return boundaries + [end]


def thumbnail(path, timestamp, destination):
    run([executable("ffmpeg"), "-v", "error", "-nostdin", "-y", "-ss", str(timestamp),
         "-i", str(path), "-frames:v", "1", "-vf", "scale=480:-2", str(destination)], timeout=60)


def calibrate(context, script, candidates):
    if not isinstance(script, dict) or not isinstance(script.get("blocks"), list) or not script["blocks"]:
        raise ValueError("剧本必须有非空 blocks 数组")
    plan = deepcopy(script)
    plan["title"] = text(plan.get("title"), "剧本标题")
    ids = set()
    by_id = {clip["id"]: clip for clip in candidates}
    fps = context["spec"]["video"]["fps"]
    cursor = 0
    for index, block in enumerate(plan["blocks"]):
        block_id = text(block.get("id"), "block.id")
        if block_id in ids:
            raise ValueError(f"重复的 block.id：{block_id}")
        ids.add(block_id)
        source_type = block.get("source_type", "material")
        if source_type not in {"material", "aigc"}:
            raise ValueError(f"不支持的 source_type：{source_type}")
        block["source_type"] = source_type
        clip_id = text(block.get("clip_id"), "block.clip_id")
        clip = by_id.get(clip_id)
        if source_type == "material" and clip is None:
            raise ValueError(f"剧本引用了未通过初筛的片段：{clip_id}")
        available = clip["out"] - clip["in"] if clip else number(block.get("duration"), "AIGC 计划时长", 0.1)
        target = block.get("duration")
        if target is not None:
            number(target, "分镜时长", 0.1)
        mode = block.get("voice_mode", "tts")
        if mode not in {"tts", "file", "original_vocal", "source_audio", "none"}:
            raise ValueError(f"不支持的人声模式：{mode}")
        block["voice_mode"] = mode
        line = block.get("text", "")
        if not isinstance(line, str):
            raise ValueError("台词 text 必须是字符串")
        block["text"] = line.strip()
        if mode in {"tts", "file"}:
            text(line, "旁白")
        if mode == "none" and line.strip():
            raise ValueError("静音分镜的 text 应留空；不要生成没有对应声音的旁白字幕")
        if mode in {"original_vocal", "source_audio"} and clip is None:
            raise ValueError("原声模式需要真实素材片段；AIGC 音轨请以 file 提供")
        block["subtitle_policy"] = block.get("subtitle_policy", "none")
        block["subtitle_mode"] = block.get("subtitle_mode", "generated" if line.strip() else "none")
        data = None
        measured = None
        playback = 1.0
        attempts = []
        if mode == "tts":
            rewrites = block.get("rewrites", [])
            if not isinstance(rewrites, list) or any(not isinstance(x, str) or not x.strip() for x in rewrites):
                raise ValueError("rewrites 必须是已确认的备选台词数组")
            for attempt, candidate_line in enumerate([line] + rewrites[:2]):
                data = speech(context, candidate_line, f"voice-{index:03}-try-{attempt}")
                measured = data.shape[-1] / SAMPLE_RATE
                # Keep all speech, using at most 10% acceleration when the window is short.
                limit = min(target if target is not None else available, available)
                playback = max(1.0, measured / limit)
                attempts.append({"text": candidate_line, "measured_seconds": measured, "playback_rate": playback})
                if playback <= 1.1:
                    block["text"] = candidate_line
                    break
            else:
                raise ValueError(f"分镜 {block_id} 旁白超过可用画面时长，已尝试 {len(attempts)} 版台词；请缩短台词或补充素材")
        elif mode in {"file", "original_vocal"}:
            path = input_path(text(block.get("audio_file"), f"{block_id}.audio_file"))
            audio_start = number(block.get("audio_in", 0), "音频入点")
            data = pcm(path, audio_start, target if mode == "original_vocal" else None)
            measured = data.shape[-1] / SAMPLE_RATE
            playback = max(1.0, measured / min(target if target is not None else available, available))
            if playback > 1.1:
                raise ValueError(f"分镜 {block_id} 音频过长，不能截断旁白；请改写或延长画面")
        elif mode == "source_audio":
            data = pcm(clip["path"], clip["in"], target if target is not None else available)
            measured = data.shape[-1] / SAMPLE_RATE
        duration = target if target is not None else min(measured, available) if measured is not None else available
        frames = math.ceil(duration * fps - 1e-8)
        if frames / fps > available + 1 / fps:
            raise ValueError(f"分镜 {block_id} 时长超过素材范围")
        block.update(start_frame=cursor, frames=frames, duration=frames / fps,
                     measured_voice_seconds=measured, voice_playback_rate=playback, calibration_attempts=attempts)
        if data is not None:
            path = Path(context["run_dir"]) / f"voice-{index:03}.wav"
            save_pcm(data, path)
            block["calibrated_audio"] = str(path)
        cursor += frames
    plan.update(total_frames=cursor, fps=fps, duration=cursor / fps)
    return plan
