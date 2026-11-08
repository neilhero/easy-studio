# v0.7 自动审片与初剪

## 可以运行的链路

已有关联候选 → 视频/声音/参考与技术证据 → 实际语义审查 → 候选排序和可用入出点 → 独立初剪导出 → 接缝和全片复查 → 初剪报告及未决问题。

入口：网页导航“自动审片与初剪”，或本机MCP `studio_call` 的 `automation.*` 操作。项目写操作继续要求project_id、expected_revision及稳定idempotency_key，见AGENT-API.md。

**语义检查有实际执行者才会继续。** 本版提供已接通的本地命令适配器和宿主智能体认领接口，不附带视觉模型，不假设电脑已有视觉模型服务。没有配置命令时准备证据后明确显示waiting_review，由连接Studio的智能体实际检查再提交；仅凭技术成功或缺少回复不自动通过。后台命令配置也不使当前聊天自动持续执行任务。

当前机器已验证本地Whisper可用，常驻服务配置STUDIO_ASR_PYTHON及STUDIO_ASR_MODEL后可转写。未配置、失败、无音轨分别记录；转写不代替试听。实际短剧候选质量仍需用人工标注样本评测，本版不宣称已达到某个审片准确率。

## 证据与覆盖

- 每个候选保留最多12张均匀采样帧，注明源时间和采样间隔；完整范围的视频审查副本；有音轨时生成16kHz单声道WAV。
- 候选包含镜头内容、目标时长、连戏要求、所引用参考卡版本和预期台词。准确逐字台词通过expected_dialogue显式提供，不从描述文字中猜测台词。
- 复用技术检测，检查解码/黑帧/静帧/静音/音轨等。技术疑点必须有审查说明才能进入自动选片；疑点不一定是内容错误。
- 测量输入LUFS、真峰值、响度范围等loudnorm分析信息；静音或不能得到有限值时用null，不伪造数值，不自动改变声音。
- 可选本地faster-whisper转写，保留词级时间、模型路径/版本/指纹；与显式台词做规范化文字相似度比较，仅作为可能漏词/错词的疑点。
- 源视频、证据图片/片段/音频均记录内容指纹。源、参考、镜头要求、候选集合变化后不能沿用旧运行的判断。证据文件缺失/被覆盖时拒绝审查提交。
- 动作检查通过必须声明实际检查的视频范围并引用clip；有声候选必须声明试听范围。全片与接缝复查必须覆盖要求的完整视频范围，有声时也须覆盖声音。没有完成的检查保持pending，不得伪报覆盖。
- 覆盖记录是受信审查执行者的声明，不是Studio能独立证明执行者真的观看过的证据。模型可能误判；保留判断来源、说明和置信度，不自动获得导演验收。

## MCP 操作

| operation | arguments |
|---|---|
| automation.start | segments段号数组，1—20镜；expected_dialogue可选段号→台词映射；confidence默认0.8，范围0.5—1 |
| automation.list | 无，运行列表 |
| automation.get | run_id，读取阶段、包、选片理由、问题与成片 |
| automation.image | run_id、packet_id、image_id（frame-N/reference-N），单张采样或参考图，MCP返回图像内容 |
| automation.packet | run_id、packet_id，读已校验的证据包；MCP同时返回采样图像内容。视频/声音路径须由有对应能力的审查宿主实际读取 |
| automation.claim | run_id、actor；认领一份待审包，租约15分钟，返回lease token与包签名 |
| automation.submit | run_id、packet_id、lease_token、signature、result |
| automation.reopen | run_id、packet_id、note，保留旧判断并补充复查；已导出候选依据不可改写 |
| automation.resume | run_id；显式恢复，复用本运行已成功生成的证据和检查记录 |
| automation.cancel | run_id；中止流程，已发出且不能取消的独立技术质检可能继续 |
| automation.evaluate | run_id、labels数组，人工标注项为packet_id/check/expected(pass或fail)，统计混淆矩阵与决定覆盖率 |

统一tasks接口收录automation运行；waiting_review、needs_decision是明确状态。输入变化时input_stale为true，旧产物和历史保留，新运行重新检查；当前不跨新运行复用语义判断。

