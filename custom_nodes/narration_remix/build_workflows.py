"""Generate canvases without importing or executing ComfyUI nodes."""
import ast
from copy import deepcopy
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
defaults = {}
for item in ast.parse((ROOT / "__init__.py").read_text()).body:
    if isinstance(item, ast.Assign) and isinstance(item.targets[0], ast.Name):
        name = item.targets[0].id
        if name == "DEFAULT_SRT":
            defaults[name] = ast.literal_eval(item.value)
        elif name in {"DEFAULT_MANIFEST", "DEFAULT_DIRECTIONS"}:
            defaults[name] = json.dumps(ast.literal_eval(item.value.args[0]), ensure_ascii=False, indent=2)


def node(id, kind, title, pos, size, values, inputs=(), outputs=()):
    return {"id": id, "type": kind, "title": title, "pos": pos, "size": size, "flags": {},
            "order": id - 1, "mode": 0, "properties": {"Node name for S&R": kind}, "widgets_values": values,
            "inputs": [{"name": n, "type": t, "link": None} for n, t in inputs],
            "outputs": [{"name": n, "type": t, "links": []} for n, t in outputs]}


def link(workflow, source, output, target, input_slot, socket):
    id = len(workflow["links"]) + 1
    nodes = {n["id"]: n for n in workflow["nodes"]}
    nodes[source]["outputs"][output]["links"].append(id)
    nodes[target]["inputs"][input_slot]["link"] = id
    workflow["links"].append([id, source, output, target, input_slot, socket])
    workflow["last_link_id"] = id


review = {
    "last_node_id": 7, "last_link_id": 0, "nodes": [
        node(1, "LoadAudio", "01 · 上传/选择解说配音", [40, 260], [340, 220], [""], outputs=[("AUDIO", "AUDIO")]),
        node(2, "RemixNarrationPlan", "02 · 校正字幕 + 逐句选镜意图", [450, 260], [530, 620],
             [defaults["DEFAULT_SRT"], defaults["DEFAULT_DIRECTIONS"]],
             inputs=[("narration", "AUDIO")], outputs=[("解说时间轴", "REMIX_NARRATION")]),
        node(3, "RemixFootageCatalog", "03 · 素材目录 / 标签 / 可用区间", [450, 980], [530, 490],
             ["remix/footage", defaults["DEFAULT_MANIFEST"]], outputs=[("素材目录", "REMIX_CATALOG")]),
        node(4, "RemixEditDecision", "04 · 运行后在此审阅选镜清单", [1100, 260], [620, 520], [3.5, 30, "error"],
             inputs=[("narration_plan", "REMIX_NARRATION"), ("catalog", "REMIX_CATALOG")],
             outputs=[("剪辑清单", "REMIX_EDL"), ("审阅报告", "STRING")]),
        node(5, "PreviewAudio", "先听配音，确认语气与节奏", [40, 580], [340, 190], [],
             inputs=[("audio", "AUDIO")], outputs=[("audio", "AUDIO")]),
        node(6, "Note", "解说混剪 · 一期 / 先审阅，再导出", [40, 20], [1680, 170], [
            "主线：主题与开场 → 确认文案 → 配音与时间字幕 → 素材标注 → 逐句选镜 → 清单审阅 → 字幕配乐 → 导出与审片。\n"
            "当前画布先审阅选镜，不渲染视频。准备真实配音、对应 SRT 和本地素材后运行。示例字幕不是已对齐的配音。\n"
            "先看画面是否对应解说，再考虑转场。素材放到 ComfyUI/input/remix/footage。"]),
        node(7, "Note", "输入准备与现有节点接入位置", [1100, 880], [620, 460], [
            "1. 配音：左侧 LoadAudio 上传，也可替换成 ElevenLabs / Fish Audio 等现有 TTS 的 AUDIO 输出。\n\n"
            "2. 字幕：粘贴真实配音对应的 SRT 正文。关键词与素材 tags 精确匹配；clip_ids 可指定某句的候选片段。\n\n"
            "3. 素材：每段填写 id、file、in、out、tags；同一文件可以标多个区间。示例文件名不会自动生成。\n\n"
            "4. 审阅：查看时间、来源区间、字幕、匹配提示。缺素材时补素材或改标签；fallback 会继续选片并标记。\n\n"
            "5. 渲染：在本画布添加 混剪·字幕配乐渲染 + SaveVideo，或将相同输入填入 02 渲染导出模板。\n\n"
            "自动文案可接 Gemini，自动对齐可选 Whisper。它们未在本画布连接或调用。完整能力对照见节点包 README。"]),
    ], "links": [], "groups": [], "config": {},
    "extra": {"ds": {"scale": 0.62, "offset": [40, 30]}}, "version": 0.4,
}
for args in [(1, 0, 2, 0, "AUDIO"), (1, 0, 5, 0, "AUDIO"),
             (2, 0, 4, 0, "REMIX_NARRATION"), (3, 0, 4, 1, "REMIX_CATALOG")]:
    link(review, *args)

