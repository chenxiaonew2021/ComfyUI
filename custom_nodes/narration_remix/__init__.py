import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import comfy.model_management
from comfy_api.latest import InputImpl
import folder_paths


CATEGORY = "Narration Remix/解说混剪"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
DEFAULT_SRT = """1
00:00:00,000 --> 00:00:03,000
为什么忙了一天，还是觉得没有进展？

2
00:00:03,000 --> 00:00:06,000
先关掉干扰，只完成眼前最重要的一件事。

3
00:00:06,000 --> 00:00:09,000
明天开始，给自己一段不被打断的时间。
"""
DEFAULT_DIRECTIONS = json.dumps({
    "1": {"keywords": ["城市", "夜景"], "role": "hook"},
    "2": {"keywords": ["人物", "工作"], "role": "evidence"},
    "3": {"keywords": ["日出"], "role": "ending"},
}, ensure_ascii=False, indent=2)
DEFAULT_MANIFEST = json.dumps([
    {"id": "city", "file": "city.mp4", "in": 0, "out": 8, "tags": ["城市", "夜景"]},
    {"id": "working", "file": "work.mp4", "in": 1, "out": 9, "tags": ["人物", "工作"]},
    {"id": "sunrise", "file": "sunrise.mp4", "in": 0, "out": 7, "tags": ["日出"]},
], ensure_ascii=False, indent=2)


def check_interrupt():
    comfy.model_management.throw_exception_if_processing_interrupted()


def run_process(command, timeout=3600, capture_stderr=False):
    check_interrupt()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_interrupt()
            if time.monotonic() >= deadline:
                raise RuntimeError(f"{Path(command[0]).name} 超时，请缩短素材后重试")
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError(f"{Path(command[0]).name} 失败：{stderr.decode(errors='replace')[-1800:]}")
        return stderr if capture_stderr else stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def executable(name):
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"找不到 {name}，请安装 FFmpeg 并确保 ComfyUI 启动环境的 PATH 包含它")
    return path


