# 来源记录

- 上游：https://github.com/feicaiclub/ad-storyboarder
- 来源提交：`ea420370a0881db7a179dbbb16d02f3f121a6492`
- 上游提交日期：2026-10-19（Asia/Shanghai）
- 独立版本整理日期：2026-11-08（Asia/Shanghai）
- 原始文件：`.claude/tools/studio.py`、`studio.html`、`common.py`
- MIT 许可证：原作者版权与完整许可证保留在根目录 LICENSE。

studio.py 与 studio.html 从已使用的本机版本原样提取，与上述上游源码一致。common.py 保留当前以 FFprobe 读取媒体的方式，并将个人绝对路径替换为环境变量/PATH 发现机制。新增启动入口、忽略规则和文档。

这是独立提取的维护仓库，并非 GitHub 平台上的完整上游 fork。没有包含广告生成工作流、外部服务凭据或个人影视项目文件。更新上游时应人工比较这三个工具文件，并保留本机媒体探测适配。

独立仓库将这三个工具文件放在根目录，并调整 common.py 的根路径和 run.py 的入口；功能保持一致。
