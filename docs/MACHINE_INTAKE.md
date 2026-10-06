# M1：新机器只读接入

M1 为每次接入保存机器档案、接口能力、建议配置和执行状态。它只读取当前进程可见的接口、查找工具文件和计算本工具模块摘要，不启动外部命令、性能负载或业务采集，不改变设备设置。Python 运行只依赖标准库；不需要 ROS、C++ 构建或 root。

已在原生 ARM64 Linux 容器验证安装、输出和失败路径。用户反馈三台 Orin/Thor 的机器信息与 M2a 安装通过，0.6.1 参数读取顺序修补已完成现场复核；Orin 完成真实进程资源关联，节点语义仍未验证。0.6.2 最终状态修补已收到三机通过反馈并收尾；后续M2b见 [业务功能关系指南](BUSINESS_MAPPING.md)。现场结论来自用户摘要，本机未取得原始设备记录。

## 安装与首次执行

按 [README 安装说明](../README.md#采集现有业务) 将当前源码安装到虚拟环境。激活后可在任意目录执行，输出路径相对于当前目录，也可使用绝对路径：

```bash
robot-perf-intake --view native-host --require-jetson --machine-id robot-demo \
  --output results/machine-intake-001
```

`--machine-id` 是操作者选择的非敏感代号；在同一台设备的不同轮次重复使用它可关联档案。省略时每次生成新的匿名代号。`--view` 可选 `unknown`、`native-host`、`container`，是操作者声明，不是自动宿主认证。容器中的设备接口可能来自挂载或透传，不能凭此证明原生执行。

`--require-jetson` 要求当前视图有 Jetson 识别证据；不要求具体 Orin/Thor 型号或保证 BSP 兼容。普通 Linux 可省略此项。源码目录内的等效入口为 `python3 -m perfkit.intake`，安装后同样可以用模块入口。

### 无 pip 或离线设备

若设备有 pip，准备机器生成 wheel、设备离线安装的方法见 [业务采集指南](BUSINESS_MONITOR.md#直接运行)。wheel 包含两个入口，不需要设备安装构建工具；先核对 Python 版本与文件摘要。

若有 venv 和 ensurepip，但没有 pip，可在新建的隔离环境中离线引导。下列命令在目标设备执行，wheel 由准备机器提供，路径替换成现场路径：

```bash
python3 -m venv --without-pip "$HOME/.venvs/robot-systems-perf"
"$HOME/.venvs/robot-systems-perf/bin/python" -m ensurepip --upgrade
"$HOME/.venvs/robot-systems-perf/bin/python" -m pip install --no-index \
  /path/to/robot_systems_perf-0.7.2-py3-none-any.whl
```

[Python ensurepip 官方说明](https://docs.python.org/3.10/library/ensurepip.html) 明确该引导不访问网络；发行版可能未提供它，不因此自动安装系统包。若 venv/ensurepip 均缺失，完整源码包可直接离线运行，在任意目录单次指定模块位置：

```bash
PYTHONPATH=/path/to/robot-systems-perf python3 -m perfkit.intake \
  --view native-host --require-jetson --skip-temperature \
  --output results/machine-intake-offline-001
```

这是源码模块入口，不产生 `robot-perf-intake` 安装命令；工具代码版本以模块摘要关联。若必须 wheel 安装，先按设备发行版准备匹配的 Python 打包组件，或使用已审阅的离线引导材料；不把开发集成使用的 pip zip 当设备已安装 pip 的证明。

## 人工补充字段

将 [机器补充模板](../templates/machine-metadata.template.json) 复制到设备本地，填写散热、部署版本等自动读取不到的信息；未知项保留 `null`。每个字段只接受不超过 512 字符的非空单行文字，拒绝未知字段。示例使用通用名称：

```bash
cp templates/machine-metadata.template.json machine-metadata.local.json
robot-perf-intake --view native-host --require-jetson --machine-id robot-demo \
  --metadata machine-metadata.local.json --output results/machine-intake-002
```

模板复制命令在源码目录执行；之后传入本地文件的实际路径。该补充文件应留设备，不提交现场版本、名称等私有信息。旧的 `platform-profile.template.json` 服务于既有测试环境记录，不能直接作为这里的新格式输入。

所有人工字段标为 `declared_not_verified`，空项为 `not_configured`。填写 ROS/RMW 或功耗模式，不证明业务实际加载的中间件或当前模式。M1 不运行 `nvpmodel -q`，功耗查询保留未查询原因。

## 五个输出文件

| 文件 | 内容 |
| --- | --- |
| `machine-profile.json` | 当前可见系统、架构、内核、设备型号/BSP、CPU/内存/块设备容量与接口来源；人工声明单列；读取窗口、代号、运行 ID、工具版本及模块 SHA256 |
| `capabilities.json` | proc/sysfs 等接口逐项可读状态与缺失原因；外部工具是否找到、版本/选项及采样验证状态 |
| `monitor-config.suggested.json` | 可通过现有 monitor 校验的 light 起点；每秒系统/进程采样、关闭线程与遥测、预算保持 null |
| `MACHINE_REPORT.md` | 简要能力报告、人工缺口、明确要求与下一步 |
| `intake-status.json` | 最终状态、错误、要求及已完成文件；这是执行结果的判断依据 |

内存为可见 MemTotal，块设备大小是容量，不是 I/O 性能；容器可见总内存不等于容器配额。接口的 `available=true` 只表示该能力至少一个来源可读，不表示所有字段齐全、事件归因可用或性能达标。检查具体来源与原因；字段缺失为 null，不替换成零。

找到工具仍可能版本不兼容或权限不足。M1 不执行版本/help/采样查询，因此找到的工具保持 `not_evaluated`；缺少可执行文件时版本/选项标为 `unavailable`，实际采样仍为未验证。需要资格验证时，显式使用源码仓库中的 [工具对照入口](TOOL_COMPARISON.md)。这些脚本不包含在 Python wheel 内，M1 不导入其结果冒充本轮验证。

M1 不读取内核启动参数、业务参数、环境变量全集、主机名或序列号。可选 `EP_SOURCE_REVISION` 仅作未验证的版本声明并标来源；实际运行代码用包版本和模块 SHA256 关联。结果仍可能包含设备型号、接口路径和人工声明，分享前需检查；原始结果放 `results/`，不进入公开源码。

## 失败、必需能力和中断

默认要求 Linux，其他读取缺口保留原因。只有明确要求某项能力时，缺失才使执行失败。例如必须有可读热接口时：

```bash
robot-perf-intake --require-jetson --require-capability thermal \
  --output results/machine-intake-required-001
```

名称来自 `capabilities.json` 的 `interfaces` 键，可重复指定；“必需”仍只检查至少一个接口可读。不设置 thermal 要求仍会探测温度，要跳过必须显式指定：

```bash
robot-perf-intake --view native-host --require-jetson --skip-temperature \
  --output results/machine-intake-no-temperature-001
```

此选项不枚举 thermal 目录、不访问温度接口；thermal 能力为 `status=skipped`，`available`/`present` 为 null，来源列表为空并带主动跳过原因。档案和状态保存 `skip_temperature=true`。同时设置 `--require-capability thermal` 时退出非零。0.6.1 将共享参数预检查提前到 CLI 的 metadata 读取之前；API 也保留校验，冲突请求不访问 metadata 或平台接口、不创建结果目录。默认继续探测温度，其他能力与建议采集配置不因此改变。

成功退出 0；非法输入、已有目录或必需项缺失退出非零；处理中 SIGINT/SIGTERM 退出 130。已经建立状态后发生异常，保留失败/中断状态及已写文件；初始化或磁盘写入本身失败时可能只有部分证据，不能视为成功。Markdown 是生成时快照，最终以状态 JSON 为准。已有结果目录一律拒绝覆盖，每轮换新目录。

## 接入后怎么采集

先检查档案与读取缺口，再复制建议配置，明确 UID、PID、名称或 cgroup 范围，按 [业务采集指南](BUSINESS_MONITOR.md) 运行 monitor。默认候选范围是当前用户的活动进程，不能直接认作业务算法清单；建议配置也没有自动完成节点/组件归属。资源预算和业务 deadline 未配置时保持未配置，不判通过。

Orin/Thor 首轮只需分别执行一次 M1、核对本地已知型号/BSP/内存等事实及缺失原因，确认状态和新目录行为。向项目反馈工具版本/模块摘要、脱敏能力状态、错误类型及本地核对差异即可，不必上传业务进程名、完整人工声明或原始机器档案。本增量没有修改性能路径，无需为验证 M1 重跑全量 C01/业务套件。
