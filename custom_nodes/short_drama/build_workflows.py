"""Build UI/API workflows from the installed node schemas; never submits a prompt."""
import json
from pathlib import Path
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent
SCHEMAS = {}


def schema(name):
    if name not in SCHEMAS:
        with urllib.request.urlopen("http://127.0.0.1:8188/object_info/" + name, timeout=10) as response:
            SCHEMAS[name] = json.load(response)[name]
    return SCHEMAS[name]


def widget_type(kind):
    return isinstance(kind, list) or kind in ("STRING", "INT", "FLOAT", "BOOLEAN", "COMBO", "COMFY_DYNAMICCOMBO_V3")


def port_type(kind):
    return "COMBO" if isinstance(kind, list) else kind


def default_value(definition):
    kind = definition[0]
    options = definition[1] if len(definition) > 1 else {}
    if "default" in options:
        return options["default"]
    if isinstance(kind, list):
        return kind[0]
    return {"STRING": "", "INT": 0, "FLOAT": 0.0, "BOOLEAN": False}.get(kind)


class Workflow:
    def __init__(self):
        self.api = {}
        self.nodes = {}
        self.links = []
        self.groups = []

    def add(self, node_id, kind, title, values, position, size=(480, 480), color="#25394a"):
        info = schema(kind)
        inputs = {**info["input"].get("required", {}), **info["input"].get("optional", {})}
        connections = {name: value for name, value in values.items() if isinstance(value, list)}
        ordinary, converted, widgets = [], [], []
        for name, definition in inputs.items():
            kind_type = definition[0]
            options = definition[1] if len(definition) > 1 else {}
            if options.get("hidden"):
                continue
            is_widget = widget_type(kind_type) and not options.get("forceInput")
            if is_widget:
                value = values.get(name, default_value(definition))
                widgets.append(default_value(definition) if name in connections else value)
                if name in connections:
                    converted.append({"name": name, "type": port_type(kind_type), "link": None, "widget": {"name": name}})
            else:
                ordinary.append({"name": name, "type": port_type(kind_type), "link": None})
        if kind == "SaveVideo":
            widgets = ["ShortDrama/video", "mp4", "auto"]
            ordinary = [{"name": "video", "type": "VIDEO", "link": None}]
            converted = [{"name": "filename_prefix", "type": "STRING", "link": None, "widget": {"name": "filename_prefix"}}]
        node = {
            "id": node_id, "type": kind, "title": title, "pos": list(position), "size": list(size),
            "flags": {}, "order": len(self.nodes), "mode": 0, "inputs": ordinary + converted,
            "outputs": [{"name": info["output_name"][index], "type": port_type(output), "links": [], "slot_index": index,
                         "shape": 6 if info["output_is_list"][index] else 3}
                        for index, output in enumerate(info["output"])],
            "properties": {"Node name for S&R": kind}, "widgets_values": widgets,
            "color": color, "bgcolor": "#19212b",
        }
        self.nodes[node_id] = node
        self.api[str(node_id)] = {"class_type": kind, "inputs": values, "_meta": {"title": title}}
        for name, (source, slot) in connections.items():
            source_node = self.nodes[int(source)]
            input_slot = next(index for index, item in enumerate(node["inputs"]) if item["name"] == name)
            link_id = len(self.links) + 1
            output_type = source_node["outputs"][slot]["type"]
            node["inputs"][input_slot]["link"] = link_id
            source_node["outputs"][slot]["links"].append(link_id)
            self.links.append([link_id, int(source), slot, node_id, input_slot, output_type])

    def group(self, title, bounds, color):
        self.groups.append({"id": len(self.groups) + 1, "title": title, "bounding": bounds, "color": color, "font_size": 26, "flags": {}})

    def note(self, content, position):
        self.nodes[90] = {"id": 90, "type": "Note", "title": "使用说明", "pos": list(position), "size": [520, 300],
                          "flags": {}, "order": len(self.nodes), "mode": 0, "inputs": [], "outputs": [],
                          "properties": {"Node name for S&R": "Note"}, "widgets_values": [content],
                          "color": "#433b25", "bgcolor": "#272317"}

    def save(self, stem, title):
        ui = {"last_node_id": max(self.nodes), "last_link_id": len(self.links), "nodes": list(self.nodes.values()),
              "links": self.links, "groups": self.groups, "config": {},
              "extra": {"ds": {"scale": 0.65, "offset": [30, 40]}}, "version": 0.4}
        destinations = [
            (PACKAGE / "api" / f"{stem}.json", self.api),
            (PACKAGE / "example_workflows" / f"{stem}.json", ui),
            (ROOT / "ComfyUI/user/default/workflows" / f"{title}.json", ui),
        ]
        for path, data in destinations:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(path.relative_to(ROOT))


