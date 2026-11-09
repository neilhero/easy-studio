# Easy Studio 本机智能体接口 v1

## 范围与运行

本机 MCP stdio 代理和网页共用素材评审、质检、初剪、生成返修、剪映精修和整集制作功能。外部提供方由连接的宿主按项目和预算授权执行。

先启动一个Studio服务，明确允许导入的媒体目录：

```sh
python3 run.py --root '/项目目录' --port 8790 --import-root '/允许导入的目录'
```

`--import-root`可重复；不指定时禁止从外部导入。目录内符号链接也按解析后的实际路径校验。FFMPEG_BIN、FFPROBE_BIN和超分配置沿用README。耗时任务在Studio服务中运行，不在MCP代理内运行；代理断开不停止服务中的任务。服务重启后保留终态，未结束的任务显示中断，不自动重跑。

服务会在项目 `.studio/` 创建SQLite账本、文件权限为0600的agent-token及服务锁；该目录不可通过 `/f/` 获取，不提交Git。一个项目只允许一个Studio服务进程。SQLite保留项目/素材/镜头身份、幂等回执、任务索引；原JSON/Markdown仍是各功能的业务记录，不做破坏性迁移。查询时把原执行器状态协调到统一任务索引，支持网页创建的历史任务。取消与重试的能力以各执行器返回为准，质检目前不能取消。

## MCP接入

将以下内容作为支持本机stdio的宿主配置参考；不同宿主的配置位置和格式须分别核对。仓库安装本身不自动修改宿主全局配置，也不宣称ChatGPT远程会话能直连本机地址。当前机器已通过下述Codex CLI命令单独注册easy-studio。

```json
{
  "mcpServers": {
    "easy-studio": {
      "command": "/你的Python绝对路径/python3",
      "args": [
        "/代码目录/easy-studio/mcp_server.py",
        "--url", "http://127.0.0.1:8790",
        "--token-file", "/项目目录/.studio/agent-token"
      ]
    }
  }
}
```

支持MCP `2025-11-25`、`2025-06-18` stdio生命周期（initialize、notifications、tools/list、tools/call、ping），每行一个JSON-RPC消息，stdout只输出协议消息。明确不宣称支持2026版无状态协议。代理只连接127.0.0.1/localhost的HTTP根地址，不使用系统代理转发令牌。

协议参考：[MCP 2025-11-25生命周期](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/basic/lifecycle.mdx)、[工具规范](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2025-11-25/server/tools.mdx)。

## 整集制作（v1.0）

先获取 `studio_project` 的写入身份和修订号，再提交一次 `episode.start`。下面只展示该操作的 `arguments`。

```json
{
  "authorize": true,
  "template": "episode",
  "segments": ["S1", "S2", "S3"],
  "source": "current",
  "deliver": "rough",
  "acceptance": "director",
  "max_credits": 0,
  "max_attempts": 2
}
```

- `template` 还支持 `review`（只审候选）、`rough`（候选到初剪）、`repair`（指定一项返修）。`source=auto` 优先已有时间线，`source=candidates` 按指定镜号重新选片。
- 每个 `repairs` 项需给出 `segment/provider/issue/preserve`。本机用 `rife`。LibTV 另须提供 `libtv_project/model/prompt/quoted_credits`，参考、参数和水印沿用 `generation.submit` 的约定。每次重试使用已授权单次报价，累计受整集上限及每镜尝试次数约束。若需要将候选初剪写入工作草稿后返修，初始授权中须给 `adopt_rough=true`。
- `episode.get` 返回子任务。审查使用 `automation.claim/packet/submit`，应根据实际连续画面和声音记录检查范围。LibTV 使用 `generation.claim/record`；未知受理用 `recover=true` 查询同一请求。网页和 MCP 会显示等待原因。
- `episode.pause` 暂停新派发。重启后 `episode.resume` 认回已提交子任务。明确修改输入后用 `refresh=true` 开新一轮。新返修需要 `authorize_repairs=true` 与 `repairs`，仍受原授权预算与尝试次数限制。
- `deliver=jianying` 会生成可编辑草稿，并等待剪映原生导出、Studio 回收及全片复查。若有多份通过的回收版，使用 `episode.decide` 选定。Studio 初剪默认也需提交 `episode.decide`；用户明确授权按完整审查规则交付时才使用 `acceptance=reviewed`。
- `episode.report` 支持按 `id` 和 `cycle` 读交付报告。报告记录实际内容指纹、审查依据、已知费用和未知预留；技术成功与内容判断分别记载。

