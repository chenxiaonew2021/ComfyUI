from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps
import torch

from comfy_api.latest import InputImpl, Types
import folder_paths

from .runtime import (ROOT, FONT, SAMPLE_RATE, audio, calibrate, dump, executable, input_path,
                      narration, number, pcm, probe, run, scene_boundaries, snapshot, stage,
                      text, thumbnail, video_info)
from .rpc import ProductRemixEulerRPC


CATEGORY = "Product Remix/商品混剪"
CTX = "PRODUCT_REMIX_CONTEXT"


def inputs(**required):
    return {"required": {"context": (CTX,), **{k: (v,) for k, v in required.items()}}}


class ProductRemixInitialize:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"spec_json": ("STRING", {"multiline": True,
                             "default": (ROOT / "example_inputs/demo.json").read_text(encoding="utf-8")})}}

    RETURN_TYPES = (CTX,)
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    DESCRIPTION = "本地商品与制作规格。所有媒体文件相对 ComfyUI/input；运行结果只写项目内 output/ProductRemix。"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def execute(self, spec_json):
        spec = json.loads(spec_json)
        if not isinstance(spec, dict) or not isinstance(spec.get("product"), dict):
            raise ValueError("需要 product 对象")
        text(spec["product"].get("name"), "商品名称")
        if not isinstance(spec.get("materials"), list) or not spec["materials"]:
            raise ValueError("materials 不能为空")
        video = spec.setdefault("video", {})
        video.setdefault("resolution", "720x1280")
        video.setdefault("fps", 30)
        video.setdefault("fit", "pad")
        if video["resolution"] not in {"720x1280", "1080x1920", "1280x720", "1920x1080", "1080x1080"}:
            raise ValueError("不支持的输出尺寸")
        if type(video["fps"]) is not int or video["fps"] not in {24, 25, 30} or video["fit"] not in {"pad", "crop"}:
            raise ValueError("fps 支持 24/25/30；fit 支持 pad/crop")
        spec.setdefault("policy", {})
        spec.setdefault("assets", {})
        root = Path(folder_paths.get_output_directory()) / "ProductRemix/runs"
        root.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="local-", dir=root))
        context = {"spec": spec, "run_dir": str(directory), "run_id": directory.name, "execution": "local"}
        dump(directory / "01-InitializeContext.json", {"stage": 1, "name": "InitializeContext", "status": "success", "output": context})
        return (context,)


