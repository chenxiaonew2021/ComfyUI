"""Focused contract tests; pass --integration to submit only local ComfyUI graphs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

INTEGRATION = "--integration" in sys.argv
import build_workflows as workflows
from product_remix import runtime, rpc

m = workflows.module
ARTIFACTS = workflows.WORKSPACE / "artifacts/product-remix"
ARTIFACTS.mkdir(parents=True, exist_ok=True)


class LocalContracts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ARTIFACTS)
        self.addCleanup(self.temporary.cleanup)
        self.context = {"spec": deepcopy(workflows.DEMO), "run_dir": self.temporary.name, "run_id": "unit"}
        self.clip = {"id": "clip", "path": str(runtime.input_path("remix/e2e/footage/courtyard.mp4")), "in": 0, "out": 2.0}

    def test_voice_is_accelerated_without_cutting_words(self):
        voice = runtime.torch.ones((2, round(2.1 * runtime.SAMPLE_RATE))) * 0.05
        with patch.object(runtime, "speech", return_value=voice):
            plan = runtime.calibrate(self.context, {"title": "test", "blocks": [
                {"id": "b1", "clip_id": "clip", "text": "完整句子", "voice_mode": "tts"}]}, [self.clip])
        self.assertEqual(plan["total_frames"], 60)
        self.assertAlmostEqual(plan["blocks"][0]["voice_playback_rate"], 1.05)
        self.assertEqual(plan["blocks"][0]["text"], "完整句子")

    def test_identity_audio_decode_keeps_every_sample(self):
        samples = runtime.torch.linspace(0, 1, runtime.SAMPLE_RATE)
        data = (runtime.torch.sin(samples * 440 * 2 * runtime.np.pi) * 0.2).repeat(2, 1)
        path = Path(self.temporary.name) / "exact.wav"
        runtime.save_pcm(data, path)
        decoded = runtime.pcm(path)
        self.assertEqual(decoded.shape, data.shape)
        self.assertLess(float(runtime.torch.max(runtime.torch.abs(decoded - data))), 0.00005)

    def test_rewrite_retry_is_bounded_and_failure_keeps_no_false_result(self):
        voice = runtime.torch.ones((2, 4 * runtime.SAMPLE_RATE)) * 0.05
        with patch.object(runtime, "speech", return_value=voice) as synthesizer:
            with self.assertRaisesRegex(ValueError, "已尝试 3 版"):
                runtime.calibrate(self.context, {"title": "test", "blocks": [
                    {"id": "b1", "clip_id": "clip", "text": "超时台词", "rewrites": ["改写一", "改写二", "不应尝试"]}]}, [self.clip])
            self.assertEqual(synthesizer.call_count, 3)

    def test_missing_aigc_fails_and_records_failure(self):
        plan = {"blocks": [{"clip_id": "new", "source_type": "aigc"}]}
        with self.assertRaisesRegex(ValueError, "等待 AIGC"):
            m.ProductRemixAIShoot().execute(self.context, plan)
        entry = json.loads((Path(self.temporary.name) / "10-AIShoot.json").read_text())
        self.assertEqual(entry["status"], "failed")

    def test_missing_subtitle_erase_never_silently_passes(self):
        with self.assertRaisesRegex(ValueError, "真实清字幕"):
            m.ProductRemixVisuals().execute(self.context, {"blocks": [{"clip_id": "c", "subtitle_policy": "erase"}]})

    def test_missing_original_vocal_requires_separated_audio(self):
        with self.assertRaisesRegex(ValueError, "audio_file"):
            runtime.calibrate(self.context, {"title": "test", "blocks": [
                {"id": "b1", "clip_id": "clip", "voice_mode": "original_vocal", "text": ""}]}, [self.clip])

    def test_path_escape_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "ComfyUI/input"):
            runtime.input_path("../../../README.md")

    def test_unknown_clip_rejected(self):
        with self.assertRaisesRegex(ValueError, "未通过初筛"):
            runtime.calibrate(self.context, {"title": "test", "blocks": [
                {"id": "b1", "clip_id": "unknown", "voice_mode": "none"}]}, [self.clip])

    def test_partial_bad_material_preserves_valid_source(self):
        self.context["spec"]["materials"] = [workflows.DEMO["materials"][0], {"id": "missing", "file": "product-remix/missing.mp4"}]
        result = m.ProductRemixSupplement().execute(self.context)[0]
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(len(result["warnings"]), 1)

    def test_audio_and_visual_branch_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "同一个时间轴"):
            m.ProductRemixAggregate().execute(self.context, {"fps": 30, "total_frames": 30},
                                             {"fps": 25, "total_frames": 30}, {}, {}, {})

    def test_euler_request_constructs_nested_base_without_mutating_input(self):
        class Base:
            thrift_spec = {1: (11, "Caller", False), 2: (13, "Extra", (11, None, 11, None), False)}
        class Item:
            thrift_spec = {1: (10, "ID", True)}
        class Request:
            thrift_spec = {1: (15, "Items", (12, Item), True), 255: (12, "Base", Base, True)}
        class Client:
            def Plan(self, request):
                self.request = request
                return {"BaseResp": {"StatusCode": 0}, "Data": {"ok": True}}
        client = Client()
        profile = {"caller": "local.test", "request_type": "Request", "method": "Plan", "response_path": "Data"}
        data = {"Items": [{"ID": 42}], "Base": {"Extra": {"trace": "local"}}}
        result = rpc.invoke(SimpleNamespace(Request=Request), client, profile, data, "test-token")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.request.Items[0].ID, 42)
        self.assertEqual(client.request.Base.Caller, "local.test")
        self.assertEqual(client.request.Base.Extra, {"trace": "local", "gdpr-token": "test-token"})
        self.assertNotIn("gdpr-token", data["Base"]["Extra"])
        with self.assertRaises(ValueError):
            rpc.struct_from_dict(Request, {"Items": [{"ID": "42"}]})
        with self.assertRaises(ValueError):
            rpc.struct_from_dict(Request, {"Unknown": 1})

    def test_euler_business_error_is_not_success(self):
        class Request:
            class Base:
                thrift_spec = {1: (11, "Caller", False)}
            thrift_spec = {255: (12, "Base", Base, True)}
        class Client:
            calls = 0
            def Plan(self, request):
                self.calls += 1
                return {"BaseResp": {"StatusCode": 1601}}
        client = Client()
        with self.assertRaisesRegex(RuntimeError, "1601"):
            rpc.invoke(SimpleNamespace(Request=Request), client,
                       {"caller": "test", "request_type": "Request", "method": "Plan"}, {})
        self.assertEqual(client.calls, 1)

    def test_ui_links_have_matching_slots_and_api_inputs(self):
        for path in (workflows.ROOT / "example_workflows").glob("*.json"):
            workflow = json.loads(path.read_text())
            nodes = {n["id"]: n for n in workflow["nodes"]}
            for link_id, source, output_slot, target, input_slot, socket in workflow["links"]:
                self.assertIn(link_id, nodes[source]["outputs"][output_slot]["links"])
                self.assertEqual(nodes[target]["inputs"][input_slot]["link"], link_id)
                self.assertEqual(nodes[source]["outputs"][output_slot]["type"], socket)
                self.assertEqual(nodes[target]["inputs"][input_slot]["type"], socket)


def request(path, data=None):
    req = urllib.request.Request("http://127.0.0.1:8188" + path,
                                 data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def submit(name, prompt):
    result = request("/prompt", {"prompt": prompt})
    runtime.dump(ARTIFACTS / f"submission-{name}.json", result)
    prompt_id = result["prompt_id"]
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        history = request("/history/" + prompt_id).get(prompt_id)
        if history:
            runtime.dump(ARTIFACTS / f"history-{name}.json", history)
            if history["status"]["status_str"] != "success":
                raise RuntimeError(json.dumps(history["status"], ensure_ascii=False))
            report = history["outputs"]["21"]["text"][0]
            directory = Path(report.split("产物：", 1)[1].splitlines()[0])
            saved = history["outputs"]["20"]["images"][0]
            saved_path = workflows.WORKSPACE / "ComfyUI/output" / saved["subfolder"] / saved["filename"]
            print(f"{name}: {saved_path}", flush=True)
            return directory, saved_path
        time.sleep(0.5)
    raise TimeoutError(f"本地测试超时：{prompt_id}（不自动重试提交）")


def verify_media(directory, path):
    result = json.loads((directory / "result.json").read_text())
    final = result["final"]
    ffmpeg, ffprobe = runtime.executable("ffmpeg"), runtime.executable("ffprobe")
    runtime.run([ffmpeg, "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"])
    info = json.loads(runtime.run([ffprobe, "-v", "error", "-count_frames", "-show_streams", "-of", "json", str(path)]))
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    sound = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert int(video["nb_read_frames"]) == final["total_frames"], (video, final)
    assert abs(float(video["duration"]) - final["duration"]) < 1 / final["fps"]
    assert sound["sample_rate"] == "48000" and sound["channels"] == 2
    frames = json.loads(runtime.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
                                     "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)]))["frames"]
    timestamps = [float(frame["best_effort_timestamp_time"]) for frame in frames]
    assert all(abs(value - index / final["fps"]) < 0.00001 for index, value in enumerate(timestamps)), "视频帧时间戳不连续"
    packets = json.loads(runtime.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_packets",
                                      "-show_entries", "packet=pts_time,duration_time", "-of", "json", str(path)]))["packets"]
    gaps = [abs(float(b["pts_time"]) - (float(a["pts_time"]) + float(a["duration_time"]))) for a, b in zip(packets, packets[1:])]
    assert max(gaps, default=0) < 0.001, max(gaps)
    reference = runtime.pcm(directory / "voice-track.wav").numpy()
    actual = runtime.pcm(path).numpy()
    sample_count = min(reference.shape[-1], actual.shape[-1])
    reference = reference[:, :sample_count].ravel()
    actual = actual[:, :sample_count].ravel()
    correlation = float(runtime.np.dot(reference, actual) / (runtime.np.linalg.norm(reference) * runtime.np.linalg.norm(actual)))
    assert correlation > 0.94, correlation
    return {"saved_video": str(path), "run_dir": str(directory), "frames": int(video["nb_read_frames"]),
            "duration": float(video["duration"]), "size": [video["width"], video["height"]],
            "video_codec": video["codec_name"], "audio_codec": sound["codec_name"],
            "max_audio_packet_gap_seconds": max(gaps, default=0), "voice_correlation": correlation,
            "stages": result["stages"], "decoded": True}


def advanced_spec():
    directory = workflows.WORKSPACE / "ComfyUI/input/product-remix/validation"
    directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = runtime.executable("ffmpeg")
    clean, original, generated = directory / "clean.mp4", directory / "source.mp4", directory / "generated.mp4"
    runtime.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=2",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "2", str(clean)])
    overlay = directory / "original-subtitle.png"
    runtime.narration.subtitle_image("原字幕测试", 320, 180, m.ImageFont.truetype(runtime.FONT, 20), 12, overlay)
    runtime.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(clean), "-i", str(overlay),
                 "-filter_complex", "[0:v][1:v]overlay=0:0[v]", "-map", "[v]", "-map", "0:a", "-c:v", "libx264", "-c:a", "copy", str(original)])
    runtime.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=1",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(generated)])
    spec = deepcopy(workflows.DEMO)
    spec["product"] = {"name": "可选分支技术测试", "selling_points": ["测试"], "tags": ["测试"]}
    spec["materials"] = [{"id": "control", "file": "product-remix/validation/source.mp4", "tags": ["测试"], "in": 0, "out": 2,
                           "scene_detect": True, "description": "本地控制画面与正弦音轨，不是真实广告素材"}]
    spec["script"] = {"title": "技术测试", "blocks": [
        {"id": "b1", "clip_id": "control", "voice_mode": "source_audio", "duration": 2, "text": "", "subtitle_policy": "erase"},
        {"id": "b2", "clip_id": "generated", "source_type": "aigc", "voice_mode": "none", "duration": 1, "text": ""}]}
    spec["policy"] = {"creative_recall": False, "cover": False, "aigc": True, "super_resolution": False}
    spec["assets"] = {"aigc": {"generated": "product-remix/validation/generated.mp4"}, "erased": {"control": "product-remix/validation/clean.mp4"}}
    spec["bgm"] = {}
    return spec


def integration():
    checks = {}
    main = json.loads((workflows.ROOT / "api/local-full.json").read_text())
    directory, path = submit("local-full-verified", main)
    checks["local-full"] = verify_media(directory, path)
    replay = json.loads((workflows.ROOT / "api/rpc-replay.json").read_text())
    spec = json.loads(replay["1"]["inputs"]["spec_json"])
    spec["bgm"] = {}
    spec["policy"]["cover"] = False
    replay["1"]["inputs"]["spec_json"] = json.dumps(spec, ensure_ascii=False)
    directory, path = submit("rpc-replay-no-bgm", replay)
    checks["rpc-replay"] = verify_media(directory, path)
    spec = advanced_spec()
    directory, path = submit("optional-assets", workflows.api_workflow(spec))
    checks["optional-assets"] = verify_media(directory, path)
    enhanced = workflows.WORKSPACE / "ComfyUI/input/product-remix/validation/enhanced.mp4"
    runtime.run([runtime.executable("ffmpeg"), "-v", "error", "-nostdin", "-y", "-i", str(path), "-an",
                 "-vf", "scale=1080:1920", "-c:v", "libx264", "-preset", "fast", str(enhanced)])
    spec["policy"]["super_resolution"] = True
    spec["assets"]["super_resolution"] = "product-remix/validation/enhanced.mp4"
    directory, path = submit("enhanced-result", workflows.api_workflow(spec))
    checks["enhanced-result"] = verify_media(directory, path)
    assert checks["enhanced-result"]["size"] == [1080, 1920]
    checks["enhanced-result"]["fixture_note"] = "本地缩放只用作结果接入的测试文件，未执行或宣称 AI 超分"
    reference = json.loads((ARTIFACTS / "reference-hashes.json").read_text())
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in reference.items())
    checks["external_reference_files_unchanged"] = True
    runtime.dump(ARTIFACTS / "verification.json", checks)
    print(json.dumps(checks, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LocalContracts))
    runtime.dump(ARTIFACTS / "contract-tests.json", {"tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors), "success": result.wasSuccessful()})
    if not result.wasSuccessful():
        raise SystemExit(1)
    if INTEGRATION:
        integration()