def probe(path):
    return json.loads(run_process([
        executable("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ], timeout=30))


def input_path(value, base=None):
    root = Path(folder_paths.get_input_directory()).resolve()
    path = ((base or root) / value).resolve()
    if not path.is_relative_to(root):
        raise ValueError("素材必须位于 ComfyUI/input 内；请复制素材到 input/remix/footage")
    if not path.exists():
        raise ValueError(f"文件或目录不存在：{path}")
    return path


def positive(value, label, allow_zero=False):
    number = float(value)
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        raise ValueError(f"{label} 必须是{'非负' if allow_zero else '正'}有限数值")
    return number


def string_list(value, label):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{label} 必须是非空字符串组成的 JSON 数组")
    return [x.strip() for x in value]


def audio_duration(audio):
    data = audio["waveform"]
    if data.ndim != 3 or data.shape[0] != 1 or data.shape[1] < 1 or data.shape[-1] == 0:
        raise ValueError("请连接一条非空 AUDIO；一期不接受音频批次")
    return data.shape[-1] / positive(audio["sample_rate"], "采样率")


def write_audio(audio, path):
    audio_duration(audio)
    data = audio["waveform"][0].detach().cpu().numpy().T
    if not np.isfinite(data).all():
        raise ValueError("音频包含无效采样值")
    pcm = (np.clip(data, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(data.shape[1])
        output.setsampwidth(2)
        output.setframerate(int(audio["sample_rate"]))
        output.writeframes(pcm.tobytes())


def normalization_gain(path):
    # Measure loudness separately: dynamic loudnorm can emit discontinuous PTS.
    # Apply a fixed gain to the original samples, with true-peak headroom.
    log = run_process([
        executable("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(path), "-vn", "-af",
        "aresample=48000,aformat=channel_layouts=stereo,loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
        "-f", "null", "-",
    ], capture_stderr=True).decode(errors="replace")
    match = re.search(r'\{\s*"input_i".*?\}', log, re.DOTALL)
    if not match:
        raise RuntimeError("FFmpeg 未返回响度测量结果")
    measured = json.loads(match.group())
    loudness, peak = float(measured["input_i"]), float(measured["input_tp"])
    if not math.isfinite(loudness) or not math.isfinite(peak):
        return 0.0
    return min(-16 - loudness, -1.5 - peak)


def parse_time(value):
    match = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)[,.](\d{3})", value.strip())
    if not match:
        raise ValueError(f"无效 SRT 时间：{value}")
    hours, minutes, seconds, milliseconds = map(int, match.groups())
    return hours * 3600 + minutes * 60 + seconds + milliseconds / 1000


def parse_srt(text):
    cues = []
    seen = set()
    for block in re.split(r"\n\s*\n", text.lstrip("\ufeff").replace("\r\n", "\n").strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit() or "-->" not in lines[1]:
            raise ValueError("SRT 每段需要序号、起止时间和字幕文字；请粘贴 SRT 正文")
        cue_id = lines[0].strip()
        start, end = map(parse_time, lines[1].split("-->"))
        if cue_id in seen or end <= start or (cues and start < cues[-1]["end"]):
            raise ValueError(f"字幕 {cue_id} 序号重复、时长无效或与上一段重叠，请先校正 SRT")
        text_line = re.sub(r"<[^>]*>", "", "\n".join(lines[2:])).strip()
        if not text_line:
            raise ValueError(f"字幕 {cue_id} 没有文字")
        cues.append({"id": cue_id, "start": start, "end": end, "text": text_line})
        seen.add(cue_id)
    return cues


def srt_timestamp(value):
    total = round(value * 1000)
    seconds, milliseconds = divmod(total, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


class RemixNarrationPlan:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "narration": ("AUDIO",),
            "srt_text": ("STRING", {"multiline": True, "default": DEFAULT_SRT}),
            "directions_json": ("STRING", {"multiline": True, "default": DEFAULT_DIRECTIONS}),
        }}

    RETURN_TYPES = ("REMIX_NARRATION",)
    RETURN_NAMES = ("解说时间轴",)
    FUNCTION = "build"
    CATEGORY = CATEGORY
    DESCRIPTION = "使用真实配音和已校正的 SRT 建立时间轴。方向 JSON 的键是字幕序号，支持 keywords、role、clip_ids。此节点不执行语音识别。"

    def build(self, narration, srt_text, directions_json):
        duration = audio_duration(narration)
        cues = parse_srt(srt_text)
        directions = json.loads(directions_json)
        if not isinstance(directions, dict):
            raise ValueError("选镜方向必须是以字幕序号为键的 JSON 对象")
        unknown = set(directions) - {cue["id"] for cue in cues}
        if unknown:
            raise ValueError(f"选镜方向引用了不存在的字幕序号：{sorted(unknown)}")
        warnings = []
        for cue in cues:
            if cue["end"] > duration + 0.05:
                raise ValueError(f"字幕 {cue['id']} 到 {cue['end']:.2f}s，超过配音 {duration:.2f}s；请使用同一配音的 SRT")
            cue["end"] = min(cue["end"], duration)
            if cue["end"] <= cue["start"]:
                raise ValueError(f"字幕 {cue['id']} 位于配音结束之后")
            direction = directions.get(cue["id"], {})
            if not isinstance(direction, dict):
                raise ValueError(f"字幕 {cue['id']} 的方向必须是 JSON 对象")
            cue["keywords"] = string_list(direction.get("keywords", []), "keywords")
            cue["clip_ids"] = string_list(direction.get("clip_ids", []), "clip_ids")
            cue["role"] = str(direction.get("role", ""))
            if len(cue["text"].replace("\n", "")) > 32:
                warnings.append(f"字幕 {cue['id']} 较长，建议拆成更短的字幕并校正时间")
        return ({"duration": duration, "cues": cues, "warnings": warnings},)


class RemixFootageCatalog:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "folder": ("STRING", {"default": "remix/footage"}),
            "manifest_json": ("STRING", {"multiline": True, "default": DEFAULT_MANIFEST}),
        }}

    RETURN_TYPES = ("REMIX_CATALOG",)
    RETURN_NAMES = ("素材目录",)
    FUNCTION = "load"
    CATEGORY = CATEGORY
    DESCRIPTION = "相对 ComfyUI/input 的目录。素材标注包含 id、file、in、out、tags。同一文件可标多个区间；[] 表示按文件名扫描。"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def load(self, folder, manifest_json):
        directory = input_path(folder)
        if not directory.is_dir():
            raise ValueError("素材目录必须是文件夹")
        manifest = json.loads(manifest_json)
        if not isinstance(manifest, list):
            raise ValueError("素材标注必须是 JSON 数组")
        if not manifest:
            manifest = [{"id": str(p.relative_to(directory)), "file": str(p.relative_to(directory)),
                         "tags": [p.stem]} for p in sorted(directory.rglob("*"))
                        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS]
        if not manifest:
            raise ValueError("素材目录为空；请先放入视频并填写标注")
        catalog = []
        seen = set()
        metadata = {}
        for item in manifest:
            check_interrupt()
            if not isinstance(item, dict) or not isinstance(item.get("file"), str):
                raise ValueError("每条素材标注必须包含 file 字符串")
            path = input_path(item["file"], directory)
            if not path.is_relative_to(directory) or path.suffix.lower() not in VIDEO_EXTENSIONS:
                raise ValueError("素材 file 必须是指定目录中的视频文件")
            clip_id = str(item.get("id", item["file"]))
            if not clip_id or clip_id in seen:
                raise ValueError(f"素材 ID 为空或重复：{clip_id}")
            seen.add(clip_id)
            if path not in metadata:
                metadata[path] = probe(path)
            info = metadata[path]
            video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
            if video is None:
                raise ValueError(f"素材没有视频流：{path.name}")
            duration = positive(video.get("duration") or info["format"].get("duration"), f"{clip_id} 时长")
            start = positive(item.get("in", 0), "in", allow_zero=True)
            end = positive(item.get("out", duration), "out")
            if end <= start or end > duration + 0.05:
                raise ValueError(f"素材 {clip_id} 区间 {start}–{end}s 超出视频时长 {duration:.3f}s")
            catalog.append({"id": clip_id, "path": str(path), "in": start, "out": min(end, duration),
                            "tags": string_list(item.get("tags", [path.stem]), "tags"),
                            "width": video["width"], "height": video["height"]})
        return (catalog,)


def match_score(cue, clip):
    tags = {tag.casefold() for tag in clip["tags"]}
    if cue["keywords"]:
        return len(tags & {word.casefold() for word in cue["keywords"]})
    return sum(tag in cue["text"].casefold() for tag in tags)


class RemixEditDecision:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "narration_plan": ("REMIX_NARRATION",), "catalog": ("REMIX_CATALOG",),
            "max_shot_seconds": ("FLOAT", {"default": 3.5, "min": 0.5, "max": 30, "step": 0.1}),
            "fps": ("INT", {"default": 30, "min": 1, "max": 60}),
            "unmatched_policy": (["error", "fallback"], {"default": "error"}),
        }}

    RETURN_TYPES = ("REMIX_EDL", "STRING")
    RETURN_NAMES = ("剪辑清单", "审阅报告")
    FUNCTION = "plan"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True
    DESCRIPTION = "逐句标签匹配，优先不同片段。error 遇到无匹配时停止；fallback 会记录警告。输出时间、来源区间和字幕以便审阅；不会渲染。"

    def plan(self, narration_plan, catalog, max_shot_seconds, fps, unmatched_policy):
        if not 1 <= fps <= 60 or unmatched_policy not in {"error", "fallback"}:
            raise ValueError("无效帧率或未匹配处理策略")
        max_frames = max(1, round(positive(max_shot_seconds, "镜头时长") * fps))
        total_frames = math.ceil(narration_plan["duration"] * fps)
        warnings = list(narration_plan["warnings"])
        cues = narration_plan["cues"]
        intervals = []
        cursor = 0
        for cue in cues:
            start, end = round(cue["start"] * fps), min(total_frames, round(cue["end"] * fps))
            if start > cursor:
                intervals.append((cursor, start, cue, ""))
            if end > start:
                intervals.append((start, end, cue, cue["text"]))
            else:
                warnings.append(f"字幕 {cue['id']} 短于一帧，未烧录；请加长该字幕")
            cursor = end
        if cursor < total_frames:
            intervals.append((cursor, total_frames, cues[-1], ""))
        by_id = {clip["id"]: clip for clip in catalog}
        used = {clip["id"]: 0 for clip in catalog}
        positions = {clip["id"]: clip["in"] for clip in catalog}
        previous = None
        shots = []
        for begin, end, cue, subtitle in intervals:
            missing = set(cue["clip_ids"]) - set(by_id)
            if missing:
                raise ValueError(f"字幕 {cue['id']} 指定的片段不存在：{sorted(missing)}")
            pool = [by_id[key] for key in cue["clip_ids"]] if cue["clip_ids"] else catalog
            scored = [(match_score(cue, clip), clip) for clip in pool]
            matches = [(score, clip) for score, clip in scored if score > 0 or cue["clip_ids"]]
            if not matches:
                if unmatched_policy == "error":
                    raise ValueError(f"字幕 {cue['id']} 没有匹配素材；请补充 keywords/tags 或指定 clip_ids：{cue['text']}")
                matches = scored
                warnings.append(f"字幕 {cue['id']} 无标签匹配，使用 fallback；需要人工换镜")
            frame = begin
            while frame < end:
                # Spread cuts evenly across the sentence instead of leaving a tiny last shot.
                wanted = math.ceil((end - frame) / math.ceil((end - frame) / max_frames))
                options = []
                for score, clip in matches:
                    capacity = math.floor((clip["out"] - clip["in"]) * fps + 1e-6)
                    if capacity < 1:
                        continue
                    available = math.floor((clip["out"] - positions[clip["id"]]) * fps + 1e-6)
                    take = min(wanted, capacity)
                    reset = available < take
                    options.append((score, clip["id"] != previous, not reset, -used[clip["id"]],
                                    take, clip, reset))
                if not options:
                    raise ValueError(f"字幕 {cue['id']} 的候选片段都短于一帧")
                # Stable ordering makes the same inputs produce the same editing decisions.
                score, _, _, _, take, clip, reset = max(options, key=lambda item: item[:5])
                if reset:
                    positions[clip["id"]] = clip["in"]
                    warnings.append(f"片段 {clip['id']} 可用区间不足，重复使用该区间；建议补充素材")
                if previous == clip["id"]:
                    warnings.append(f"字幕 {cue['id']} 连续使用片段 {clip['id']}，请确认画面节奏")
                source_in = positions[clip["id"]]
                shots.append({"start_frame": frame, "frames": take, "start": frame / fps,
                              "end": (frame + take) / fps, "source": clip["path"], "clip_id": clip["id"],
                              "source_in": source_in, "source_out": source_in + take / fps,
                              "subtitle": subtitle, "cue_id": cue["id"], "role": cue["role"],
                              "match_score": score, "manual": bool(cue["clip_ids"])})
                positions[clip["id"]] += take / fps
                used[clip["id"]] += 1
                previous = clip["id"]
                frame += take
        warnings = list(dict.fromkeys(warnings))
        edl = {"version": 1, "fps": fps, "total_frames": total_frames,
               "audio_duration": narration_plan["duration"], "duration": total_frames / fps,
               "cues": cues, "shots": shots, "warnings": warnings}
        lines = [f"配音 {edl['audio_duration']:.3f}s / 成片 {edl['duration']:.3f}s / {len(shots)} 镜头 / {fps}fps"]
        lines += ["提示：" + warning for warning in warnings]
        for i, shot in enumerate(shots, 1):
            reason = "指定候选" if shot["manual"] else f"标签匹配 {shot['match_score']}"
            lines.append(f"{i:02} | {shot['start']:.2f}–{shot['end']:.2f}s | {shot['clip_id']} "
                         f"源 {shot['source_in']:.2f}–{shot['source_out']:.2f}s | {reason} | "
                         f"{shot['role']} | {shot['subtitle'] or '（字幕间隙）'}")
        report = "\n".join(lines)
        edl["report"] = report
        return {"ui": {"text": [report]}, "result": (edl, report)}


def subtitle_image(text, width, height, font, bottom_margin, path):
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    lines = []
    for paragraph in text.splitlines():
        line = ""
        for char in paragraph:
            if line and draw.textlength(line + char, font=font) > width * 0.86:
                lines.append(line)
                line = ""
            line += char
        if line:
            lines.append(line)
    line_height = round(font.size * 1.4)
    y = height - bottom_margin - len(lines) * line_height
    if y < height * 0.1:
        raise ValueError("字幕过长，超出画面安全区；请拆分 SRT 字幕或减小字号")
    for line in lines:
        x = (width - draw.textlength(line, font=font)) / 2
        draw.text((x, y), line, font=font, fill="white", stroke_width=max(1, font.size // 15),
                  stroke_fill="black", anchor="lt")
        y += line_height
    image.save(path)


class RemixRender:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "edl": ("REMIX_EDL",), "narration": ("AUDIO",),
            "resolution": (["720x1280", "1080x1920", "1280x720", "1920x1080", "1080x1080"],
                           {"default": "720x1280"}),
            "fit": (["pad", "crop"], {"default": "pad"}),
            "burn_subtitles": ("BOOLEAN", {"default": True}),
            "font_path": ("STRING", {"default": "/System/Library/Fonts/STHeiti Medium.ttc"}),
            "font_size": ("INT", {"default": 38, "min": 12, "max": 150}),
            "bottom_margin": ("INT", {"default": 100, "min": 0, "max": 500}),
            "bgm_db": ("FLOAT", {"default": -22, "min": -60, "max": 0, "step": 1}),
            "duck_bgm": ("BOOLEAN", {"default": True}),
        }, "optional": {"bgm": ("AUDIO",)}}

    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("成片", "产物目录")
    FUNCTION = "render"
    CATEGORY = CATEGORY
    DESCRIPTION = "按剪辑清单硬切，原素材静音，叠加解说、可选 BGM 和中文字幕。返回原生 VIDEO 接 SaveVideo。pad 保留全画面；crop 居中裁切。"

    def render(self, edl, narration, resolution, fit, burn_subtitles, font_path, font_size,
               bottom_margin, bgm_db, duck_bgm, bgm=None):
        if resolution not in {"720x1280", "1080x1920", "1280x720", "1920x1080", "1080x1080"} or fit not in {"pad", "crop"}:
            raise ValueError("请选择支持的画幅和缩放方式")
        if abs(audio_duration(narration) - edl["audio_duration"]) > 0.001:
            raise ValueError("配音与剪辑清单时长不同，请将同一个 LoadAudio 同时连接到时间轴和渲染节点")
        if not math.isfinite(bgm_db) or not -60 <= bgm_db <= 0:
            raise ValueError("背景音乐音量需在 -60 到 0 dB 之间")
        width, height = map(int, resolution.split("x"))
        font = ImageFont.truetype(str(Path(font_path).expanduser()), font_size) if burn_subtitles else None
        ffmpeg = executable("ffmpeg")
        root = Path(folder_paths.get_output_directory()) / "NarrationRemix" / "runs"
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="remix-", dir=root))
        (directory / "edit-decision.json").write_text(json.dumps(edl, ensure_ascii=False, indent=2), encoding="utf-8")
        (directory / "review.txt").write_text(edl["report"], encoding="utf-8")
        srt = "\n\n".join(f"{i}\n{srt_timestamp(cue['start'])} --> {srt_timestamp(cue['end'])}\n{cue['text']}"
                           for i, cue in enumerate(edl["cues"], 1)) + "\n"
        (directory / "subtitles.srt").write_text(srt, encoding="utf-8")
        work = directory / "intermediates"
        work.mkdir()
        fps = edl["fps"]
        clip_files = []
        overlays = {}
        for index, shot in enumerate(edl["shots"]):
            check_interrupt()
            source = input_path(shot["source"])
            destination = work / f"shot-{index:05}.mp4"
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                       "-ss", str(shot["source_in"]), "-i", str(source)]
            scale = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
                     if fit == "pad" else f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}")
            filters = f"setpts=PTS-STARTPTS,fps={fps},{scale},setsar=1,tpad=stop_mode=clone:stop_duration={1 / fps}"
            if burn_subtitles and shot["subtitle"]:
                if shot["subtitle"] not in overlays:
                    overlay = work / f"subtitle-{len(overlays):05}.png"
                    subtitle_image(shot["subtitle"], width, height, font, bottom_margin, overlay)
                    overlays[shot["subtitle"]] = overlay
                command += ["-loop", "1", "-framerate", str(fps), "-i", str(overlays[shot["subtitle"]]),
                            "-filter_complex", f"[0:v:0]{filters}[base];[base][1:v:0]overlay=0:0:shortest=1,format=yuv420p[v]",
                            "-map", "[v]"]
            else:
                command += ["-map", "0:v:0", "-vf", filters + ",format=yuv420p"]
            command += ["-an", "-frames:v", str(shot["frames"]), "-c:v", "libx264", "-preset", "fast",
                        "-crf", "20", "-threads", "2", "-video_track_timescale", "90000", str(destination)]
            run_process(command)
            actual = float(probe(destination)["format"]["duration"])
            if abs(actual - shot["frames"] / fps) > 1.1 / fps:
                raise RuntimeError(f"镜头 {index + 1} 可解码时长不足，请调整素材出点：{source.name}")
            clip_files.append(destination)
        concat = work / "concat.txt"
        concat.write_text("\n".join(f"file '{path.name}'" for path in clip_files), encoding="utf-8")
        voice_path = work / "narration.wav"
        write_audio(narration, voice_path)
        duration = edl["duration"]
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "concat", "-safe", "1",
                   "-i", str(concat), "-i", str(voice_path)]
        voice_gain = normalization_gain(voice_path)
        audio_filter = (f"[1:a]aresample=48000,aformat=channel_layouts=stereo,apad,atrim=duration={duration},"
                        f"asetpts=PTS-STARTPTS,volume={voice_gain:.6f}dB[voice]")
        if bgm is not None:
            bgm_path = work / "bgm.wav"
            write_audio(bgm, bgm_path)
            music_gain = normalization_gain(bgm_path)
            command += ["-stream_loop", "-1", "-i", str(bgm_path)]
            fade = min(0.5, duration / 2)
            audio_filter += (f";[2:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={duration},asetpts=PTS-STARTPTS,"
                             f"volume={music_gain + bgm_db:.6f}dB,"
                             f"afade=t=in:d={fade},afade=t=out:st={duration - fade}:d={fade}[music]")
            if duck_bgm:
                audio_filter += ";[voice]asplit=2[main][side];[music][side]sidechaincompress=threshold=0.025:ratio=8:attack=20:release=300[ducked];[main][ducked]amix=inputs=2:duration=first:normalize=0[mix]"
            else:
                audio_filter += ";[voice][music]amix=inputs=2:duration=first:normalize=0[mix]"
            audio_filter += ";[mix]alimiter=limit=0.95:level=0:latency=1[a]"
        else:
            audio_filter += ";[voice]anull[a]"
        output = directory / "render.mp4"
        command += ["-filter_complex", audio_filter, "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
                    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k", "-t", str(duration),
                    "-movflags", "+faststart", str(output)]
        run_process(command)
        shutil.rmtree(work)
        return {"ui": {"text": [str(directory)]}, "result": (InputImpl.VideoFromFile(str(output)), str(directory))}


NODE_CLASS_MAPPINGS = {
    "RemixNarrationPlan": RemixNarrationPlan,
    "RemixFootageCatalog": RemixFootageCatalog,
    "RemixEditDecision": RemixEditDecision,
    "RemixRender": RemixRender,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "RemixNarrationPlan": "混剪 · 解说时间轴",
    "RemixFootageCatalog": "混剪 · 素材目录",
    "RemixEditDecision": "混剪 · 选镜与清单审阅",
    "RemixRender": "混剪 · 字幕配乐渲染",
}
WEB_DIRECTORY = "./web"