### 审查结果契约

result须包含：model（模型名及版本或人工身份）、reason、score(0—100)、usable=[源入点,源出点]、checks、watched_ranges、listened_ranges。技术报告有疑点时填写具体technical_disposition说明。

checks恰好包含identity、props、direction、action、dialogue五项，每项为：

```json
{
  "status": "pending",
  "confidence": 0,
  "evidence": ["frame-0", "clip"],
  "note": "尚未检查完整动作，不能确认"
}
```

状态为pass/fail/pending/na；pass必须有已存在的证据ID，置信度低于本次门槛不能入选。全标NA不能入选。有明确台词时dialogue不能标NA，无音轨时不能判台词通过。NA必须有合理说明，不能用于绕过需要检查的声音或动作。

watched_ranges/listened_ranges使用原视频时钟，不是审查副本从0开始的播放时钟。例如接缝副本覆盖源影片12—14秒，实际完整观看对应[12,14]。采样帧不能代替连续动作检查，转写不能代替试听。

### 本地审查命令适配

在服务环境设置STUDIO_REVIEW_COMMAND为JSON命令数组，例如 `["/模型环境/python","/你编写的审查适配器.py"]`。首项必须是绝对路径；不经shell运行，不从项目备注或提示词读取命令。

命令通过stdin接收JSON：project_root、packet、checks及固定的资料信任提示。packet的媒体路径相对于project_root。适配器必须真实调用或使用你部署的审查模型，处理其输入能力和返回，再在stdout输出单个result JSON；日志写stderr。单次超时300秒，用户取消时回收子进程。输出仍受同样的签名、证据和覆盖规则约束。

未配置命令时默认使用宿主智能体桥接：反复claim→packet实际读取→submit，所有候选提交后会自动继续；导出后会出现新的接缝/全片审查包，同样提交后才完成。

## 初剪、恢复与复查

- 候选按实际审查评分选用，保存可用源区间和理由；不按分辨率、文件名“最新”或模型名选片。
- 只把建议选片登记为selected，不把它自动升级为原生候选过审或成片验收。
- 开始运行时记录草稿revision；等待审查时如果其他人改了草稿，停止而不覆盖用户修改。
- 单次至多100个候选，每候选至多60秒，自动初剪至多300秒；证据文件需要本机空间。第一版串行运行，允许等待中的运行保留，不做复杂并发调度。
- 导出为新版本，保留原片与旧版；对最终文件重新执行技术检查、声音分析和视频证据生成。每个相邻切点前后约1秒产生接缝包，另有完整成片包。
- 完成语义复查后标“初剪已检查，待导演验收”。有问题或证据不足时needs_decision，保留问题与证据，可更换候选并新建运行；不自动付费补拍。
- 同运行中断后可resume，已完成证据复用；导出已登记任务ID时查询原任务。若在提交导出和保存回执的窗口中断，保留dispatching状态并拒绝盲目重提，先用统一任务和草稿核对。
- 参考/候选/要求变化必须新建运行；当前不做跨运行最小依赖重算。已有素材流程稳定后再扩展单镜生成返修。

## 评测与验证范围

本版含6项自动流程测试，使用真实FFmpeg媒体，语义模型为明确标记的测试替身；覆盖宿主桥接、本地命令、候选评分选片、导出、接缝/全片复查、图像篡改、源变化、租约、覆盖不足、置信度、草稿冲突及未知受理窗口。连同已有功能共42项测试通过。本地Whisper另以静音音频实跑，返回空转录，未进行真实台词识别率评测。

automation.evaluate需要人工标签，正类为“有问题”；返回TP/FP/FN/TN、unresolved、precision、recall_on_decided、decision_coverage。pending/NA不计入已决定混淆矩阵，不能把该子集指标冒充全量准确率。当前没有真实《再西游》人工标注样本集的误报/漏报数据，应收集人物、道具、方向、动作及台词的小样后再决定自动采用门槛。

参考实现依据：[FFmpeg loudnorm](https://www.ffmpeg.org/ffmpeg-filters.html#loudnorm)、[faster-whisper本地模型与转写](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py)。模型/包为外部可选运行环境，Studio核心保持标准库依赖。
