# 商品混剪：实现前节点核对

来源：[引用会话](thread://01a0b54c-9397-7421-a94b-e7ee4bae61fb)；只读核对 `shop_content_creation/biz/video_remix_team/workflow/workflow.go`。这里的本地工作流保留 19 阶段的依赖关系，不创建业务服务任务、不调用调度器、不写业务数据库。

| 原流程阶段 | 本地实现及需要自定义的原因 |
| --- | --- |
| 01 InitializeContext | 自定义：校验商品、素材和执行规格，建立本地运行目录 |
| 02 TrafficGray | 自定义：解析显式功能开关；不冒充线上实验分流 |
| 03 SupplementVideo | 自定义：合并本地商家/历史/直播素材清单，去重；不查询商品数据库 |
| 04 ProcessMaterial | 自定义：探测、按已标注边界或场景变化切段、抽帧，保留人工/外部理解信息；不把文件名当视觉理解 |
| 05 PrepareSellingPoints | 自定义：整理已确认卖点与受众，缺失时要求补充，不编造商品功效 |
| 06 PrepareCreativeRecall | 自定义：本地创意库标签召回；关闭时返回明确的 skipped 状态 |
| 07 ScriptCoarseFilter | 自定义：按商品标签、描述和可用时长筛选，并输出结构化剧本任务给现有 Codex 节点 |
| 08 ScriptPlan | 自定义：接受人工/外部剧本或本地模板，真实配音测时、有限变速和最多两次备选台词校准；LLM 复用 CodexTextGenerate |
| 09 CoverDesign | 自定义：从素材抽帧并排版封面；可接现有生图节点的 IMAGE 输出 |
| 10 AIShoot | 自定义资源适配：无 AIGC 时跳过；接预生成本地视频或现有 H3 节点的 VIDEO，不重复开发生成模型 |
| 11 ResolveVisualSources | 自定义：把素材 ID/AIGC ID 解析为真实文件和区间 |
| 12 ValidateRequirement | 自定义：校验片段、声音、字幕策略、时间轴与文件可用性 |
| 13 PrepareVisuals | 自定义：消费清字幕结果并验证区间；擦除开启但没有结果时失败，不用模糊遮挡冒充擦除 |
| 14 PrepareVoices | 自定义：复用校准音频、按时间轴拼接原声/旁白/静音、生成句级字幕；原声分离必须提供实际人声文件 |
| 15 PrepareBGM | 自定义：读取指定 BGM 或本地音乐库按标签选择，未选音乐时明确跳过 |
| 16 PrepareRemix | 自定义：汇总画面、人声、BGM、封面，检查分支结果一致 |
| 17 BuildCloudEdit | 自定义：生成本地 EDL 和视频/人声/字幕/BGM/封面轨；不是未经核验的 VEdit ParamJson |
| 18 SubmitCloudEdit | 适配现有 RemixRender：FFmpeg 本地真实合成，增加封面覆盖；不向云剪服务提交 |
| 19 SuperResolution | 自定义结果适配：关闭时检查原片；开启时接入已生成超分文件并保留混音，不把普通缩放当 AI 超分 |
| 保存/审阅 | 复用原生 SaveVideo；新增报告节点落盘阶段状态、时间轴和媒体信息 |
| Euler RPC | 独立可选适配节点：按真实 IDL 构造请求、显式 Caller/GDPR、读取响应。默认回放本地响应；真实模式需要用户提供合法 IDL/路由/身份 |

19 个阶段保留在画布上便于与会话对照；它们是业务处理和契约适配，不是 19 个新模型。画布保留封面/AIGC 与画面/人声/BGM 分支，实际调度并行程度由 ComfyUI 决定。

## 本次验证边界

本机 ComfyUI + 项目已有视频 + macOS 中文语音 + FFmpeg 生成真实 MP4；验证开启/关闭配乐、原声、AIGC 资源回填、清字幕与超分结果接入，以及坏输入失败。内部 RPC 用本地替身验证请求构造、鉴权注入、返回码和响应转换，不将其标成远程联调成功。

Euler 依据：[如何在 Dorado Notebook 任务中使用 Euler](https://bytedance.larkoffice.com/docx/doxcntVKIIkEYcGdNuFIarJJcfc)。文档说明的是 `thriftpy2.load`、`euler.Client`、`Base.Caller` 和 `Base.Extra['gdpr-token']`，不包含混剪各个业务能力的 IDL 或权限；不能从这篇文档推断那些接口已经可以在 Mac 上调用。