def initial(mode):
    graph = Workflow()
    brief = {
        "direction": "一位侦探在雨夜巷口遇见失散多年的妹妹。悬疑开场，温情反转，结尾留下一个小悬念。",
        "project_name": "我的短剧", "visual_style": "写实电影感，自然表演，克制运镜，统一人物、服装、光线与色调。",
        "shot_count": 3, "shot_seconds": 5, "ratio": "16:9",
    }
    graph.add(1, "ShortDramaBrief", "01 · 在这里填写故事方向", brief, (40, 110), (520, 540))
    graph.add(2, "CodexTextGenerate", "02 · 完善剧本与分镜 / Sol · 极高", {
        "prompt": ["1", 0], "model": "gpt-5.6-sol", "reasoning_effort": "xhigh", "timeout_seconds": 900,
        "instructions": "你是专业短剧编剧和分镜导演，严格按给定制作规格与 JSON Schema 输出。完整剧本、人物设定和逐镜描述必须自洽。",
        "output_schema": ["1", 1], "reuse_result": True, "generation_id": 0,
    }, (650, 110), (490, 630))
    graph.add(3, "ShortDramaShots", "03 · 自动展开所有镜头 / 可粘贴修改稿", {
        "spec": ["1", 2], "script_json": ["2", 0], "video_mode": mode, "edited_script": "",
    }, (1220, 110), (440, 580))
    graph.add(4, "SaveText", "完整剧本与分镜 · 预览并保存", {
        "text": ["3", 13], "filename_prefix": ["3", 11], "format": "json",
    }, (650, 830), (1000, 390))
    graph.group("01—03  输入方向 → 完善剧本 → 分镜", [0, 30, 1695, 1230], "#466d8a")
    return graph


def image_values(prompt, edit=False):
    return {
        "prompt": prompt,
        # 由文生图节点从进程环境变量读取服务地址；留空可继续兼容节点默认值。
        "base_url": "",
        "model": "gpt-image-2", "size": ["3", 6], "quality": "medium", "n": 1,
        "timeout_seconds": 600, "reuse_result": True, "generation_id": 0,
    }


