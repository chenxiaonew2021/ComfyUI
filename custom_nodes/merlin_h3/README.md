# Merlin H3 · ComfyUI 视频节点

复用 Toonflow 的本地 `merlin-h3-service`，通过 Worker 调用 Merlin 的 FL2VA / Ref2VA。ComfyUI 保持 CPU 模式，无需下载模型权重或填写 AK。两者共享 H3 队列、并发和生成资源；ComfyUI 提交的任务会出现在 H3 任务门户，但不会自动成为 Toonflow 项目的素材。

## 已安装的节点

| 节点 | 输入 | 输出 |
| --- | --- | --- |
| Merlin H3 · FL2VA 提交 | 提示词、可选首帧、可选尾帧 | `task`、`task_id` |
| Merlin H3 · Ref2VA 提交 | 提示词、最多 9 张参考图 / 3 段视频 / 3 段音频，可混用 | `task`、`task_id` |
| Merlin H3 · 获取视频 | 连接 `task`，或手动填写已有 `task_id` | 标准 `VIDEO`、`task_id` |

FL2VA 四种输入均可：无图（T2VA）、仅首帧、仅尾帧、两帧。每个首尾帧端口仅接收一张图，尾帧不会被当成首帧。

Ref2VA 按 [MiniMax 官方规格](https://github.com/MiniMax-AI/MiniMax-H3/blob/d21241f0a4b3acbb34c97dae47fa417b7065e438/README.zh-CN.md#L80-L85) 支持混合参考：

| 类型 | 数量上限 | 时长 |
| --- | --- | --- |
| 图片 | 9 张 | 每个 `reference_image_1`–`9` 端口支持 IMAGE batch，按图片总数计数 |
| 视频 | 3 段 | 每段 2–15 秒，全部参考视频累计不超过 15 秒 |
| 独立音频 | 3 段 | 每段 2–15 秒，全部独立音频累计不超过 15 秒 |

**三类合计最多 12 个，至少一张图片或一段视频；不能仅用音频。** 视频自身的音轨不占独立音频文件数量。

视频输入接内置 `Load Video`，转为 MP4 上传。第一段视频沿用端口名 `reference_video`，其余为 `reference_video_2`、`reference_video_3`，保留原工作流连线。音频接 `Load Audio` 的 `AUDIO` 输出，端口为 `reference_audio_1`–`3`，每个端口一个音频，转为 PCM16 WAV 后上传，单个 WAV 不超过 15 MiB。本地 H3 上传接口也接受 MP3（`audio/mpeg`）或 WAV（`audio/wav`）。

同类素材按已连接端口顺序排列，图片 batch 内顺序也保留；未连接端口跳过。提示词通过 `<Picture 1>`、`<Video 1>`、`<Audio 1>` 引用，各类分别从 1 编号。数据结构和 Worker 文件转发参考 `/Users/bytedance/projects/minimax-h3/minimax_h3_task_portal`；本地服务已同步音频接入和素材顺序，不需修改原 Portal 部署。

## 日常使用

在项目根目录启动：

```bash
./start-merlin-h3.command
./start-comfyui.command
```

已经运行的服务无需重复启动。H3 任务门户位于 `http://127.0.0.1:18082`；推理还需要 Worker 能访问内部服务。当前状态以门户的两个通道各自可用 Pod 数量为准。

在 ComfyUI 工作流列表中打开：

- **H3 FL2VA 文生视频**：无需参考图；可接入首帧和尾帧。
- **H3 Ref2VA 参考图生视频**：在 Load Image 中选择自己的参考图。
- **H3 Ref2VA 参考视频生视频**：在 Load Video 中上传参考视频后运行。
- **H3 Ref2VA 图像视频音频混合参考**：同时接入三类参考；可断开不需要的素材，至少保留视觉参考。
- **文生图到 H3 视频**：先生成一张图片，再作为 Ref2VA 参考图。
- **H3 按任务编号取回视频**：查询已经提交的任务，不重新生成。

基本连线：

```text
Load Image / 文生图 → H3 Ref2VA 提交 → H3 获取视频 → Save Video
                               └ task_id → Preview as Text
```

节点和示例默认 **5 步**，便于快速试跑；`inference_steps` 仍为可调正整数，正式生成可手动提高。本地 H3 API 在省略该字段时也默认 5 步，显式传入的步数原样送到推理服务。已有任务保持提交时的参数。API 地址 `http://127.0.0.1:18081`，最长等待 120 分钟。示例默认 4 秒、16:9，固定 768P；时长支持 4–15 秒整数，画幅支持 21:9、16:9、4:3、1:1、3:4、9:16。视频包含原生音频，输出 `VIDEO` 保留音画；不提供静音或模型 seed 控件。

Save Video 默认保存到 `ComfyUI/output/MerlinH3/`，可改文件名前缀。

## 长任务、重试和恢复

提交节点立即返回任务编号；获取节点负责等待、下载。只需提交时，断开获取节点并仅运行提交节点，或使用 `api/fl2va-submit.json` / `api/ref2va-submit.json`。它们是输出节点，没有保存视频节点也可以提交。

- **相同输入复用已有任务**：素材内容、参数、`generation_id` 都不变时，从本地记录读取原任务。
- **生成新版本**：手动修改 `generation_id`，例如从 0 改为 1。它只控制任务去重，不是模型 seed。
- **记录落盘**：`ComfyUI/user/merlin_h3/jobs/*.json` 保存确切请求、幂等键和任务编号。不要在任务未确认时删除记录。
- **提交响应丢失**：重跑相同输入会使用原素材 asset ID、原请求、原幂等键。提交超过 23 小时仍未确认时停止自动重提，避免超过上游 24 小时幂等期后重复生成。
- **等待超时或关闭 ComfyUI**：任务由 H3 服务继续处理。用恢复工作流填写 `task_id` 取回视频；无需重跑上游生图节点。尤其是 文生图节点每次运行可能产生新图，重新运行整个链路会提交新的视频任务。
- **取消**：ComfyUI 的取消只停止本地等待，不取消 H3 任务。当前 H3 服务不允许取消已运行的任务。
- **远端失败**：显示任务编号和服务错误，原任务记录保留；确认需要新任务后修改 `generation_id`。

任务编号会写入 ComfyUI 日志、提交节点历史输出的 `h3_task` / `text` 字段，并连接到示例中的文本预览。轮询期间状态和排队位置写入日志。结果下载中断可以单独重试获取节点。

H3 结果遵循服务自身的保留期；本地任务记录不等于永久保存远端结果，请及时用 Save Video 保存。

## 接口和 MCP

节点调用现有本地接口，不依赖 Toonflow 进程：

1. `POST /v2/uploads`：multipart `file`，获得 `asset://...`。
2. `POST /v2/video_generation`：`model=MiniMax-H3`、提示词、素材角色、分辨率、时长、画幅、步数；附带 `Idempotency-Key`。
3. `GET /v2/query/video_generation/{task_id}`：读取 queued / running / succeeded / failed / cancelled。
4. `GET /results/{token}`：流式下载完整 MP4，不把视频先解码为一大批图片。

`api/` 目录是 ComfyUI API 格式示例，`example_workflows/` 是 UI 格式。`api/ref2va-mixed-submit.json` 展示图像、视频和音频的组合，需要将 `reference.mp4`、`reference.wav` 替换为实际已上传到 ComfyUI 的文件名。MCP 可通过 `run_workflow` 运行；长任务建议先执行提交工作流，从历史输出取得 H3 task_id，再执行 `api/get-video.json`。后者可异步提交到 ComfyUI，再查询 ComfyUI prompt_id。H3 task_id 与 ComfyUI prompt_id 是两个不同的编号。

复用依赖：Pillow、requests 和 ComfyUI 自带视频类型；未添加 Python 依赖。节点源码位于 `ComfyUI/custom_nodes/merlin_h3`。

本地服务的 `0005_order_reference_assets` 迁移保留新任务中的素材顺序；启动器会自动执行待应用迁移。Worker 沿用 Portal 的 multipart 协议：单张图片用 `input_reference`，视频、多文件和包含音频的请求使用重复的 `input_references`，按素材 MIME 分类。没有新增独立的 `input_audio` 文件字段。

未运行测试套件或提交真实视频生成；实际推理需在 H3 服务启动并具备可用实例后执行。
