import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

app.registerExtension({
  name: "short-drama.narration-remix.review",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!["RemixEditDecision", "RemixRender"].includes(nodeData.name)) return;
    const onExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      onExecuted?.apply(this, arguments);
      if (!this.remixReport) {
        this.remixReport = ComfyWidgets.STRING(this, "执行结果", ["STRING", { multiline: true }], app).widget;
        this.remixReport.inputEl.readOnly = true;
        this.remixReport.options.serialize = false;
      }
      this.remixReport.value = (message.text ?? []).join("\n");
      this.setSize([Math.max(this.size[0], 440), Math.max(this.size[1], 400)]);
      this.setDirtyCanvas(true, true);
    };
  },
});