def build_full(mode):
    graph = initial(mode)
    graph.add(5, "TextToImageGenerate", "04 · 统一人物与美术设定图", image_values(["3", 0]), (1770, 110), (490, 590), "#354933")
    graph.add(6, "SaveImage", "人物设定图 · 保存", {"images": ["5", 0], "filename_prefix": ["3", 7]}, (1770, 830), (490, 390))
    graph.add(7, "ReferenceImageGenerate", "05 · 每镜首帧 / 分镜图（参考人物图）", {
        "reference_images": ["5", 0], **image_values(["3", 1], edit=True),
    }, (2350, 110), (490, 590), "#354933")
    graph.add(8, "SaveImage", "逐镜分镜图 · 保存", {"images": ["7", 0], "filename_prefix": ["3", 8]}, (2350, 830), (490, 390))
    video_x = 2940
    if mode == "FL2VA":
        graph.add(9, "ReferenceImageGenerate", "06 · 每镜尾帧（参考同镜首帧）", {
            "reference_images": ["7", 0], **image_values(["3", 2], edit=True),
        }, (2930, 110), (490, 590), "#354933")
        graph.add(10, "SaveImage", "逐镜尾帧 · 保存", {"images": ["9", 0], "filename_prefix": ["3", 9]}, (2930, 830), (490, 390))
        video_x = 3520
    graph.group("04—06  人物一致性与逐镜图片（自动复用）", [1730, 30, video_x - 1770, 1230], "#52764f")
    video_inputs = {
        "prompt": ["3", 3], "base_url": "http://127.0.0.1:18081", "duration": ["3", 4],
        "ratio": ["3", 5], "inference_steps": 5, "generation_id": 0,
    }
    if mode == "FL2VA":
        video_inputs.update(first_frame=["7", 0], last_frame=["9", 0])
    else:
        video_inputs.update(reference_image_1=["7", 0], reference_image_2=["5", 0])
    graph.add(11, "MerlinH3FL2VA" if mode == "FL2VA" else "MerlinH3Ref2VA",
              "07 · MiniMax " + mode + " · 逐镜提交", video_inputs, (video_x, 110), (490, 680), "#4a3549")
    graph.add(12, "PreviewAny", "逐镜任务编号 · 可用于恢复", {"source": ["11", 1]}, (video_x, 900), (490, 280))
    graph.add(13, "MerlinH3GetVideo", "08 · 等待并取回逐镜视频", {
        "task_id": "", "base_url": "http://127.0.0.1:18081", "wait_minutes": 120, "task": ["11", 0],
    }, (video_x + 580, 110), (440, 300))
    graph.add(14, "SaveVideo", "每个镜头 · 视频预览并保存", {
        "video": ["13", 0], "filename_prefix": ["3", 10], "format": "mp4", "format.codec": "auto",
    }, (video_x + 580, 560), (440, 530))
    graph.group("07—08  对应通道生成逐镜视频", [video_x - 40, 30, 1100, 1230], "#835777")
    graph.add(15, "ShortDramaJoin", "09 · 按镜头顺序拼接 / 保留原声", {"videos": ["14", 0], "fps": 24},
              (video_x + 1140, 110), (480, 220), "#4a4230")
    graph.add(16, "SaveVideo", "10 · 完整成片 · 预览并导出 MP4", {
        "video": ["15", 0], "filename_prefix": ["3", 12], "format": "mp4", "format.codec": "auto",
    }, (video_x + 1140, 500), (610, 580))
    graph.group("09—10  拼接与成片", [video_x + 1100, 30, 690, 1230], "#8a764f")
    graph.note(
        "先启动 start-merlin-h3.command，并确认对应通道有可用实例。\n"
        "在 01 填写方向、镜头数、每镜时长与画幅，然后运行。默认 3 镜 × 5 秒。\n"
        "可先使用「短剧 · 00 剧本审阅」；将确认/修改后的 JSON 粘到 03 的 edited_script，便可跳过 Codex。\n"
        "人物图、分镜图、剧本默认复用。重新创作时修改相应生成节点的 generation_id。H3 的编号只重生成视频。\n"
        + ("本版为真正的首尾帧：每镜独立生成首帧、尾帧，共用同一人物设定。\n" if mode == "FL2VA" else
           "本版每镜使用 <Picture 1> 分镜图 + <Picture 2> 人物设定图，不是首尾帧约束。\n")
        + "n 保持 1，确保一个镜头对应一个图像任务。声音由 H3 原生生成；无配音校对或字幕烧录。",
        (40, 830),
    )
    stem = "full-fl2va" if mode == "FL2VA" else "full-ref2va"
    title = "短剧 · 01 完整生成（首尾帧 FL2VA）" if mode == "FL2VA" else "短剧 · 02 完整生成（参考图 Ref2VA）"
    graph.save(stem, title)


if __name__ == "__main__":
    review = initial("FL2VA")
    review.note("只生成并保存剧本 JSON，不生成图片或视频。\n满意后将 JSON 粘到完整工作流 03 的 edited_script。\n完整流程的镜头数量、画幅和视觉风格应与这里一致。", (40, 830))
    review.save("script-review", "短剧 · 00 剧本审阅")
    build_full("FL2VA")
    build_full("Ref2VA")
