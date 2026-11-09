# v0.8 模型生成与单镜返修

网页打开「单镜返修」，选当前剪辑里的一镜，写清问题、需要保留的内容和证据时间码。生成结果只挂到该镜的候选。看片通过后选择候选入点，按旧镜时长等长替换并保存草稿，再导出新版。返修导出沿用原画幅。替换校验原剪辑和素材指纹，其他镜头的顺序、素材、时间与音量保留。剪辑已改变、候选未过审或可用长度不足时拒绝替换。

## 本地推理

配置 `STUDIO_RIFE_BIN` 为 [RIFE ncnn Vulkan 20221029](https://github.com/nihui/rife-ncnn-vulkan/releases/tag/20221029) 的绝对可执行路径，`STUDIO_RIFE_MODEL` 为包含 v4.6 的 `flownet.param`、`flownet.bin` 的目录。可通过 `STUDIO_RIFE_DEVICE=-1` 使用 CPU；默认设备 0。FFmpeg/FFprobe 沿用原设置。服务环境变更后重启。执行器对当前镜头的实际入出点运行真实插帧，单镜 0.16–12 秒，输入上限 1280×720，单任务串行。输出为补帧候选，按原速度封装并保留同区间音频；重新导出的成片沿用 Studio 的 25 fps 规格。这个本地模式适合动作补帧，不提供文生视频或重新表演。

## LibTV 宿主桥

Studio 保存请求与状态。连接 LibTV 的 Codex 宿主按以下步骤认领执行，操作均沿用 [智能体接口](AGENT-API.md) 的项目 ID、当前修订与幂等键。系统后台不会自行取用 LibTV 登录凭据。

1. `generation.submit` 接收 `provider:"libtv"`、`segment`、`issue`、`preserve`、可选 `evidence`，以及 `libtv_project`、`model`、`prompt`、`params`、`references`、`watermark`。填写当次预计积分 `quoted_credits`、本单上限 `max_credits`、含首次的 `max_attempts` 和明确的 `confirm_spend:true`。已有任务可传 `resume_task_id` 进入原任务查询，不会开始新生成。镜头 ID 可用项目段号或稳定镜头 ID。
2. 宿主先用 LibTV `doctor` 和 `target_project` 核对身份和精确画布，读取实时 `get_model.inputContract`。调用 `generation.claim`，传 `job_id` 和 `actor`，得到 30 分钟租约。提交前确认当前报价在预留内。纯文本用 `generation_submit`，绑定尝试的 `attempts[-1].key` 为 LibTV 幂等键，`kind:"video"`，并明确一个视频输出。首帧或图片/视频/声音参考用 `generate_video`，按实时工具契约传入已核对的原画布节点、相同尝试键和明确的 `intent:"generate"`。参考仅接受 `references.start_frame:[nodeId]` 或 `image/video/audio` 节点列表。带参考入口的权限关闭时，保留待处理状态。
3. 使用 `generation.record` 回写 `job_id`、`lease_token`、`outcome` 及原 `task_id`。提交、等待、成功、失败分别用 `submitted`、`pending`、`succeeded`、`failed`。失败须写 `reason`。可回填已确认的 `actual_credits`，未知留空。未知受理登记 `unknown`，下一次认领必须明确 `recover:true`，按原参数和原键核对回执，不创建新尝试。已有任务 ID 的阶段用 `task_status` 查询该 ID。旧任务回执过期时，仅在同画布节点的任务身份与最终视频明确匹配的情况下据此恢复。
4. 成功回执记录 `node_id` 和明确的 `output_index`。认领下载阶段，以原画布、节点、序号、稳定交付键及已记录的水印选择调用 `prepare_project_delivery`。宿主下载其授权链接到 Studio 的 `--import-root`，计算 SHA-256。以 `outcome:"downloaded"` 回写本地绝对 `source`、`sha256`、`delivery:{project_id,node_id,output_index,source_identity,watermark}`。Studio 校验关联、文件哈希和完整解码后发布候选。模型提交和交付均保留不同的证据，不把 URL 当作文件。
5. 只有已确认失败的外部尝试、本地失败/中断或已退回的候选可调用 `generation.retry`，传 `job_id`、当前 `attempt`，外部再次带报价与预算确认。未知扣费保留预留，已知实扣替换预留；累计占用达到上限会拒绝新尝试。预算约束的是 Studio 的派发，实际扣费以 LibTV 回执为准。

`generation.get/list` 查询状态和当前候选；`generation.providers` 显示本机模型配置。实际看片后用进度页或 `review.mutate` 提交内容判断。过审后调用 `generation.apply`，传 `job_id` 与可选源入点 `in`，随后用同一 `job_id` 调用 `generation.export`。新版本的接缝和全片复核可继续使用 v0.7 的「检查当前剪辑」。
