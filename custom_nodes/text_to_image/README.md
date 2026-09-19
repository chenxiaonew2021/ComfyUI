# 文生图 · ComfyUI 图片生成

独立自定义节点，直接使用图片服务密钥，默认模型为 `gpt-image-2`。无需 Comfy Credits 或本地模型权重，支持当前 CPU 模式。依赖均已包含在本项目的 ComfyUI 环境中。

另有 **参考图生图**（`ReferenceImageGenerate`）：连接 `reference_images`，通过配置的参考图生图地址发送 multipart `image[]`。用于参考人物设定绘制分镜、参考首帧绘制尾帧；支持 IMAGE batch 提供多张参考，沿用项目已有的参考图协议。

## 使用

1. 节点代码位于 `ComfyUI/custom_nodes/text_to_image`，随 ComfyUI 工程发布。
2. 在工作流列表打开 **文生图**，或导入 `example_workflows/text-to-image.json`。
3. 填写提示词，调整 `size`、`quality`、`n`，点击运行。
4. `文生图` 输出连接到内置 `Save Image`，图片保存在 ComfyUI 的输出目录下，文件前缀为 `TextToImage/gpt-image-2`。

默认生成一张 `1024x1024` / `low` 图片。其他尺寸按服务商支持情况填写，例如 `1280x720`、`2048x1152`、`3840x2160`。`size=auto` 或 `quality=auto` 会省略相应请求字段。多张结果作为 IMAGE batch 传给保存节点；所有图片应具有相同尺寸。

## 凭据与接口

服务密钥按以下顺序读取，每次运行时重新读取：

1. ComfyUI 进程的 `IMAGE_GENERATION_API_KEY` 环境变量。
2. `~/.config/comfyui/image_generation_api_key`，单行文本，建议权限 `0600`。

请求地址可通过环境变量配置，环境变量优先于节点中的 `base_url` 输入：

1. `TEXT_TO_IMAGE_BASE_URL`：文生图服务基础地址。
2. `REFERENCE_IMAGE_BASE_URL`：参考图生图服务基础地址。

例如，在启动 ComfyUI 的同一个终端中执行：

```bash
export TEXT_TO_IMAGE_BASE_URL="https://<your-image-service>/text-to-image"
export REFERENCE_IMAGE_BASE_URL="https://<your-image-service>/image-edit"
./start-comfyui.command
```

设置环境变量后需要重启 ComfyUI；如果没有设置环境变量，也可以在节点的 `base_url` 中手工填写地址。

工作流、API JSON、节点定义和源码不含服务密钥。更改进程环境变量需重启；节点只接受 HTTPS 地址，并拒绝带查询参数或密钥的 URL。

服务地址由 `TEXT_TO_IMAGE_BASE_URL` 和 `REFERENCE_IMAGE_BASE_URL` 提供，代码和工作流不保存具体部署域名。

文生图使用 `POST <base_url>/images/generations`，服务密钥放入查询参数 `ak`，附带 `X-TT-LOGID`。JSON 字段为 `model`、`prompt`、`n`、`size`、`quality`。支持 `data[].b64_json` 和 `data[].url`；下载图片不携带服务密钥。协议采用 OpenAI-compatible 文生图接口。

默认每次运行都会重新请求。两个节点均可开启 `reuse_result`，在相同提示词、模型、参数、参考图和 `generation_id` 下复用已成功保存的图片，缓存位于 `ComfyUI/user/text_to_image/cache/`。修改编号可生成新图；它不是模型随机种子。旧工作流默认关闭复用，行为不变。

没有自动重试。超时或取消不代表上游停止生成；网络请求期间取消会在请求返回或超时后被处理。错误仅展示脱敏信息和 logID。

## API 调用

`workflow_api.json` 是相同流程的 API 格式。修改节点 `1` 的 `inputs.prompt` 等字段，将整个文件内容放入 `POST /prompt` 请求的 `prompt` 字段。凭据仍由服务端读取，无需在请求体中传 AK。通过 `/history/{prompt_id}` 获取图片信息，再调用 `/view` 下载。

## 本地节点调研

2026-09-18 查询本地 `/object_info`：共 928 个已加载节点。官方 `OpenAIGPTImage1` / `OpenAIGPTImageNodeV2` 使用隐藏的 Comfy 账户凭据和 `/proxy/openai/images/generations` 路径，没有独立的服务地址 / 密钥配置；故新增此独立扩展，不修改 ComfyUI 核心或官方节点。
