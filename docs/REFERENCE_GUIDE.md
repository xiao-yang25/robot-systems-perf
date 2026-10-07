# 官方与开源测量参考资料

核对日期：2026-10-07。资料用于指标定义、实验协议与工具适配，不证明工具已安装、已在 Orin/Thor 运行或本项目符合统一认证标准。

## 阅读顺序

| 问题 | 推荐资料 | 用于本项目 |
| --- | --- | --- |
| 怎样看到运行中的节点和通信接口 | [ROS 2 topic/rqt_graph 教程](https://github.com/ros2/ros2_documentation/blob/humble/source/Tutorials/Beginner-CLI-Tools/Understanding-ROS2-Topics/Understanding-ROS2-Topics.rst) | 先画发现拓扑；不自动推断样本处理路径 |
| 哪些 QoS 字段影响通信语义 | [ROS 2 QoS](https://github.com/ros2/ros2_documentation/blob/humble/source/Concepts/Intermediate/About-Quality-of-Service-Settings.rst) | 保存实际端点信息；QoS deadline 与业务完成 deadline 分开 |
| 如何测回调与关联框架事件 | [ros2_tracing](https://github.com/ros2/ros2_tracing/tree/humble)、[tracetools_analysis](https://github.com/ros-tracing/tracetools_analysis/tree/humble) | 匹配 SDK/分支，先验证真实 tracepoints、初始化及时间域，再导入 |
| 复杂节点内部怎样关联输入输出 | [CARET 架构](https://tier4.github.io/caret_doc/main/design/software_architecture/)、[内部路径](https://tier4.github.io/caret_doc/main/configuration/intra_node_data_path/) | 复用追踪分析；业务路径需明确关系，不能按最近时间戳拼接 |
| 怎样组织可重复 ROS 图基准 | [NVIDIA ros2_benchmark](https://github.com/NVIDIA-ISAAC-ROS/ros2_benchmark) | 固定输入、播放速率、监测边界、重复运行及来源信息 |
| 通信微基准记录哪些指标 | [ROS 2 buildfarm_perf_tests](https://github.com/ros2/buildfarm_perf_tests) | 对照时延、速率、丢失、CPU、RSS；历史 RMW 列表不当作当前兼容认证 |
| 怎样选代表性机器人负载 | [RobotPerf](https://github.com/robotperf/benchmarks) | 参考感知、定位、导航、控制、操作分类；不把本项目简化算法称为官方完整栈 |
| CPU/GPU/内存平台状态怎样读 | [Tegrastats r38.2](https://docs.nvidia.com/jetson/archives/r38.2/DeveloperGuide/AT/JetsonLinuxDevelopmentTools/TegrastatsUtility.html)、[Nsight Systems](https://docs.nvidia.com/nsight-systems/UserGuide/index.html) | 整机指标和事件时间线分开；EMC% 不等于 GB/s，GPU 活动不等于推理时延 |
| 线程是否等待 CPU | [Linux schedstat](https://docs.kernel.org/scheduler/sched-stats.html) | 累计运行/运行队列等待辅助诊断，不冒充逐事件等待原因 |

REP-2014 的 [ROS 性能基准提案](https://github.com/ros-infrastructure/rep/blob/master/rep-2014.rst)当前状态为 **Rejected**。可阅读其讨论，不能称为已采纳标准。上述来源同样不规定适用于所有机器人业务的统一时延、CPU 或成功率上限。

## 下载与复现

公开下载目录被 Git/Docker 忽略，避免第三方源码、体积较大的页面和设备记录混入项目代码。此前 2026-10-03 的资料仍保留，本次补充与当前 Humble 参考环境相关的追踪、图工具、基准和路径文档。

跟踪在 Git 中的 `configs/reference-sources.json` 固定 GitHub 文档/源码提交和文件摘要；HTML 中的 rolling-page 标明滚动页面，只记录取得的字节摘要，不伪称网站版本固定。分支名是参考来源，不是实机兼容认证；在 Thor 上必须重新核对 BSP、ROS、Python、RMW 与工具版本。

在另一台机器 clone 后下载到新目录：

```bash
python3 scripts/download_references.py \
  --output reference-downloads/reference-001
```

此脚本为源码工具，不在 wheel 安装命令中。只使用 Python 标准库；HTTPS、公有来源、有界大小/超时；不自动安装、执行、解压归档或修改代理。按 shell 的标准代理环境使用，仓库不包含本机代理地址。每个条目只尝试一次，失败返回非零并记录，不靠重复下载掩盖失败。

输出：

- `INDEX.md`：离线文件入口。
- `manifest.json`：目录状态、取得时间、catalog 摘要、每项来源/提交/文件大小/实际 SHA256/错误。
- Markdown/RST/HTML 文档、源码归档及对应许可证。RST 可用文本编辑器离线阅读；HTML 不包含完整在线站点资源，归档不递归包含子模块或外部数据集。
- 下载/格式/预期 SHA256 失败保留 `.part` 及失败记录，不冒充可用文件；已有目录拒绝覆盖。

源码/固定文档摘要用于复核下载字节，不能替代上游签名验证。滚动 HTML 的哈希只在本地清单记录；下载新快照可能变化。长期离线复现应复制已验证包，不假定滚动网页总能重新取得同样字节。下载也不证明构建、事件支持或目标平台性能。

本机另提供 `reference-downloads/performance-references-2026-10-07.tar.gz`，包含既有 2026-10-03 和新增 2026-10-07 两组公开资料与各自清单；复制该包即可离线查阅。它不会随 clone 获取，也不会自动发布到 GitHub。源码包保留 LICENSE/COPYING；下载资料不成为产品运行依赖。

本次新增21项均已下载并独立复核 SHA256：4个源码归档、12份文本/许可证、5个HTML页面，约4.03MiB；合并离线包约23MiB，并提供同名 `.sha256` 文件。rqt_graph 的 `ros2` 历史分支为1.2.1，BSD许可文本在源文件头部，没有独立 LICENSE；它仅作参考，不能据此推断当前 Humble/Thor 兼容。其余版本和具体文件以 catalog/manifest 为准。

## 采用的方法

1. 先核对负载、消息类型/大小、QoS、时间边界、吞吐分母和有效交付契约，再比较同名指标。
2. 固定输入、软件/镜像与设备运行条件；记录预热、测量窗口、重复次数和失败运行，分别报告短测与性能验收。
3. 同时报告条件时延分布和未完成/丢弃/无效/超时，不能仅凭完成样本 P99 宣布业务通过。
4. 采集器及事件导出开销独立计量；稳定同输入做无采集/采集对照，预算未配置保持 not_configured。
5. 资源、发现拓扑、追踪路径和业务终态分层关联；缺身份、时钟或完整性依据保持 unresolved/not_evaluated。

项目设计与现有实现边界见 [拓扑和追踪接入](ROS_TOPOLOGY_AND_TRACING.md)；更广的工具对照及历史下载见 [工具参考](TOOL_LANDSCAPE.md)。