render = deepcopy(review)
render["nodes"][5]["title"] = "解说混剪 · 一期 / 字幕配乐与成片导出"
render["nodes"][5]["size"] = [2750, 170]
render["nodes"][5]["widgets_values"] = [
    "主线：确认文案/配音 → SRT 时间轴 + 素材标签 → 逐句选镜 → 中文字幕 + 配乐 → MP4。\n"
    "请先准备真实配音、对应 SRT 和素材，并确认选镜。运行将渲染成片；配音决定总时长，原素材静音。\n"
    "默认 720×1280 草稿，pad 保全画面；正式输出可改 1080×1920。BGM 可选，右下方提供待连接的加载节点。"]
render["nodes"][6]["widgets_values"][0] = render["nodes"][6]["widgets_values"][0].replace(
    "5. 渲染：在本画布添加 混剪·字幕配乐渲染 + SaveVideo，或将相同输入填入 02 渲染导出模板。",
    "5. 渲染：本画布已连接渲染与 SaveVideo。请先审阅选镜；两个模板的输入不共享，修改后需同步。")
render["nodes"] += [
    node(8, "RemixRender", "05 · 统一画幅 / 中文字幕 / 配乐", [1820, 260], [440, 540],
         ["720x1280", "pad", True, "/System/Library/Fonts/STHeiti Medium.ttc", 38, 100, -22, True],
         inputs=[("edl", "REMIX_EDL"), ("narration", "AUDIO"), ("bgm", "AUDIO")],
         outputs=[("成片", "VIDEO"), ("产物目录", "STRING")]),
    node(9, "SaveVideo", "06 · 成片预览 / MP4 导出", [2370, 260], [430, 600],
         ["NarrationRemix/Final", "mp4", "auto"], inputs=[("video", "VIDEO")], outputs=[("video", "VIDEO")]),
    node(10, "LoadAudio", "可选 · 上传 BGM 后接渲染的 bgm", [1820, 930], [440, 240], [""], outputs=[("AUDIO", "AUDIO")]),
    node(11, "Note", "声音与成片检查", [2370, 960], [430, 380], [
        "BGM 不接也能运行。上传音乐后，把 AUDIO 接到渲染节点 bgm。\n\n"
        "BGM 默认 -22 dB，讲话时进一步压低；最终仍需听审。\n\n"
        "pad 保留全画面，crop 居中裁切，尚不支持主体自动追踪。1080 竖屏建议字号 56、底部安全区 150 起调。\n\n"
        "审片：开头是否吸引人、画面是否贴合解说、字幕是否挡主体、有没有重复镜头、配乐是否盖人声。\n\n"
        "中间视频、SRT、EDL 和报告保存在 output/NarrationRemix/runs；最终视频由 SaveVideo 保存。"]),
]
render["last_node_id"] = 11
render["extra"]["ds"]["scale"] = 0.48
for args in [(4, 0, 8, 0, "REMIX_EDL"), (1, 0, 8, 1, "AUDIO"), (8, 0, 9, 0, "VIDEO")]:
    link(render, *args)

examples = ROOT / "example_workflows"
installed = WORKSPACE / "ComfyUI/user/default/workflows"
examples.mkdir(exist_ok=True)
installed.mkdir(parents=True, exist_ok=True)
for name, workflow in [("解说混剪 · 01 选镜审阅", review), ("解说混剪 · 02 渲染导出", render)]:
    source = examples / f"{name}.json"
    source.write_text(json.dumps(workflow, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copyfile(source, installed / source.name)
    print(installed / source.name)

example_inputs = ROOT / "example_inputs"
example_inputs.mkdir(exist_ok=True)
for filename, key in [("subtitles.srt", "DEFAULT_SRT"), ("directions.json", "DEFAULT_DIRECTIONS"), ("materials.json", "DEFAULT_MANIFEST")]:
    (example_inputs / filename).write_text(defaults[key] + "\n", encoding="utf-8")
(WORKSPACE / "ComfyUI/input/remix/footage").mkdir(parents=True, exist_ok=True)
node_link = WORKSPACE / "ComfyUI/custom_nodes/narration_remix"
if not node_link.exists() and not node_link.is_symlink():
    node_link.symlink_to(ROOT, target_is_directory=True)
elif node_link.resolve() != ROOT:
    raise RuntimeError(f"节点安装位置已被占用：{node_link}")
