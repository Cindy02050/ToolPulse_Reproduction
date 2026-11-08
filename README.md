# ToolPulse: Experiment Reproduction

[English](README.md) | [简体中文](README.zh-CN.md)

**State-flow-tracking online tool-use assistance for task-oriented agents.**

ToolPulse tracks an agent's execution context and temporal tool-use patterns across interactions. It examines proposed tool calls before execution and analyzes tool feedback afterward, using the evolving state to identify ungrounded edits, repeated calls, recurring failures, and unproductive patterns. Assistance takes the form of a reminder or context-specific guidance, while the agent remains responsible for completing the task.

This repository provides the SWE-agent implementation for reproducing ToolPulse code-repair experiments on **OmniCode Python, Java, and C++ tasks**. It includes the experiment runner, pinned SWE-agent and SWE-ReX submodules, integration patches, task ID lists, sampling tools, and experiment result files. Patch correctness is evaluated with the official OmniCode evaluation pipeline.

**This repository contains an invasive implementation of ToolPulse that directly modifies SWE-agent's internal execution and message-handling logic. It is intended solely for reproducing the experimental results.**

## How ToolPulse works

ToolPulse combines execution-state tracking, symptom detection, and assistance generation:

```text
Agent proposes a tool call
  → Inspect the proposed action using the current execution state
  → Execute the action, or return a pre-execution hint
  → Analyze feedback from an executed action
  → Add assistance to the next agent input
```

The default multi-granularity detector identifies five symptoms:

| Symptom | Signal |
|---|---|
| Blind Command | Editing a file before inspecting it in the current task |
| Calling Repetition | Consecutive calls with the same action and arguments |
| Failure Inertia | Recurring failures without an intervening recovery signal |
| Functional Cycle | Repeated semantic action sequences |
| Strategic Fixation | Highly similar behavior across consecutive interaction windows |

The SWE-agent integration checks actions before execution and analyzes completed steps afterward. Hints are included in the agent's subsequent input. A pre-execution hint returns control to the agent before the proposed action runs. Detection is heuristic; its signals provide context for the agent's next decision.

## Requirements and installation

- Linux x86_64
- Python 3.13
- Git and Docker, with access to the Docker daemon
- Access to the OmniCode task images and a model service

From the repository root, initialize the submodules and apply the patches:

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

Apply the patches once, before installing the Python packages. `requirements.txt` installs both submodules in editable mode and pins the third-party dependencies.