## 两个工具

- `studio_project`：返回当前项目ID、全局revision、导入授权目录、任务计数和全部操作说明。固定绑定服务启动时的项目；不接受任意路径切换。
- `studio_call`：参数为operation、arguments；写操作还须提供project_id、expected_revision、idempotency_key。读操作无需写入字段。

写入示例（ID、revision及路径用前一步的真实返回值）：

```json
{
  "operation": "assets.import",
  "arguments": {"source": "/允许导入的目录/镜1.mp4"},
  "project_id": "项目UUID",
  "expected_revision": "studio_project返回的revision",
  "idempotency_key": "本次导入操作唯一且可重放的键"
}
```

每次新的写操作前读取最新revision；同一操作因连接超时而重放时，保留整个原请求，不能更换其revision或参数。不同参数重复使用同一键会拒绝。回执已保存时返回原结果并附replayed；受理结果未完整落盘时返回needs_reconciliation，先查任务/文件与项目，不盲目重提。失败请求可能已有局部文件副作用，不能将SQLite回滚当成媒体文件也已撤销。修正参数后应重新核对并使用新键。

全局revision涵盖当前素材内容、分段表、评审、草稿、参考和问题/反馈记录及智能体写入序列。旧网页接口仍可使用，网页与智能体写入在服务中串行化；网页修改同样会使旧revision失效。文件被其他程序直接改动时，下次扫描/校验会发现变化，但不保证对任意外部程序提供跨文件原子事务。

## 操作契约

| 操作 | arguments |
|---|---|
| assets.list | offset=0，limit=50（1—200） |
| assets.import | source：获授权目录内的媒体绝对路径，复制后核验指纹 |
| assets.relink | asset_id，path：素材库/生成区内移动后的同内容相对路径；保留原ID，旧评审/草稿不自动转移 |
| shots.list | 无；返回原段号id、稳定shot_id及当前候选/评审 |
| shots.create | segment：唯一原段号；scene、text、duration可选。保留原分段表并备份；无法识别的旧表拒绝追加 |
| review.context | segment：原段号或shot_id；final：实际成片相对路径 |
| review.mutate | 原评审动作action/segment；asset_id可代替path；select/review必须附fingerprint；bind/accept必须附final/signature，退回须写note |
| edit.get | 无，读取当前保存的草稿 |
| edit.save | clips数组；每项id、asset_id或src、in/out（源秒）、a（音量，默认1）；可显式提供原生revision |
| edit.action | action：selected/import/undo/redo；可选原生revision |
| export.start | 可选原生草稿revision；返回任务与快照，不覆盖旧成片 |
| upscale.start | asset_ids或paths数组，scale=2/3，height=720/1080/1440/2160 |
| quality.scan | asset_id或path；接受候选或实际成片 |
| quality.reports | 无；返回指纹绑定的技术报告、疑点时间码 |
| quality.get | 无，参考卡/连戏资料 |
| quality.context | segment、final |
| quality.mutate | 原asset/shot/review操作字段；segment支持shot_id，可显式提供原生revision |
| issues.list | 无，返修问题及复核历史 |
| issues.mutate | 原create/status操作字段，见editing.py；具体成片必需 |
| tasks.list | offset=0，limit=50（1—200） |
| tasks.get | task_id：统一任务UUID |
| tasks.cancel | task_id；返回请求回执，继续查询终态；不支持的取消明确失败 |
| tasks.retry | task_id；失败/取消/中断时按原快照重试；源视频变化时须新建任务 |
| timeline.get | final可选；指定成片时只返回指纹匹配的冻结快照，旧项目需先人工核对 |
| evidence.extract | asset_id或path，t：有效源视频秒；返回新PNG、源指纹和文件定位，MCP工具同时返回小尺寸图片内容 |

