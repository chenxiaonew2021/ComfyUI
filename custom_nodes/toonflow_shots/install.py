"""Install the contract and canvas; does not execute nodes or submit generation."""
import json
from pathlib import Path
import shutil

from contract import ShotRequest

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parents[2]
(ROOT / "request.schema.json").write_text(json.dumps(ShotRequest.model_json_schema(), ensure_ascii=False, indent=2) + "\n")
name = "短剧分镜 · Toonflow 一致性视频.json"
canvas = json.loads((ROOT / "example_workflows" / name).read_text())
for node in canvas["nodes"]:
    if node["type"] == "PreviewAny":
        node["outputs"] = [{"name": "STRING", "type": "STRING", "links": []}]
    if node["type"] == "Note":
        node["widgets_values"] = [value.replace("output/ShortDrama/", "output/ToonflowShots/") for value in node["widgets_values"]]
(ROOT / "example_workflows" / name).write_text(json.dumps(canvas, ensure_ascii=False, indent=2) + "\n")
shutil.copyfile(ROOT / "example_workflows" / name, WORKSPACE / "ComfyUI/user/default/workflows" / name)
link = WORKSPACE / "ComfyUI/custom_nodes/toonflow_shots"
if not link.exists() and not link.is_symlink():
    link.symlink_to(ROOT, target_is_directory=True)
elif link.resolve() != ROOT:
    raise RuntimeError(f"节点目录已被占用：{link}")
print("Installed:", link)
