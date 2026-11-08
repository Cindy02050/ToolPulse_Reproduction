# ToolPulse：实验复现

[English](README.md) | [简体中文](README.zh-CN.md)

**基于状态流跟踪，为任务型 agent 提供在线工具使用辅助。**

ToolPulse 持续跟踪 agent 的执行上下文和跨步骤的工具使用模式，在工具调用执行前检查拟执行动作，在执行后分析工具反馈。通过不断更新的状态，它识别缺乏依据的编辑、重复调用、反复失败以及无效的行为模式，并提供简短提醒或结合上下文的指导，由 agent 继续负责推进任务。

本仓库提供 ToolPulse 在 SWE-agent 上的实现，用于复现 **OmniCode Python、Java 和 C++ 代码修复实验**。仓库包含实验入口、固定版本的 SWE-agent 与 SWE-ReX 子模块、接入补丁、任务 ID 清单、抽样工具和实验结果文件。补丁正确性通过官方 OmniCode 评测流程验证。

**本仓库采用侵入式实现，直接修改 SWE-agent 内部的执行流程和消息处理逻辑，仅用于复现实验结果。**

## 工作原理

ToolPulse 将执行状态跟踪、症状检测和辅助生成连接起来：

```text
Agent 提出工具调用
  → 根据当前执行状态检查拟执行动作
  → 执行动作，或返回执行前提示
  → 分析已执行动作的工具反馈
  → 将辅助信息加入 agent 的下一次输入
```

默认的多粒度检测器识别五类症状：

| 症状 | 检测信号 |
|---|---|
| Blind Command（盲目操作） | 在当前任务中尚未查看文件就尝试编辑 |
| Calling Repetition（调用重复） | 连续执行动作和参数完全相同的调用 |
| Failure Inertia（失败惯性） | 缺少中间恢复信号，持续出现相同失败 |
| Functional Cycle（功能循环） | 重复出现相同的语义动作序列 |
| Strategic Fixation（策略固着） | 相邻交互窗口呈现高度相似的行为 |

SWE-agent 接入在动作执行前进行检查，在步骤执行后进行分析，并将提示加入 agent 后续输入。产生执行前提示时，控制权会在该动作执行前交回 agent。检测采用启发式方法，检测信号为 agent 的下一步决策提供上下文。

## 环境与安装

- Linux x86_64
- Python 3.13
- Git、Docker，以及访问 Docker 服务的权限
- 可获取的 OmniCode 任务镜像和模型服务

在仓库根目录初始化子模块并应用补丁：

```bash
git submodule update --init --recursive
git -C SWE-agent apply --check ../patches/sweagent.patch
git -C SWE-agent apply ../patches/sweagent.patch
git -C SWE-ReX apply --check ../patches/swerex.patch
git -C SWE-ReX apply ../patches/swerex.patch

python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

docker info
```

在安装 Python 包之前应用补丁，每个补丁只需应用一次。`requirements.txt` 以 editable 模式安装两个子模块，并固定第三方依赖版本。