统一任务包含id、kind、native_id、status、progress、outputs、error、can_cancel、can_retry；状态为queued/running/succeeded/failed/cancelled/interrupted。任务UUID在重启后不变。content_status固定为not_inferred，任务成功不改变任何候选过审或成片验收状态，内容判断仍读取原评审记录。新任务的原生JSON和统一索引同时保留；原JSON不删除、不修改历史批准。

素材身份与指纹分别记录：同路径改内容保留asset_id并增加素材revision；移动后的同内容可显式relink保留原ID。复制文件是新素材，不自动合并。原片未移动时禁止把另一份文件冒充原身份。镜头身份基于固定原段号登记；修改段号不是重命名功能，应显式处理新旧关联。

## 无页面操作验收

测试 `test_agent_api.py::test_http_auth_and_stdio_end_to_end` 启动真实stdio代理和独立临时HTTP服务，通过两个MCP工具完成：

1. 导入3条真实视频；创建3个镜头，关联并选用候选。
2. 运行技术质检，通过统一task_id等待实际终态。
3. 用稳定素材ID保存3段裁切，导出新MP4并读取冻结时间线。
4. 提取证据图，读取网页同一份候选、草稿和统一任务列表。
5. 验证仅选用素材仍保持待审，不伪造内容批准。

额外测试覆盖幂等重放、同键异参拒绝、网页修改后的旧revision拒绝、未知回执协调、跨项目拒绝、导入目录/符号链接限制、令牌不可经网页读取、单项目服务锁、历史任务发现、中断状态及稳定任务ID。

## 边界

本版提供工具调用与任务基础，不含自动导演规划、多模态自动审片、外部生成平台桥接或自由依赖图调度。API只处理本机授权项目，不是面向公网的认证服务。MCP代理已由真实进程验收；在具体智能体宿主的连接配置和授权仍需单独完成，当前对话不会因代码安装自动新增工具。


## 当前机器的Codex注册

本次升级已按[OpenAI Docs的MCP配置方式](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)使用 `codex mcp add easy-studio -- <Python> <代理路径> --url <本机服务> --token-file <令牌文件路径>` 新增连接，并通过 `codex mcp get easy-studio --json` 核对。配置只存令牌文件路径，不把令牌文本写进命令或Git。

该注册不等于当前聊天已热加载工具。新建本机Codex聊天或重新连接后，以是否发现studio_project/studio_call并能成功查询为准；本次已另用真实MCP进程对正式服务完成只读握手和项目查询。服务须保持运行，才能执行任务。


## v0.7新增

支持automation.start/list/get/packet/claim/submit/resume/cancel/evaluate；契约见[AUTOMATIC-REVIEW.md](AUTOMATIC-REVIEW.md)。流程中的候选与全片都需要真实审查；waiting_review不等于已完成。MCP的automation.packet返回采样图，完整动作/声音须在支持视频/声音的宿主中实际检查。


## v0.7.1 当前剪辑检查

`automation.workspace`只读返回当前使用素材、入出点、备用数量及可选版本；可传`source`和`final`。
`automation.start`现在默认`mode: "inspect"`：跟随已保存草稿，`segments`用于强制复查指定镜头，不传或空数组表示自动按变化检查。
如需旧版重新选片行为，必须显式传`mode: "rebuild"`，`segments`为参与初剪的镜头。建议另存，原草稿及原选片记录保留。
完成建议可用`automation.apply`（`run_id`、`draft_revision`）显式采用为草稿；原草稿可撤销，期间有修改则拒绝覆盖。
详细范围、缓存及历史报告行为见[AUTOMATIC-REVIEW.md](AUTOMATIC-REVIEW.md)。

## v0.8 单镜返修

新增 `generation.providers/list/get/submit/claim/record/retry/apply/export`，参见[模型生成与单镜返修](GENERATION.md)。
