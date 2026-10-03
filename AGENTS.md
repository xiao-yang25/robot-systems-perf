# 工程入口

项目范围与指标原则见 [PERFORMANCE_PLAN.md](PERFORMANCE_PLAN.md)，实际已实现能力和执行方式见 [README.md](README.md)、[设备测试指南](docs/JETSON_RUNBOOK.md)。规划中的未实现指标不能在报告中冒充实测值。

- 当前支持同机 C01 ROS 2 跨进程、S01 周期任务与资源采样；可选GPU/EMC活动遥测；跨设备时钟、GPU推理事件和逐事件内核分析另行实现。
- 现有业务通过 [业务采集指南](docs/BUSINESS_MONITOR.md) 的 monitor 入口观察。自动发现只表示候选，不冒充算法识别或 ROS 节点/回调时延；外部进程只读，不拥有其生命周期，不发送信号。
- 保留原始 CSV、环境、配置和运行状态；失败运行退出非零，不覆盖已有结果。
- 延迟是完成样本的条件分布，未完成及无效交付另列；消息未交付不能直接判为网络丢包。
- 资源缺失用 null 与原因表示；计数器身份、重置与窗口覆盖必须验证。采样对照有缺失交付或输入质量问题时不能给预算通过。
- Python 运行依赖限标准库；C++ 以 ROS 2 与 Linux 接口为基础。容器测试不得使用不同 CPU 架构仿真来报告目标平台性能。
- 检查入口：`python3 -m unittest discover -s tests -v`、`bash -n scripts/run-docker.sh scripts/run-native.sh`、容器内 `python3 tests/integration_checks.py` 与短套件实测。修改进程清理或指标统计时补相关故障与独立已知数据验证。
- `results/`、`build/` 和离线 bundle 不进入源码提交；不要发布设备现场信息或本机路径。
