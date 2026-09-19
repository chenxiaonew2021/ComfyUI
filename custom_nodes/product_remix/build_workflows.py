"""Generate UI/API workflows and install this node pack within this project only."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
sys.path.insert(0, str(WORKSPACE / "ComfyUI"))
sys.argv = [sys.argv[0], "--cpu"]
module_spec = importlib.util.spec_from_file_location("product_remix", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
module = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = module
module_spec.loader.exec_module(module)

DEMO = json.loads((ROOT / "example_inputs/demo.json").read_text(encoding="utf-8"))
KINDS = list(module.NODE_CLASS_MAPPINGS)[:19] + ["SaveVideo", "ProductRemixReport"]
DEPENDENCIES = {
    2: {}, 3: {}, 4: {"sources": [3, 0]}, 5: {"catalog": [4, 0]},
    6: {"catalog": [4, 0], "selling": [5, 0], "policy": [2, 0]},
    7: {"catalog": [4, 0], "selling": [5, 0], "creative": [6, 0], "policy": [2, 0]},
    8: {"candidates": [7, 0]}, 9: {"plan": [8, 0], "catalog": [4, 0], "policy": [2, 0]},
    10: {"plan": [8, 0]}, 11: {"plan": [8, 0], "catalog": [4, 0], "generated": [10, 0]},
    12: {"requirement": [11, 0]}, 13: {"requirement": [12, 0]},
    14: {"requirement": [12, 0]}, 15: {"requirement": [12, 0]},
    16: {"requirement": [12, 0], "visuals": [13, 0], "voices": [14, 0], "bgm": [15, 0], "cover": [9, 0]},
    17: {"prepared": [16, 0]}, 18: {"timeline": [17, 0]},
    19: {"rendered": [18, 0], "policy": [2, 0]}, 20: {"video": [19, 0]},
    21: {"final": [19, 1], "saved_video": [20, 0]},
}
POSITIONS = {
    1: (30, 200), 2: (30, 1090), 3: (540, 200), 4: (540, 650),
    5: (1030, 200), 6: (1030, 590), 7: (1030, 1010), 8: (1520, 200),
    9: (1520, 800), 10: (1520, 1170), 11: (2010, 200), 12: (2010, 720),
    13: (2500, 200), 14: (2500, 650), 15: (2500, 1100), 16: (2990, 200),
    17: (2990, 730), 18: (3480, 200), 19: (3480, 780), 20: (3970, 200), 21: (3970, 1070),
}


def api_workflow(spec):
    prompt = {}
    for node_id, kind in enumerate(KINDS, 1):
        values = {key: [str(dep[0]), dep[1]] for key, dep in DEPENDENCIES.get(node_id, {}).items()}
        if node_id not in {1, 20}:
            values["context"] = ["1", 0]
        if node_id == 1:
            values["spec_json"] = json.dumps(spec, ensure_ascii=False, indent=2)
        if node_id == 20:
            values.update(filename_prefix="ProductRemix/Final", format="mp4", **{"format.codec": "auto"})
        prompt[str(node_id)] = {"class_type": kind, "inputs": values}
    return prompt


def schema_for(kind):
    if kind == "SaveVideo":
        return ({"required": {"video": ("VIDEO",), "filename_prefix": ("STRING", {"default": "ProductRemix/Final"}),
                              "format": (["mp4"], {"default": "mp4"}), "format.codec": (["auto"], {"default": "auto"})}}, ("VIDEO",), ("video",))
    if kind == "CodexTextGenerate":
        return ({"required": {"prompt": ("STRING", {"multiline": True}), "model": ("STRING", {"default": "gpt-5.6-sol"}),
                              "reasoning_effort": (["low", "medium", "high", "xhigh", "max", "ultra"], {"default": "xhigh"}),
                              "timeout_seconds": ("INT", {"default": 900})},
                 "optional": {"instructions": ("STRING", {"default": "只生成 JSON，不操作文件或执行命令。"}),
                              "output_schema": ("STRING", {"default": ""}), "reuse_result": ("BOOLEAN", {"default": True}),
                              "generation_id": ("INT", {"default": 0})}}, ("STRING",), ("text",))
    cls = module.NODE_CLASS_MAPPINGS[kind]
    outputs = cls.RETURN_TYPES
    return cls.INPUT_TYPES(), outputs, getattr(cls, "RETURN_NAMES", outputs)


def ui_workflow(prompt, subtitle):
    workflow = {"last_node_id": max(map(int, prompt)), "last_link_id": 0, "nodes": [], "links": [], "groups": [],
                "config": {}, "extra": {"ds": {"scale": 0.25, "offset": [80, 80]}}, "version": 0.4}
    index = {}
    for key, data in prompt.items():
        node_id, kind = int(key), data["class_type"]
        schema, outputs, names = schema_for(kind)
        node = {"id": node_id, "type": kind, "title": module.NODE_DISPLAY_NAME_MAPPINGS.get(kind, kind),
                "pos": list(POSITIONS.get(node_id, (2010, 1210))), "size": [440, 400], "flags": {}, "order": node_id - 1, "mode": 0,
                "properties": {"Node name for S&R": kind}, "inputs": [],
                "outputs": [{"name": name, "type": typ, "links": []} for name, typ in zip(names, outputs)], "widgets_values": []}
        if node_id == 1:
            node["size"] = [450, 810]
        elif node_id == 20:
            node["size"] = [440, 780]
        elif node_id in {8, 17}:
            node["size"] = [440, 490]
        for name, field in {**schema.get("required", {}), **schema.get("optional", {})}.items():
            typ, options = field[0], field[1] if len(field) > 1 else {}
            value = data["inputs"].get(name)
            linked = isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and value[0] in prompt
            widget = isinstance(typ, list) or typ in {"STRING", "FLOAT", "INT", "BOOLEAN"}
            if not widget or linked:
                slot = {"name": name, "type": typ if isinstance(typ, str) else "COMBO", "link": None}
                if widget:
                    slot["widget"] = {"name": name}
                node["inputs"].append(slot)
            if widget:
                fallback = typ[0] if isinstance(typ, list) else {"STRING": "", "INT": 0, "FLOAT": 0.0, "BOOLEAN": False}[typ]
                node["widgets_values"].append(options.get("default", fallback) if linked or value is None else value)
        workflow["nodes"].append(node)
        index[key] = node
    for key, data in prompt.items():
        for name, value in data["inputs"].items():
            if not isinstance(value, list) or len(value) != 2 or not isinstance(value[0], str) or value[0] not in index:
                continue
            source, slot = value
            target_slot = next(i for i, x in enumerate(index[key]["inputs"]) if x["name"] == name)
            link_id = len(workflow["links"]) + 1
            socket = index[source]["outputs"][slot]["type"]
            index[source]["outputs"][slot]["links"].append(link_id)
            index[key]["inputs"][target_slot]["link"] = link_id
            workflow["links"].append([link_id, int(source), slot, int(key), target_slot, socket])
    workflow["last_link_id"] = len(workflow["links"])
    note_id = workflow["last_node_id"] + 1
    workflow["nodes"].append({"id": note_id, "type": "Note", "title": "商品混剪 · 19 阶段完整流程", "pos": [30, -150],
                              "size": [4380, 200], "flags": {}, "order": 99, "mode": 0, "properties": {}, "widgets_values": [subtitle], "inputs": [], "outputs": []})
    workflow["last_node_id"] = note_id
    for col, title in enumerate(["输入与策略", "素材处理", "创意与筛选", "剧本 / 封面 / AIGC", "资源与校验", "画面 / 人声 / 音乐", "汇总与时间轴", "合成与增强", "保存与报告"]):
        workflow["groups"].append({"id": col + 1, "title": title, "bounding": [10 + col * 490, 100, 480, 1510],
                                   "color": ["#35686d", "#3f627e", "#66548c"][col % 3], "font_size": 26, "flags": {}})
    return workflow


def install():
    examples, api = ROOT / "example_workflows", ROOT / "api"
    installed = WORKSPACE / "ComfyUI/user/default/workflows"
    inputs_dir = WORKSPACE / "ComfyUI/input/product-remix"
    for directory in (examples, api, installed, inputs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    main = api_workflow(DEMO)
    codex = deepcopy(main)
    codex["24"] = {"class_type": "CodexTextGenerate", "inputs": {
        "prompt": ["7", 1], "output_schema": ["7", 2], "model": "gpt-5.6-sol", "reasoning_effort": "xhigh",
        "timeout_seconds": 900, "reuse_result": True, "generation_id": 0,
        "instructions": "仅生成结构化剧本，不执行任何工具或命令，不写文件，不发布。"}}
    codex["8"]["inputs"]["script_json"] = ["24", 0]
    fixture = deepcopy(main)
    fixture["24"] = {"class_type": "ProductRemixEulerRPC", "inputs": {
        "mode": "fixture", "profile_file": "product-remix/rpc-profile.json", "request_json": '{"ProductID":"local-demo"}'}}
    fixture["8"]["inputs"]["script_json"] = ["24", 0]
    notes = "\n".join([
        "读取引用会话后按 19 个业务阶段组织。左侧 JSON 是唯一的本地制作规格；已有猫咪视频仅用于流程演示。",
        "默认全部在本机执行：素材探测/切段 → 已确认台词 → macOS 配音测时 → 句级字幕 → 中文封面/配乐 → FFmpeg → SaveVideo。",
        "封面与 AIGC、画面/人声/BGM 的依赖分支均保留。节点 17 可审阅逐镜清单，输出报告包含 19 阶段状态。",
        "AIGC/清字幕/超分开启后需要真实生成结果；不会自动调用生产任务、写业务库或发布。详见 ComfyUI/custom_nodes/product_remix/README.md。",
    ])
    variants = [("商品混剪 · 01 本地完整流程", "local-full", main, notes),
                ("商品混剪 · 02 Codex 剧本接入", "codex-script", codex, notes + "\n本画布会调用现有 Codex 节点生成剧本，需要可用账号；本次仅验证图结构，不进行远程推理。"),
                ("商品混剪 · 03 RPC 契约回放", "rpc-replay", fixture, notes + "\nRPC 节点默认为 fixture，只消费本地响应记录。选择 euler 前必须配置真实 IDL、接口、Caller 和路由。")]
    for name, slug, prompt, note in variants:
        (api / f"{slug}.json").write_text(json.dumps(prompt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        source = examples / f"{name}.json"
        source.write_text(json.dumps(ui_workflow(prompt, note), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.copyfile(source, installed / source.name)
        print(installed / source.name)
    fixture_script = {"title": DEMO["product"]["name"], "blocks": [
        {"id": f"b{i + 1}", "clip_id": item["id"], "voice_mode": "tts", "text": item["narration"]}
        for i, item in enumerate(DEMO["materials"])]}
    files = {
        "demo.json": DEMO,
        "rpc-profile.json": {"fixture_file": "product-remix/rpc-fixture.json", "response_path": "Script"},
        "rpc-fixture.json": {"request": {"ProductID": "local-demo"}, "response": {"Script": fixture_script}},
        "rpc-profile.euler.example.json": {"idl_file": "product-remix/idl/REPLACE_WITH_REAL_SERVICE.thrift",
            "service": "REPLACE_WITH_REAL_SERVICE", "request_type": "REPLACE_WITH_REAL_REQUEST", "method": "REPLACE_WITH_REAL_METHOD",
            "target": "sd://REPLACE_WITH_AUTHORIZED_PSM", "caller": "REPLACE_WITH_AUTHORIZED_CALLER", "timeout_seconds": 30,
            "gdpr_token_env": "PRODUCT_REMIX_GDPR_TOKEN", "response_path": ""},
    }
    for filename, value in files.items():
        (ROOT / "example_inputs" / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        destination = inputs_dir / filename
        if not destination.exists():
            destination.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    target = WORKSPACE / "ComfyUI/custom_nodes/product_remix"
    if not target.exists() and not target.is_symlink():
        target.symlink_to(ROOT, target_is_directory=True)
    elif target.resolve() != ROOT:
        raise RuntimeError(f"节点安装位置已被其他文件占用：{target}")


if __name__ == "__main__":
    install()
