import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

app.registerExtension({
  name: "short-drama.product-remix.report",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!["ProductRemixBuildTimeline", "ProductRemixReport"].includes(nodeData.name)) return;
    const original = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      original?.apply(this, arguments);
      if (!this.productRemixReport) {
        this.productRemixReport = ComfyWidgets.STRING(this, "执行结果", ["STRING", { multiline: true }], app).widget;
        this.productRemixReport.inputEl.readOnly = true;
        this.productRemixReport.options.serialize = false;
      }
      this.productRemixReport.value = (message.text ?? []).join("\n");
      this.setDirtyCanvas(true, true);
    };
  },
});
