"""Run ToolPulse code-repair experiments on OmniCode tasks."""

from __future__ import annotations

import argparse
import csv
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from tqdm import tqdm

from sweagent.run.run import main as sweagent_main
from sweagent.utils.log import get_logger

CONFIG_DIR = Path(__file__).resolve().parent / "configs"
MODE_LANGUAGES = {
    "bugfixing": "python",
    "bugfixing-java": "java",
    "bugfixing-cpp": "cpp",
}
logger = get_logger(__name__)


def parse_bool(value: str) -> bool:
    """Accept the boolean spellings supported by the experiment CLI."""
    normalized = value.lower()
    if normalized in {"yes", "true", "t", "y", "1"}:
        return True
    if normalized in {"no", "false", "f", "n", "0"}:
        return False
    raise argparse.ArgumentTypeError("Boolean value expected.")


def load_tasks(input_path: Path) -> list[dict[str, Any]]:
    """Load local JSON, JSONL, CSV, or a Hugging Face dataset's test split."""
    if input_path.exists():
        with input_path.open(encoding="utf-8", newline="") as stream:
            suffix = input_path.suffix.lower()
            if suffix == ".json":
                tasks = json.load(stream)
            elif suffix == ".jsonl":
                tasks = [json.loads(line) for line in stream if line.strip()]
            elif suffix == ".csv":
                tasks = list(csv.DictReader(stream))
            else:
                raise ValueError(f"Unsupported task file type: {input_path.suffix}")
    else:
        from datasets import load_dataset

        tasks = load_dataset(str(input_path))

    if isinstance(tasks, dict):
        tasks = tasks.get("test", tasks)
    if hasattr(tasks, "to_list"):
        tasks = tasks.to_list()
    if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
        raise ValueError("Dataset must contain a list of task dictionaries or a test split.")
    return tasks


def run_instance(
    instance: dict[str, Any],
    model_name: str,
    api_key: str | None,
    output_dir: Path,
    mode: str = "bugfixing",
    thinking_budget: int | None = None,
    use_apptainer: bool = False,
    g2: bool = False,
    api_base: str | None = None,
    per_instance_cost_limit: float = 2.0,
    total_cost_limit: float = 0.0,
    extra_args: list[str] | None = None,
) -> dict[str, Any]:
    """Run one task and return its SWE-agent prediction payload unchanged."""
    if mode not in MODE_LANGUAGES:
        raise ValueError(f"Unknown bugfix mode: {mode}")
    if thinking_budget is not None and thinking_budget < 0:
        raise ValueError("Thinking budget must be non-negative.")
    if thinking_budget and not model_name.startswith("gemini"):
        raise ValueError("A positive thinking budget is supported only for Gemini models.")

    language = MODE_LANGUAGES[mode]
    config_suffix = "" if language == "python" else f"_{language}"
    model_suffix = "_gpt-5-mini" if "gpt-5-mini" in model_name else ""
    config_file = CONFIG_DIR / f"bugfixing{config_suffix}{model_suffix}.yaml"
    image_tag = instance["instance_id"] if language == "python" else f"{instance['repo'].replace('/', '_')}_base"

    with tempfile.TemporaryDirectory(prefix="toolpulse-task-") as temporary_dir:
        statement_path = Path(temporary_dir) / "problem_statement.txt"
        statement_path.write_text(instance["problem_statement"], encoding="utf-8")
        args = [
            "run", "--config", str(config_file),
            f"--agent.model.name={model_name}",
            f"--agent.model.per_instance_cost_limit={per_instance_cost_limit}",
            f"--agent.model.total_cost_limit={total_cost_limit}",
            f"--env.repo.github_url=https://github.com/{instance['repo']}",
            f"--env.repo.base_commit={instance['base_commit']}",
            f"--env.deployment.image=omnicodeorg/omnicode:{image_tag}",
            '--env.deployment.docker_args=["-w","/"]',
            f"--problem_statement.path={statement_path}",
            f"--problem_statement.id={instance['instance_id']}",
            f"--output_dir={output_dir}",
        ]
        if api_key is not None:
            args.append(f"--agent.model.api_key={api_key}")
        if api_base is not None:
            args.append(f"--agent.model.api_base={api_base}")
        if language != "python":
            args.append(f"--agent.language={language}")
        if thinking_budget:
            kwargs = {"thinking": {"type": "enabled", "budget_tokens": thinking_budget}}
            args.append(f"--agent.model.completion_kwargs={json.dumps(kwargs, separators=(',', ':'))}")
        args.extend(extra_args or [])
        # The pinned SWE-agent dispatcher consumes these final two arguments.
        args.extend([f"--use_apptainer={str(use_apptainer).lower()}", f"--g2={str(g2).lower()}"])
        sweagent_main(args)

    prediction_path = output_dir / instance["instance_id"] / f"{instance['instance_id']}.pred"
    return json.loads(prediction_path.read_text(encoding="utf-8"))