| Dependency | Source | Pinned commit |
|---|---|---|
| SWE-agent | [seal-research/SWE-agent](https://github.com/seal-research/SWE-agent) | `04bb72b888088b966f5539153b15b20e0218d348` |
| SWE-ReX | [seal-research/SWE-ReX](https://github.com/seal-research/SWE-ReX) | `9545022c29113c720de1d1803b3ce38574edbe1c` |

[patches/sweagent.patch](patches/sweagent.patch) adds ToolPulse, the DefaultAgent integration, hint configuration, dependencies, and tests. [patches/swerex.patch](patches/swerex.patch) changes the Docker fallback startup command to invoke pipx through `python3 -m pipx`.

## Task data

Download the full task data from [OmniCode on Hugging Face](https://huggingface.co/datasets/seal-research/OmniCode):

```bash
python -m pip install huggingface_hub
hf download seal-research/OmniCode --repo-type dataset --local-dir data
```

The repository includes 30 selected task IDs per language:

| Language | Task data file | Selected IDs | Runner mode |
|---|---|---|---|
| Python | `data/omnicode_instances_python.json` | [python_sample_ids.txt](data/python_sample_ids.txt) | `bugfixing` |
| Java | `data/omnicode_instances_java.json` | [java_sample_ids.txt](data/java_sample_ids.txt) | `bugfixing-java` |
| C++ | `data/omnicode_instances_cpp.json` | [cpp_sample_ids.txt](data/cpp_sample_ids.txt) | `bugfixing-cpp` |

The `--mode` option selects the language adapter, configuration, and `omnicodeorg/omnicode` Docker image naming scheme. Task data files may be renamed. The images must be available locally or accessible from the registry.

## Run experiments

### One instance

Configure the main agent's model service and run a Python task:

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

Replace the model name and service credentials with your provider's settings. For Java or C++, use the corresponding data file, task ID, mode, and output directory from the table above. The runner sets the language adapter for these modes automatically.

### Selected task set

To run the 30 selected Python instances, replace the `--instance_ids` argument in the command above with:

```bash
--instance_ids "$(python -c 'import json; from pathlib import Path; print(",".join(json.loads(s) for s in Path("data/python_sample_ids.txt").read_text().splitlines() if s.strip()))')"
```

Use `data/java_sample_ids.txt` or `data/cpp_sample_ids.txt` for the other languages, together with the matching task data file and mode.

The runner writes per-instance trajectories and `.pred` files, plus an aggregated `all_preds.jsonl`. Instances already recorded in the aggregate file are skipped on subsequent runs. Use a separate output directory for each model, language, detector, and hint configuration.

`--per_instance_cost_limit` defaults to `2.0`; `--total_cost_limit` defaults to `0.0`. Setting either to `0.0` disables that cost limit. Model-call limits are also configured in the language-specific YAML files under [baselines/sweagent/configs](baselines/sweagent/configs).

## Detectors and assistance

Select a detector with `--agent.brain`:

| Value | Detection approach |
|---|---|
| `multi_granularity` | Combines action-level checks, semantic sequence detection, and window-based behavior analysis; the default |
| `matrix_profile` | Detects similar interaction windows using Matrix Profile features |
| `sequitur_rule_based` | Detects recurring tool sequences using Sequitur grammar rules |
| `sequitur_semantic_based` | Applies Sequitur to semantic action labels |

Select assistance with `--agent.hint`. Hint templates and model prompts are defined in `SWE-agent/config/toolpulse_hints.yaml` after applying the patch.

| Value | Assistance |
|---|---|
| `remind_only` | Brief symptom descriptions; the default |
| `remind_and_guide` | Symptom descriptions with fixed guidance |
| `different_message_type` | Structured JSON feedback with separate `observation` and `hint` fields |
| `llm_coach` | Context-specific guidance from an auxiliary language model |
| `sequitur_default` | Templates for Sequitur events |
| `matrix_profile_default` | Templates for Matrix Profile events |

Template-based hints require no auxiliary model. To use `--agent.hint=llm_coach`, configure the hint model separately:

```bash
export TOOLPULSE_LLM_API_KEY="your-hint-model-api-key"
export TOOLPULSE_LLM_BASE_URL="https://your-provider.example/v1"
export TOOLPULSE_LLM_MODEL="your-hint-model"
```

The auxiliary model receives the task description, recent trajectory, and detected behavior to generate guidance.

## Sampling

Use the included ID lists to reproduce the selected task sets. To generate a new sample:

```bash
python scripts/sample_omnicode_benchmark.py \
  --input data/omnicode_instances_python.json \
  --n 30 --seed 20260609 --strategy balanced --score-profile simple
```

The script uses only the Python standard library and prints one JSON-quoted instance ID per line. Sampling results depend on the dataset version and parameters.

## Evaluation and results

Evaluate generated predictions using the [official OmniCode repository](https://github.com/seal-research/OmniCode). Follow its BugFixing instructions for Python and MSWEBugFixing instructions for Java/C++. Use the generated `all_preds.jsonl` as the prediction artifact and follow the upstream requirements for prediction formatting and evaluation setup.

[eva](eva) contains six experiment result JSONL files. These files retain their recorded contents and field names.

## Repository structure

```text
.
├── baselines/sweagent/   # Experiment runner and configurations
├── SWE-agent/           # Pinned agent submodule
├── SWE-ReX/             # Pinned execution-environment submodule
├── patches/             # ToolPulse integration and container startup patches
├── data/                # Selected task ID lists
├── scripts/             # Task sampling script
├── tests/               # Experiment runner tests
├── eva/                 # Experiment result JSONL files
├── requirements.txt     # Python dependencies
├── README.md            # English documentation
└── README.zh-CN.md       # Chinese documentation
```

## Sources and licenses

Task data, container images, and the evaluation pipeline are provided by [OmniCode](https://github.com/seal-research/OmniCode). SWE-agent and SWE-ReX retain their upstream copyright notices and license terms. Third-party adaptations remain subject to their source licenses; obtain and use the dataset under its published terms.
