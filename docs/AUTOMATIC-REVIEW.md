# v0.7.1 当前剪辑检查与重新初剪

## 可以运行的链路

默认入口“检查当前剪辑”：读取已保存草稿 → 按实际素材/区间/音量生成检查预览 → 当前镜头、接缝及全片检查 → 报告及未决问题。不会重新挑候选。

“从候选重新初剪”：已有关联候选 → 视频/声音/参考与技术证据 → 实际语义审查 → 候选排序和可用入出点 → 独立初剪建议 → 接缝和全片复查 → 由用户决定是否采用。

入口：网页导航“镜头与候选”，或本机MCP `studio_call` 的 `automation.*` 操作。项目写操作继续要求project_id、expected_revision及稳定idempotency_key，见AGENT-API.md。

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
| automation.workspace | 可选source、final；当前使用素材、源区间、备用数量及可选工作版本 |
| automation.start | mode默认inspect；source=current/draft/final/legacy、final可选；inspect的segments为强制复查镜头（可空），rebuild的segments为参与初剪的镜头（1—20镜）；expected_dialogue台词映射、confidence默认0.8 |
| automation.apply | run_id、draft_revision；检查完成的重剪建议显式采用为草稿，可撤销；草稿修订有变化拒绝覆盖 |
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

统一tasks接口收录automation运行；waiting_review、needs_decision是明确状态。输入变化时input_stale为true，旧产物和历史保留，新运行重新检查；检查当前剪辑时，可按下述条件沿用上次检查；重新初剪仍重新审查候选。

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
- 重剪建议不自动修改草稿、原生selected、候选过审或成片验收。显式采用只修改草稿，撤销可恢复原片段。
- 开始运行时记录草稿revision；等待审查时如果其他人改了草稿，停止而不覆盖用户修改。
- 单次至多100个候选，每候选至多60秒，自动初剪至多300秒；证据文件需要本机空间。第一版串行运行，允许等待中的运行保留，不做复杂并发调度。
- 导出为新版本，保留原片与旧版；对最终文件重新执行技术检查、声音分析和视频证据生成。每个相邻切点前后约1秒产生接缝包，另有完整成片包。
- 完成语义复查后标“初剪已检查，待导演验收”。有问题或证据不足时needs_decision，保留问题与证据，可更换候选并新建运行；不自动付费补拍。
- 同运行中断后可resume，已完成证据复用；导出已登记任务ID时查询原任务。若在提交导出和保存回执的窗口中断，保留dispatching状态并拒绝盲目重提，先用统一任务和草稿核对。
- 重剪的参考/候选/要求变化必须新建运行。当前剪辑检查只依赖实际使用片段；未入片候选变化不影响当前检查。

## 当前剪辑、增量检查与旧报告

- `source=current`默认已保存草稿优先，其次调用方指定的可靠成片，再其次已有基础剪辑表。未保存的网页编辑不在检查范围。界面会显示检查对象；可显式选择草稿或某版成片。
- 草稿和基础剪辑表以保存的素材、入出点、顺序、音量另存检查预览，不覆盖手动草稿。含独立声音/字幕/特效的旧剪辑表不会被简化检查，须使用已有可靠快照的成片。当前检查支持1—100片段、总长300秒以内。
- 指定`source=final`时只检查该成片及其冻结镜头映射，不跟随之后的草稿修改。不根据最新剪辑表猜测旧成片的对应关系。
- 在同来源、同置信门槛的上次完整检查中，按素材内容、截取区间、音量、镜头/台词要求、参考及输出尺寸、渲染/证据实现匹配单镜；接缝要求左右两镜都匹配。接缝窗口最多各1秒，短镜不跨出这两镜，避免沿用相邻以外的旧内容。
- 只沿用通过且证据完整的结果。换中间一镜会重新检查该镜和左右接缝，未改动镜头可沿用；新增、删除、调序按新相邻关系匹配。全片总是对新版重新检查，判断总体故事、节奏和声音。
- 沿用包保留原视频与原时间码，`reused_from`记录来源运行，`current_range`记录新版位置；它是旧检查依据，不是对新视频逐帧重审。界面明确显示该区别。原报告文件与旧成片不改写。
- 剪辑完全未变且所有证据仍完整时，直接返回上次运行并标`reused_run`；勾选镜头可强制复查它及相邻接缝。参考、要求变化和证据损坏会使相关缓存失效。
- 当前检查中更换已保存片段，旧运行提示“剪辑已修改，请检查新版”，拒绝把旧判断提交到新版。旧报告及已生成影片保留。

## 评测与验证范围

自动流程及工作版本含12项测试，使用真实FFmpeg媒体，语义模型为明确标记的测试替身；覆盖宿主桥接、本地命令、候选评分选片、导出、接缝/全片复查、图像篡改、源变化、租约、覆盖不足、置信度、草稿冲突及未知受理窗口。连同已有功能共48项测试通过。本地Whisper另以静音音频实跑，返回空转录，未进行真实台词识别率评测。

automation.evaluate需要人工标签，正类为“有问题”；返回TP/FP/FN/TN、unresolved、precision、recall_on_decided、decision_coverage。pending/NA不计入已决定混淆矩阵，不能把该子集指标冒充全量准确率。当前没有真实《再西游》人工标注样本集的误报/漏报数据，应收集人物、道具、方向、动作及台词的小样后再决定自动采用门槛。

参考实现依据：[FFmpeg loudnorm](https://www.ffmpeg.org/ffmpeg-filters.html#loudnorm)、[faster-whisper本地模型与转写](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/transcribe.py)。模型/包为外部可选运行环境，Studio核心保持标准库依赖。