def _completed_ids(output_file: Path) -> set[str]:
    """Read completed IDs while ignoring malformed aggregate records."""
    completed = set()
    if output_file.exists():
        with output_file.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    record = None
                if isinstance(record, dict) and isinstance(record.get("instance_id"), str):
                    completed.add(record["instance_id"])
                else:
                    logger.warning("Skipping invalid prediction record at %s:%d", output_file, line_number)
    return completed


def _append_prediction(output_file: Path, prediction: dict[str, Any]) -> None:
    """Append one durable prediction record under an exclusive file lock."""
    with output_file.open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell():
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    stream.write(b"\n")
            stream.write((json.dumps(prediction) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def run_tasks(
    input_tasks_path: Path,
    output_dir_path: Path,
    model_name: str,
    api_key: str | None,
    instance_ids: list[str] | None = None,
    mode: str = "bugfixing",
    thinking_budget: int | None = None,
    use_apptainer: bool = False,
    g2: bool = False,
    output_file: Path | None = None,
    api_base: str | None = None,
    per_instance_cost_limit: float = 2.0,
    total_cost_limit: float = 0.0,
    extra_args: list[str] | None = None,
) -> None:
    """Run selected tasks, resuming from the aggregate prediction file."""
    if mode not in MODE_LANGUAGES:
        raise ValueError(f"Unknown bugfix mode: {mode}")
    tasks = load_tasks(input_tasks_path)
    if instance_ids is not None:
        selected_ids = set(instance_ids)
        tasks = [task for task in tasks if task["instance_id"] in selected_ids]

    output_dir_path.mkdir(parents=True, exist_ok=True)
    prediction_file = output_file if output_file is not None else output_dir_path / "all_preds.jsonl"
    prediction_file.parent.mkdir(parents=True, exist_ok=True)
    completed = _completed_ids(prediction_file)
    logger.info("Read %d already completed ids from %s", len(completed), prediction_file)

    for task in tqdm(tasks, desc=f"Inference for {model_name}"):
        instance_id = task["instance_id"]
        if instance_id in completed:
            continue
        prediction = run_instance(
            task, model_name=model_name, api_key=api_key, output_dir=output_dir_path,
            mode=mode, thinking_budget=thinking_budget, use_apptainer=use_apptainer, g2=g2,
            api_base=api_base, per_instance_cost_limit=per_instance_cost_limit,
            total_cost_limit=total_cost_limit, extra_args=extra_args,
        )
        _append_prediction(prediction_file, {
            "instance_id": instance_id,
            "model_name_or_path": model_name,
            "full_output": None,
            "model_patch": prediction,
        })
        completed.add(instance_id)


def main(argv: list[str] | None = None) -> None:
    """Parse experiment options and forward remaining options to SWE-agent."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-i", "--input_tasks", type=Path, required=True)
    parser.add_argument("--instance_ids", help="Comma-separated task instance IDs")
    parser.add_argument("-o", "--output_dir", type=Path, required=True)
    parser.add_argument("-m", "--model_name", default="gemini/gemini-2.5-flash")
    parser.add_argument("-k", "--api_key")
    parser.add_argument("-b", "--api_base", help="API base URL for third-party relay")
    parser.add_argument("--per_instance_cost_limit", type=float, default=2.0, help="Per-instance cost limit (0 disables)")
    parser.add_argument("--total_cost_limit", type=float, default=0.0, help="Total cost limit (0 disables)")
    parser.add_argument("--mode", default="bugfixing", choices=MODE_LANGUAGES)
    parser.add_argument("--thinking_budget", type=int, default=0, help="Positive Gemini thinking budget; 0 leaves model defaults")
    parser.add_argument("--use_apptainer", type=parse_bool, default=False, help="Use Apptainer instead of Docker")
    parser.add_argument("--g2", type=parse_bool, default=False, help="Use the Apptainer /scratch layout")
    parser.add_argument("--output_file", type=Path, help="Custom aggregate prediction JSONL path")
    args, extra_args = parser.parse_known_args(argv)
    if args.thinking_budget < 0:
        parser.error("--thinking_budget must be non-negative")
    if args.thinking_budget and not args.model_name.startswith("gemini"):
        parser.error("A positive --thinking_budget is supported only for Gemini models")
    run_tasks(
        input_tasks_path=args.input_tasks, output_dir_path=args.output_dir,
        model_name=args.model_name, api_key=args.api_key,
        instance_ids=args.instance_ids.split(",") if args.instance_ids else None,
        mode=args.mode, thinking_budget=args.thinking_budget or None,
        use_apptainer=args.use_apptainer, g2=args.g2, output_file=args.output_file,
        api_base=args.api_base, per_instance_cost_limit=args.per_instance_cost_limit,
        total_cost_limit=args.total_cost_limit, extra_args=extra_args,
    )


if __name__ == "__main__":
    main()