class ProductRemixPolicy:
    INPUT_TYPES = classmethod(lambda cls: inputs())
    RETURN_TYPES = ("PRODUCT_REMIX_POLICY",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(2, "TrafficGray")
    def execute(self, context):
        value = {"creative_recall": True, "cover": True, "aigc": False, "super_resolution": False}
        supplied = context["spec"]["policy"]
        unknown = set(supplied) - set(value)
        if unknown:
            raise ValueError(f"未知策略开关：{sorted(unknown)}")
        value.update(supplied)
        if any(type(x) is not bool for x in value.values()):
            raise ValueError("策略开关必须是布尔值")
        return (value,)


class ProductRemixSupplement:
    INPUT_TYPES = classmethod(lambda cls: inputs())
    RETURN_TYPES = ("PRODUCT_REMIX_SOURCES",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(3, "SupplementVideo")
    def execute(self, context):
        output, warnings, seen, ids = [], [], set(), set()
        for item in context["spec"]["materials"]:
            if not isinstance(item, dict):
                raise ValueError("每条素材必须是对象")
            source_id = text(item.get("id"), "素材 id")
            if source_id in ids:
                raise ValueError(f"素材 id 重复：{source_id}")
            ids.add(source_id)
            try:
                path = input_path(text(item.get("file"), "素材 file"))
                if not path.is_file():
                    raise ValueError("不是文件")
            except ValueError as error:
                warnings.append(f"{source_id}: {error}")
                continue
            identity = (str(path), json.dumps(item.get("segments", []), sort_keys=True), item.get("in", 0), item.get("out"))
            if identity in seen:
                warnings.append(f"忽略相同文件和区间：{source_id}")
                continue
            seen.add(identity)
            output.append({**item, "path": str(path)})
        if not output:
            raise ValueError("所有素材均不可用：" + "; ".join(warnings))
        return ({"sources": output, "warnings": warnings},)


class ProductRemixProcessMaterial:
    INPUT_TYPES = classmethod(lambda cls: inputs(sources="PRODUCT_REMIX_SOURCES"))
    RETURN_TYPES = ("PRODUCT_REMIX_CATALOG",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    DESCRIPTION = "探测、按标注/场景切段、抽帧。台词/描述来自输入标注；不执行 ASR 或视觉模型。单素材损坏允许跳过。"

    @stage(4, "ProcessMaterial")
    def execute(self, context, sources):
        clips, warnings, ids = [], list(sources["warnings"]), set()
        for source_index, source in enumerate(sources["sources"]):
            try:
                info = video_info(source["path"])
                start = number(source.get("in", 0), "素材入点")
                end = number(source.get("out", info["duration"]), "素材出点", 0.001)
                if start >= end or end > info["duration"] + 0.05:
                    raise ValueError("素材区间超出可解码视频")
                end = min(end, info["duration"])
                segments = source.get("segments")
                if segments is None:
                    cuts = [start, end]
                    if source.get("scene_detect", False):
                        threshold = number(source.get("scene_threshold", 0.35), "scene_threshold")
                        if threshold > 1:
                            raise ValueError("scene_threshold 最大为 1")
                        cuts = scene_boundaries(source["path"], start, end, threshold)
                    segments = [{"in": a, "out": b} for a, b in zip(cuts, cuts[1:])]
                if not isinstance(segments, list) or not segments:
                    raise ValueError("segments 必须是非空数组")
                local_clips = []
                for index, segment in enumerate(segments):
                    clip_id = segment.get("id", source["id"] if len(segments) == 1 else f"{source['id']}:{index + 1}")
                    text(clip_id, "片段 id")
                    left, right = number(segment.get("in"), "片段入点"), number(segment.get("out"), "片段出点", 0.001)
                    if left < start or right > end or left >= right:
                        raise ValueError(f"{clip_id} 区间越界")
                    tags = narration.string_list(segment.get("tags", source.get("tags", [])), "tags")
                    image = Path(context["run_dir"]) / f"frame-{source_index:03}-{index:03}.jpg"
                    thumbnail(source["path"], (left + right) / 2, image)
                    local_clips.append({"id": clip_id, "path": source["path"], "in": left, "out": right,
                                        **info, "tags": tags, "frame": str(image),
                                        "description": segment.get("description", source.get("description", "")),
                                        "narration": segment.get("narration", source.get("narration", "")),
                                        "transcript": segment.get("transcript", source.get("transcript", "")),
                                        "annotation_source": "provided", "source_id": source["id"]})
                new_ids = [x["id"] for x in local_clips]
                if len(set(new_ids)) != len(new_ids) or ids.intersection(new_ids):
                    raise ValueError("片段 ID 重复")
                ids.update(new_ids)
                clips.extend(local_clips)
            except (ValueError, RuntimeError) as error:
                warnings.append(f"跳过 {source['id']}：{error}")
        if not clips:
            raise ValueError("所有视频处理失败：" + "; ".join(warnings))
        return ({"clips": clips, "warnings": warnings},)


class ProductRemixSellingPoints:
    INPUT_TYPES = classmethod(lambda cls: inputs(catalog="PRODUCT_REMIX_CATALOG"))
    RETURN_TYPES = ("PRODUCT_REMIX_SELLING",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(5, "PrepareSellingPoints")
    def execute(self, context, catalog):
        product = context["spec"]["product"]
        points = narration.string_list(product.get("selling_points", []), "selling_points")
        if not points:
            raise ValueError("请提供已确认的商品卖点；本地节点不会编造功效")
        return ({"points": points, "audiences": narration.string_list(product.get("audiences", []), "audiences"),
                 "available_clip_count": len(catalog["clips"])},)


class ProductRemixCreativeRecall:
    INPUT_TYPES = classmethod(lambda cls: inputs(catalog="PRODUCT_REMIX_CATALOG", selling="PRODUCT_REMIX_SELLING", policy="PRODUCT_REMIX_POLICY"))
    RETURN_TYPES = ("PRODUCT_REMIX_CREATIVE",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(6, "PrepareCreativeRecall")
    def execute(self, context, catalog, selling, policy):
        if not policy["creative_recall"]:
            return ({"skipped": True, "reason": "创意召回关闭", "items": []},)
        terms = set(selling["points"] + selling["audiences"] + [tag for c in catalog["clips"] for tag in c["tags"]])
        ranked = []
        for item in context["spec"].get("creative_library", []):
            score = sum(tag in terms for tag in item.get("tags", []))
            if score:
                ranked.append({**item, "score": score})
        ranked.sort(key=lambda x: -x["score"])
        return ({"items": ranked[:3], "warning": "本地创意库无匹配，使用顺序叙事" if not ranked else ""},)


def script_schema():
    properties = {"id": {"type": "string"}, "clip_id": {"type": "string"},
                  "text": {"type": "string"}, "voice_mode": {"type": "string", "enum": ["tts"]}}
    return {"type": "object", "additionalProperties": False, "required": ["title", "blocks"], "properties": {
        "title": {"type": "string"}, "blocks": {"type": "array", "minItems": 1, "items": {
            "type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}}}}


class ProductRemixCoarseFilter:
    INPUT_TYPES = classmethod(lambda cls: inputs(catalog="PRODUCT_REMIX_CATALOG", selling="PRODUCT_REMIX_SELLING", creative="PRODUCT_REMIX_CREATIVE", policy="PRODUCT_REMIX_POLICY"))
    RETURN_TYPES = ("PRODUCT_REMIX_CANDIDATES", "STRING", "STRING")
    RETURN_NAMES = ("候选片段", "剧本任务", "JSON结构")
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(7, "ScriptCoarseFilter")
    def execute(self, context, catalog, selling, creative, policy):
        product = context["spec"]["product"]
        terms = product.get("tags", []) + selling["points"]
        ranked = []
        for clip in catalog["clips"]:
            if clip["out"] - clip["in"] < 0.5:
                continue
            haystack = " ".join(clip["tags"]) + " " + clip["description"]
            score = sum(term in haystack for term in terms)
            if terms and not score:
                continue
            ranked.append({**clip, "score": score})
        ranked.sort(key=lambda x: -x["score"])
        if not ranked:
            raise ValueError("没有与商品标签匹配的片段，请补充准确标签或素材")
        result = {"clips": ranked, "creative": creative["items"], "aigc_allowed": policy["aigc"], "warnings": catalog["warnings"]}
        prompt = ("为以下已确认商品及素材编写混剪剧本。仅使用候选 clip_id；台词简短，能够在片段时长内说完。"
                  "不能杜撰卖点、价格或功效。输出 JSON，不要 Markdown。每块 voice_mode 为 tts。\n" +
                  json.dumps({"product": product, "selling": selling, "creative": creative["items"],
                              "clips": [{k: c[k] for k in ("id", "in", "out", "description", "narration", "tags")} for c in ranked]}, ensure_ascii=False))
        return result, prompt, json.dumps(script_schema(), ensure_ascii=False)


class ProductRemixScriptPlan:
    @classmethod
    def INPUT_TYPES(cls):
        value = inputs(candidates="PRODUCT_REMIX_CANDIDATES")
        value["optional"] = {"script_json": ("STRING", {"multiline": True, "default": ""})}
        return value

    RETURN_TYPES = ("PRODUCT_REMIX_PLAN",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    DESCRIPTION = "优先使用连接的结构化剧本，其次 spec.script，否则用候选片段已确认的 narration 组装模板。逐句真实 TTS 测时，不按字数猜时间。"

    @stage(8, "ScriptPlan")
    def execute(self, context, candidates, script_json=""):
        script = json.loads(script_json) if script_json.strip() else context["spec"].get("script")
        if script is None:
            clips = candidates["clips"][:3]
            order = {item["id"]: i for i, item in enumerate(context["spec"]["materials"])}
            clips = sorted(clips, key=lambda clip: (order[clip["source_id"]], clip["in"]))
            script = {"title": context["spec"]["product"]["name"], "blocks": [
                {"id": f"b{i + 1}", "clip_id": clip["id"], "voice_mode": "tts",
                 "text": text(clip["narration"], f"片段 {clip['id']} 的已确认旁白")}
                for i, clip in enumerate(clips)]}
        if any(b.get("source_type") == "aigc" for b in script.get("blocks", [])) and not candidates["aigc_allowed"]:
            raise ValueError("剧本包含 AIGC 分镜，但 policy.aigc 未开启")
        plan = calibrate(context, script, candidates["clips"])
        plan["planning_backend"] = "provided_script" if script_json.strip() or context["spec"].get("script") else "local_template"
        plan["warnings"] = candidates["warnings"]
        return (plan,)


class ProductRemixCover:
    @classmethod
    def INPUT_TYPES(cls):
        value = inputs(plan="PRODUCT_REMIX_PLAN", catalog="PRODUCT_REMIX_CATALOG", policy="PRODUCT_REMIX_POLICY")
        value["optional"] = {"cover_image": ("IMAGE",)}
        return value

    RETURN_TYPES = ("PRODUCT_REMIX_COVER",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(9, "CoverDesign")
    def execute(self, context, plan, catalog, policy, cover_image=None):
        if not policy["cover"]:
            return ({"skipped": True, "reason": "封面关闭"},)
        width, height = map(int, context["spec"]["video"]["resolution"].split("x"))
        try:
            if cover_image is not None:
                image = Image.fromarray(np.clip(cover_image[0].cpu().numpy() * 255, 0, 255).astype("uint8"))
            else:
                selected = next((c for c in catalog["clips"] if c["id"] == plan["blocks"][0]["clip_id"]), catalog["clips"][0])
                image = Image.open(selected["frame"]).convert("RGB")
            image = ImageOps.pad(image, (width, height), color="black")
            draw = ImageDraw.Draw(image)
            font = ImageFont.truetype(context["spec"].get("font_path", FONT), max(20, width // 20))
            title = plan["title"]
            lines, line = [], ""
            for char in title:
                if draw.textlength(line + char, font=font) > width * 0.86:
                    lines.append(line)
                    line = ""
                line += char
            lines.append(line)
            y = round(height * 0.12)
            for line in lines:
                draw.text(((width - draw.textlength(line, font=font)) / 2, y), line, font=font,
                          fill="white", stroke_width=2, stroke_fill="black")
                y += round(font.size * 1.4)
            path = Path(context["run_dir"]) / "cover.png"
            image.save(path)
            return ({"path": str(path), "duration": min(number(context["spec"].get("cover_seconds", 0.6), "封面时长"), plan["duration"]), "backend": "local_frame" if cover_image is None else "provided_image"},)
        except (OSError, ValueError) as error:
            return ({"skipped": True, "reason": f"封面生成失败，按原流程降级：{error}"},)


class ProductRemixAIShoot:
    @classmethod
    def INPUT_TYPES(cls):
        value = inputs(plan="PRODUCT_REMIX_PLAN")
        value["optional"] = {"generated_video": ("VIDEO",)}
        return value

    RETURN_TYPES = ("PRODUCT_REMIX_GENERATED",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    DESCRIPTION = "接入已有 H3/其他生成节点的 VIDEO 或 assets.aigc 本地结果。此适配节点本身不请求远程模型。"

    @stage(10, "AIShoot")
    def execute(self, context, plan, generated_video=None):
        required = {b["clip_id"] for b in plan["blocks"] if b["source_type"] == "aigc"}
        if not required:
            return ({"skipped": True, "reason": "剧本没有 AIGC 分镜", "assets": {}},)
        mapping = {}
        if generated_video is not None:
            if len(required) != 1:
                raise ValueError("一个 VIDEO 输入只能填充一个 AIGC ID；多结果请使用 assets.aigc 映射")
            directory = Path(folder_paths.get_input_directory()) / "product-remix/generated" / context["run_id"]
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "generated.mp4"
            generated_video.save_to(str(path), format=Types.VideoContainer.MP4, codec=Types.VideoCodec.AUTO)
            mapping[next(iter(required))] = str(path)
        for key in required:
            if key not in mapping:
                value = context["spec"]["assets"].get("aigc", {}).get(key)
                if not value:
                    raise ValueError(f"等待 AIGC 结果：{key}。请连接 VIDEO 或填写 assets.aigc")
                mapping[key] = str(input_path(value))
            video_info(mapping[key])
        return ({"assets": mapping, "backend": "provided_video"},)


class ProductRemixResolve:
    INPUT_TYPES = classmethod(lambda cls: inputs(plan="PRODUCT_REMIX_PLAN", catalog="PRODUCT_REMIX_CATALOG", generated="PRODUCT_REMIX_GENERATED"))
    RETURN_TYPES = ("PRODUCT_REMIX_REQUIREMENT",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(11, "ResolveVisualSources")
    def execute(self, context, plan, catalog, generated):
        result = deepcopy(plan)
        by_id = {c["id"]: c for c in catalog["clips"]}
        for block in result["blocks"]:
            if block["source_type"] == "material":
                clip = by_id[block["clip_id"]]
                block.update(source=clip["path"], source_in=clip["in"], source_limit=clip["out"])
            else:
                path = generated["assets"][block["clip_id"]]
                block.update(source=path, source_in=0, source_limit=video_info(path)["duration"])
        return (result,)


class ProductRemixValidate:
    INPUT_TYPES = classmethod(lambda cls: inputs(requirement="PRODUCT_REMIX_REQUIREMENT"))
    RETURN_TYPES = ("PRODUCT_REMIX_VALIDATED",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(12, "ValidateRequirement")
    def execute(self, context, requirement):
        cursor = 0
        for block in requirement["blocks"]:
            if block["start_frame"] != cursor or block["frames"] <= 0:
                raise ValueError("时间轴有空洞、重叠或无效帧数")
            info = video_info(input_path(block["source"]))
            if block["source_in"] + block["duration"] > min(block["source_limit"], info["duration"]) + 1 / requirement["fps"]:
                raise ValueError(f"分镜 {block['id']} 超过源视频长度")
            if block["subtitle_policy"] not in {"none", "erase"} or block["subtitle_mode"] not in {"none", "source", "generated"}:
                raise ValueError("无效字幕策略")
            if block["subtitle_policy"] == "erase" and block["subtitle_mode"] == "source":
                raise ValueError("不能同时擦除和依赖源字幕")
            if block["voice_mode"] in {"tts", "file"} and block["subtitle_mode"] == "source":
                raise ValueError("新旁白不能沿用未经对齐的原视频字幕")
            if block["voice_mode"] == "source_audio" and not info["has_audio"]:
                raise ValueError("原视频没有音轨")
            cursor += block["frames"]
        if cursor != requirement["total_frames"]:
            raise ValueError("总帧数不一致")
        return ({**requirement, "validated": True},)


class ProductRemixVisuals:
    INPUT_TYPES = classmethod(lambda cls: inputs(requirement="PRODUCT_REMIX_VALIDATED"))
    RETURN_TYPES = ("PRODUCT_REMIX_VISUALS",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(13, "PrepareVisuals")
    def execute(self, context, requirement):
        blocks = deepcopy(requirement["blocks"])
        for block in blocks:
            if block["subtitle_policy"] == "erase":
                path = context["spec"]["assets"].get("erased", {}).get(block["clip_id"])
                if not path:
                    raise ValueError(f"缺少 {block['clip_id']} 的真实清字幕结果；请填写 assets.erased")
                path = input_path(path)
                info = video_info(path)
                if block["source_in"] + block["duration"] > info["duration"] + 1 / requirement["fps"]:
                    raise ValueError("清字幕结果与原视频时间轴不一致")
                block["source"] = str(path)
                block["visual_processing"] = "provided_subtitle_erasure"
        return ({"blocks": blocks, "fps": requirement["fps"], "total_frames": requirement["total_frames"]},)


class ProductRemixVoices:
    INPUT_TYPES = classmethod(lambda cls: inputs(requirement="PRODUCT_REMIX_VALIDATED"))
    RETURN_TYPES = ("PRODUCT_REMIX_VOICES",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(14, "PrepareVoices")
    def execute(self, context, requirement):
        parts, cues = [], []
        fps = requirement["fps"]
        for block in requirement["blocks"]:
            samples = block["frames"] * (SAMPLE_RATE // fps)
            segment = torch.zeros((2, samples), dtype=torch.float32)
            if block.get("calibrated_audio"):
                data = pcm(block["calibrated_audio"], speed=block["voice_playback_rate"])
                if data.shape[-1] > samples + SAMPLE_RATE // fps:
                    raise ValueError(f"分镜 {block['id']} 校准后仍会截断声音")
                segment[:, :min(samples, data.shape[-1])] = data[:, :samples]
            parts.append(segment)
            if block["subtitle_mode"] == "generated" and block["text"]:
                voice_seconds = (block["measured_voice_seconds"] or block["duration"]) / block["voice_playback_rate"]
                cues.append({"id": block["id"], "start": block["start_frame"] / fps,
                             "end": block["start_frame"] / fps + min(block["duration"], voice_seconds), "text": block["text"]})
        narration_audio = audio(torch.cat(parts, dim=1))
        path = Path(context["run_dir"]) / "voice-track.wav"
        narration.write_audio(narration_audio, path)
        return ({"audio": narration_audio, "cues": cues, "path": str(path),
                 "total_frames": requirement["total_frames"], "fps": fps, "alignment": "sentence audio boundaries"},)


class ProductRemixBGM:
    INPUT_TYPES = classmethod(lambda cls: inputs(requirement="PRODUCT_REMIX_VALIDATED"))
    RETURN_TYPES = ("PRODUCT_REMIX_BGM",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(15, "PrepareBGM")
    def execute(self, context, requirement):
        settings = context["spec"].get("bgm", {})
        path = settings.get("file")
        if not path:
            terms = set(requirement.get("bgm_tags", context["spec"]["product"].get("tags", [])))
            ranked = sorted(((len(terms.intersection(x.get("tags", []))), x["file"])
                             for x in settings.get("library", [])), key=lambda x: (-x[0], x[1]))
            if ranked and ranked[0][0]:
                path = ranked[0][1]
        if not path:
            return ({"skipped": True, "reason": "未配置或未匹配背景音乐", "audio": None},)
        source = input_path(path)
        return ({"audio": audio(pcm(source)), "file": str(source), "db": settings.get("db", -22), "duck": settings.get("duck", True)},)


class ProductRemixAggregate:
    INPUT_TYPES = classmethod(lambda cls: inputs(requirement="PRODUCT_REMIX_VALIDATED", visuals="PRODUCT_REMIX_VISUALS", voices="PRODUCT_REMIX_VOICES", bgm="PRODUCT_REMIX_BGM", cover="PRODUCT_REMIX_COVER"))
    RETURN_TYPES = ("PRODUCT_REMIX_PREPARED",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(16, "PrepareRemix")
    def execute(self, context, requirement, visuals, voices, bgm, cover):
        for branch in (visuals, voices):
            if branch["fps"] != requirement["fps"] or branch["total_frames"] != requirement["total_frames"]:
                raise ValueError("画面和声音分支不属于同一个时间轴")
        return ({"requirement": requirement, "visuals": visuals, "voices": voices, "bgm": bgm, "cover": cover},)


class ProductRemixBuildTimeline:
    INPUT_TYPES = classmethod(lambda cls: inputs(prepared="PRODUCT_REMIX_PREPARED"))
    RETURN_TYPES = ("PRODUCT_REMIX_TIMELINE", "STRING")
    RETURN_NAMES = ("合成时间轴", "审阅清单")
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(17, "BuildCloudEdit")
    def execute(self, context, prepared):
        requirement = prepared["requirement"]
        fps = requirement["fps"]
        cues = prepared["voices"]["cues"]
        shots, tracks = [], []
        by_id = {c["id"]: c for c in cues}
        for block in prepared["visuals"]["blocks"]:
            start, end = block["start_frame"], block["start_frame"] + block["frames"]
            cue = by_id.get(block["id"])
            subtitle_end = min(end, max(start + 1, round(cue["end"] * fps))) if cue else start
            boundaries = sorted({start, end, subtitle_end})
            for left, right in zip(boundaries, boundaries[1:]):
                if left == right:
                    continue
                source_in = block["source_in"] + (left - start) / fps
                shots.append({"start_frame": left, "frames": right - left, "start": left / fps, "end": right / fps,
                              "source": block["source"], "source_in": source_in, "source_out": source_in + (right - left) / fps,
                              "clip_id": block["clip_id"], "cue_id": block["id"], "subtitle": cue["text"] if cue and left < subtitle_end else "",
                              "role": block.get("role", ""), "manual": True, "match_score": 0})
            tracks.append({"block_id": block["id"], "voice_mode": block["voice_mode"], "start": start / fps,
                           "end": end / fps, "source": block["source"], "source_in": block["source_in"],
                           "audio": block.get("calibrated_audio"), "voice_playback_rate": block["voice_playback_rate"]})
        report = "\n".join(f"{b['id']} | {b['start_frame']/fps:.3f}–{(b['start_frame']+b['frames'])/fps:.3f}s | {b['clip_id']} | {b['voice_mode']} | {b['text']}" for b in requirement["blocks"])
        edl = {"version": 1, "fps": fps, "total_frames": requirement["total_frames"], "duration": requirement["duration"],
               "audio_duration": requirement["duration"], "cues": cues, "shots": shots,
               "warnings": requirement["warnings"], "report": report}
        timeline = {"edl": edl, "voice": prepared["voices"]["audio"], "bgm": prepared["bgm"], "cover": prepared["cover"]}
        dump(Path(context["run_dir"]) / "timeline.json", {"edl": edl, "video_voice_tracks": tracks,
             "subtitle_track": cues, "bgm_track": snapshot(prepared["bgm"]), "cover_track": prepared["cover"], "renderer": "local_ffmpeg"})
        return {"ui": {"text": [report]}, "result": (timeline, report)}


class ProductRemixRender:
    INPUT_TYPES = classmethod(lambda cls: inputs(timeline="PRODUCT_REMIX_TIMELINE"))
    RETURN_TYPES = ("PRODUCT_REMIX_RENDERED",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(18, "SubmitCloudEdit")
    def execute(self, context, timeline):
        settings = context["spec"]["video"]
        bgm = timeline["bgm"]
        rendered = narration.RemixRender().render(
            timeline["edl"], timeline["voice"], settings["resolution"], settings["fit"], True,
            context["spec"].get("font_path", FONT), int(settings["resolution"].split("x")[0]) // 19,
            int(settings["resolution"].split("x")[1]) // 13, bgm.get("db", -22), bgm.get("duck", True), bgm=bgm["audio"])
        source = Path(rendered["result"][1]) / "render.mp4"
        cover = timeline["cover"]
        output = Path(context["run_dir"]) / "render.mp4"
        fps = timeline["edl"]["fps"]
        # One-frame subtitle gaps can leave discontinuous PTS after MP4 concatenation.
        # EDL frame indices are authoritative for both cover and no-cover exports.
        command = [executable("ffmpeg"), "-v", "error", "-nostdin", "-y", "-i", str(source)]
        if cover.get("path") and cover["duration"] > 0:
            frames = round(cover["duration"] * fps)
            command += ["-loop", "1", "-framerate", str(fps), "-i", cover["path"], "-filter_complex",
                        f"[0:v]setpts=N/({fps}*TB)[base];[base][1:v]overlay=0:0:enable='lt(n,{frames})':shortest=1[v]", "-map", "[v]"]
        else:
            command += ["-vf", f"setpts=N/({fps}*TB)", "-map", "0:v:0"]
        command += ["-map", "0:a:0", "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-threads", "2", "-c:a", "copy",
                    "-r", str(fps), "-fps_mode", "cfr", "-video_track_timescale", "90000",
                    "-frames:v", str(timeline["edl"]["total_frames"]), "-movflags", "+faststart", str(output)]
        run(command)
        return ({"path": str(output), "duration": timeline["edl"]["duration"], "fps": timeline["edl"]["fps"],
                 "total_frames": timeline["edl"]["total_frames"], "render_artifacts": str(source.parent), "backend": "local_ffmpeg"},)


class ProductRemixSuperResolution:
    INPUT_TYPES = classmethod(lambda cls: inputs(rendered="PRODUCT_REMIX_RENDERED", policy="PRODUCT_REMIX_POLICY"))
    RETURN_TYPES = ("VIDEO", "PRODUCT_REMIX_FINAL")
    RETURN_NAMES = ("成片", "验证信息")
    FUNCTION = "execute"
    CATEGORY = CATEGORY

    @stage(19, "SuperResolution")
    def execute(self, context, rendered, policy):
        result = dict(rendered)
        if policy["super_resolution"]:
            value = context["spec"]["assets"].get("super_resolution")
            if not value:
                raise ValueError("超分已开启，请提供 assets.super_resolution 的真实结果文件")
            source = input_path(value)
            info = video_info(source)
            if abs(info["duration"] - rendered["duration"]) > 1 / rendered["fps"]:
                raise ValueError("超分结果时长与合成视频不同")
            output = Path(context["run_dir"]) / "enhanced.mp4"
            run([executable("ffmpeg"), "-v", "error", "-nostdin", "-y", "-i", str(source), "-i", rendered["path"],
                 "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-t", str(rendered["duration"]),
                 "-movflags", "+faststart", str(output)])
            result.update(path=str(output), enhancement="provided_result")
        else:
            result["enhancement"] = "disabled"
        info = video_info(result["path"])
        if not info["has_audio"] or abs(info["duration"] - result["duration"]) > 1 / result["fps"]:
            raise ValueError("成片音轨缺失或时长不一致")
        result["media"] = info
        return InputImpl.VideoFromFile(result["path"]), result


class ProductRemixReport:
    INPUT_TYPES = classmethod(lambda cls: inputs(final="PRODUCT_REMIX_FINAL", saved_video="VIDEO"))
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("运行报告",)
    FUNCTION = "execute"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True

    def execute(self, context, final, saved_video):
        directory = Path(context["run_dir"])
        stages = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("[0-9][0-9]-*.json"))]
        if len(stages) != 19 or any(s["status"] not in {"success", "skipped"} for s in stages):
            raise ValueError("阶段记录不完整，不能标记任务成功")
        if abs(saved_video.get_duration() - final["duration"]) > 1 / final["fps"]:
            raise ValueError("SaveVideo 返回的视频时长不一致")
        result = {"status": "success", "execution": "local", "run_id": context["run_id"], "final": final,
                  "stages": [{k: s[k] for k in ("stage", "name", "status")} for s in stages],
                  "remote_rpc_tested": False, "published": False}
        dump(directory / "result.json", result)
        report = f"本地成片完成：{final['media']['width']}×{final['media']['height']}，{final['duration']:.3f}s，{final['fps']}fps。\n19 阶段已记录（含明确跳过的可选阶段）。\n产物：{directory}\n内部 RPC 未联调；未发布。"
        (directory / "report.txt").write_text(report, encoding="utf-8")
        return {"ui": {"text": [report]}, "result": (report,)}


NODE_CLASS_MAPPINGS = {cls.__name__: cls for cls in (
    ProductRemixInitialize, ProductRemixPolicy, ProductRemixSupplement, ProductRemixProcessMaterial,
    ProductRemixSellingPoints, ProductRemixCreativeRecall, ProductRemixCoarseFilter, ProductRemixScriptPlan,
    ProductRemixCover, ProductRemixAIShoot, ProductRemixResolve, ProductRemixValidate, ProductRemixVisuals,
    ProductRemixVoices, ProductRemixBGM, ProductRemixAggregate, ProductRemixBuildTimeline,
    ProductRemixRender, ProductRemixSuperResolution, ProductRemixReport, ProductRemixEulerRPC)}
_titles = ["01 初始化商品与任务", "02 本地策略开关", "03 补充视频素材", "04 处理视频素材", "05 整理卖点和受众",
           "06 召回创意范式", "07 片段初筛", "08 剧本与旁白校准", "09 封面设计", "10 AIGC 资源接入",
           "11 回填视频资源", "12 校验混剪需求", "13 准备画面素材", "14 准备人声和字幕", "15 选择背景音乐",
           "16 汇总混剪素材", "17 组装剪辑时间轴", "18 本地合成视频", "19 超分结果与成片检查", "运行报告", "Euler RPC / 本地回放"]
NODE_DISPLAY_NAME_MAPPINGS = {key: "商品混剪 · " + title for key, title in zip(NODE_CLASS_MAPPINGS, _titles)}
WEB_DIRECTORY = "./web"