| 依赖 | 来源 | 固定 commit |
|---|---|---|
| SWE-agent | [seal-research/SWE-agent](https://github.com/seal-research/SWE-agent) | `04bb72b888088b966f5539153b15b20e0218d348` |
| SWE-ReX | [seal-research/SWE-ReX](https://github.com/seal-research/SWE-ReX) | `9545022c29113c720de1d1803b3ce38574edbe1c` |

[patches/sweagent.patch](patches/sweagent.patch) 提供 ToolPulse、DefaultAgent 接入、提示配置、依赖和测试。[patches/swerex.patch](patches/swerex.patch) 将 Docker 回退启动命令改为通过 `python3 -m pipx` 调用 pipx。

## 任务数据

从 [Hugging Face 上的 OmniCode 数据集](https://huggingface.co/datasets/seal-research/OmniCode) 下载完整任务数据：

```bash
python -m pip install huggingface_hub
hf download seal-research/OmniCode --repo-type dataset --local-dir data
```

仓库提供每种语言各 30 个选中任务的 ID：

| 语言 | 任务数据文件 | 选中实例清单 | 运行模式 |
|---|---|---|---|
| Python | `data/omnicode_instances_python.json` | [python_sample_ids.txt](data/python_sample_ids.txt) | `bugfixing` |
| Java | `data/omnicode_instances_java.json` | [java_sample_ids.txt](data/java_sample_ids.txt) | `bugfixing-java` |
| C++ | `data/omnicode_instances_cpp.json` | [cpp_sample_ids.txt](data/cpp_sample_ids.txt) | `bugfixing-cpp` |

`--mode` 参数决定语言适配器、配置文件和 `omnicodeorg/omnicode` Docker 镜像的命名方式，任务数据文件可以重命名。镜像需已存在于本地或可从镜像仓库获取。

## 运行实验

### 单个实例

配置主 agent 的模型服务，然后运行一个 Python 任务：

```bash
export API_KEY="your-api-key"
export API_BASE="https://your-provider.example/v1"

python baselines/sweagent/sweagent_regular.py \
  --input_tasks data/omnicode_instances_python.json \
  --instance_ids astropy__astropy-7166 \
  --output_dir outputs/python \
  --model_name openai/YOUR_MODEL \
  --api_key "$API_KEY" \
  --api_base "$API_BASE" \
  --mode bugfixing \
  --thinking_budget 0 \
  --use_apptainer false \
  --g2 false \
  --agent.brain=multi_granularity \
  --agent.hint=remind_only
```

将模型名称和服务凭据替换为实际配置。运行 Java 或 C++ 时，根据上表替换数据文件、任务 ID、运行模式和输出目录。入口会为这些模式自动设置语言适配器。

### 选中任务集

运行选中的 30 个 Python 实例时，将上述命令的 `--instance_ids` 参数替换为：

```bash
--instance_ids "$(python -c 'import json; from pathlib import Path; print(",".join(json.loads(s) for s in Path("data/python_sample_ids.txt").read_text().splitlines() if s.strip()))')"
```

其他语言使用 `data/java_sample_ids.txt` 或 `data/cpp_sample_ids.txt`，并配套替换任务数据文件和运行模式。

入口输出每个实例的轨迹、`.pred` 文件以及汇总预测 `all_preds.jsonl`。再次运行时会跳过汇总文件中已记录的实例。建议为不同模型、语言、检测算法和提示配置分别使用独立输出目录。

`--per_instance_cost_limit` 默认为 `2.0`，`--total_cost_limit` 默认为 `0.0`；任一参数设为 `0.0` 表示关闭对应费用限制。模型调用次数限制还由 [baselines/sweagent/configs](baselines/sweagent/configs) 下对应语言的 YAML 文件配置。

## 检测算法与辅助方案

通过 `--agent.brain` 选择检测算法：

| 参数值 | 检测方式 |
|---|---|
| `multi_granularity` | 结合动作级检查、语义序列检测和窗口级行为分析；默认选项 |
| `matrix_profile` | 基于 Matrix Profile 特征检测相似交互窗口 |
| `sequitur_rule_based` | 使用 Sequitur 文法规则检测重复工具序列 |
| `sequitur_semantic_based` | 对语义动作标签使用 Sequitur 检测 |

通过 `--agent.hint` 选择辅助方案。应用补丁后，提示模板和模型提示词位于 `SWE-agent/config/toolpulse_hints.yaml`。

| 参数值 | 辅助内容 |
|---|---|
| `remind_only` | 简短描述检测到的症状；默认选项 |
| `remind_and_guide` | 症状描述与固定指导建议 |
| `different_message_type` | 使用 JSON 结构区分 `observation` 与 `hint` 字段 |
| `llm_coach` | 由辅助语言模型结合上下文生成指导 |
| `sequitur_default` | 面向 Sequitur 事件的提示模板 |
| `matrix_profile_default` | 面向 Matrix Profile 事件的提示模板 |

基于模板的提示无需辅助模型。使用 `--agent.hint=llm_coach` 时，单独配置提示模型：

```bash
export TOOLPULSE_LLM_API_KEY="your-hint-model-api-key"
export TOOLPULSE_LLM_BASE_URL="https://your-provider.example/v1"
export TOOLPULSE_LLM_MODEL="your-hint-model"
```

辅助模型根据任务描述、近期轨迹和检测到的行为生成指导。

## 抽样

复现选中的任务集时，直接使用仓库提供的 ID 清单。需要生成新样本时运行：

```bash
python scripts/sample_omnicode_benchmark.py \
  --input data/omnicode_instances_python.json \
  --n 30 --seed 20260609 --strategy balanced --score-profile simple
```

脚本仅依赖 Python 标准库，每行输出一个 JSON 引号包围的实例 ID。抽样结果取决于数据版本和参数。

## 评测与结果

使用 [官方 OmniCode 仓库](https://github.com/seal-research/OmniCode) 评测生成的预测。Python 按其 BugFixing 流程执行，Java/C++ 按 MSWEBugFixing 流程执行。以生成的 `all_preds.jsonl` 为预测产物，并按上游要求准备预测格式和评测环境。

[eva](eva) 保存六份实验结果 JSONL，保留其记录内容和字段名称。

## 仓库结构

```text
.
├── baselines/sweagent/   # 实验入口和配置
├── SWE-agent/           # 固定版本 agent 子模块
├── SWE-ReX/             # 固定版本执行环境子模块
├── patches/             # ToolPulse 接入与容器启动补丁
├── data/                # 选中任务 ID 清单
├── scripts/             # 任务抽样脚本
├── tests/               # 实验入口测试
├── eva/                 # 实验结果 JSONL
├── requirements.txt     # Python 依赖
├── README.md            # 英文说明
└── README.zh-CN.md       # 中文说明
```

## 来源与许可

任务数据、容器镜像和评测流程由 [OmniCode](https://github.com/seal-research/OmniCode) 提供。SWE-agent 与 SWE-ReX 保留上游版权声明和许可条款。第三方改编内容遵循其来源许可证，数据集按其发布条款获取和使用。
