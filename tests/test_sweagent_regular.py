from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

RUNNER_PATH = Path(__file__).resolve().parents[1] / "baselines" / "sweagent" / "sweagent_regular.py"
SPEC = importlib.util.spec_from_file_location("toolpulse_experiment_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.fixture
def instance():
    return {
        "instance_id": "owner__repo-1",
        "repo": "owner/repo",
        "base_commit": "abc123",
        "problem_statement": "Fix the issue. Preserve Unicode: café.",
    }


@pytest.fixture
def dispatch(monkeypatch, tmp_path, instance):
    calls = []
    prediction = {"instance_id": instance["instance_id"], "model_patch": "diff --git a/a b/a"}

    def execute(args):
        calls.append(list(args))
        options = dict(arg[2:].split("=", 1) for arg in args if arg.startswith("--") and "=" in arg)
        statement_path = Path(options["problem_statement.path"])
        assert statement_path.read_text(encoding="utf-8") == instance["problem_statement"]
        prediction_dir = Path(options["output_dir"]) / instance["instance_id"]
        prediction_dir.mkdir(parents=True, exist_ok=True)
        (prediction_dir / f"{instance['instance_id']}.pred").write_text(json.dumps(prediction), encoding="utf-8")

    monkeypatch.setattr(runner, "sweagent_main", execute)
    return calls, prediction


@pytest.mark.parametrize("mode,language,image", [
    ("bugfixing", "python", "owner__repo-1"),
    ("bugfixing-java", "java", "owner_repo_base"),
    ("bugfixing-cpp", "cpp", "owner_repo_base"),
])
@pytest.mark.parametrize("model_name,variant", [
    ("openai/glm-5.3-flash", ""), ("openai/gpt-5-mini", "_gpt-5-mini"),
])
def test_mode_selects_language_image_and_configuration(instance, dispatch, tmp_path, mode, language, image, model_name, variant):
    calls, prediction = dispatch
    result = runner.run_instance(instance, model_name=model_name, api_key=None, output_dir=tmp_path, mode=mode)
    args = calls[0]
    language_suffix = "" if language == "python" else f"_{language}"
    assert Path(args[args.index("--config") + 1]).name == f"bugfixing{language_suffix}{variant}.yaml"
    assert f"--env.deployment.image=omnicodeorg/omnicode:{image}" in args
    if language != "python":
        assert f"--agent.language={language}" in args
    assert "--env.repo.github_url=https://github.com/owner/repo" in args
    assert "--env.repo.base_commit=abc123" in args
    assert '--env.deployment.docker_args=["-w","/"]' in args
    assert result == prediction
    statement_arg = next(arg for arg in args if arg.startswith("--problem_statement.path="))
    assert not Path(statement_arg.split("=", 1)[1]).exists()


@pytest.mark.parametrize("use_apptainer,g2", [(False, False), (True, False), (True, True)])
def test_agent_overrides_precede_backend_dispatch_flags(instance, dispatch, tmp_path, use_apptainer, g2):
    calls, _ = dispatch
    runner.run_instance(
        instance, model_name="openai/model", api_key="test-placeholder-key", output_dir=tmp_path,
        api_base="https://example.test/v1", per_instance_cost_limit=1.5, total_cost_limit=4.0,
        use_apptainer=use_apptainer, g2=g2,
        extra_args=["--agent.brain=sequitur_semantic_based", "--agent.hint=matrix_profile_default"],
    )
    args = calls[0]
    assert args[-4:] == [
        "--agent.brain=sequitur_semantic_based", "--agent.hint=matrix_profile_default",
        f"--use_apptainer={str(use_apptainer).lower()}", f"--g2={str(g2).lower()}",
    ]
    assert "--agent.model.api_key=test-placeholder-key" in args
    assert "--agent.model.api_base=https://example.test/v1" in args
    assert "--agent.model.per_instance_cost_limit=1.5" in args
    assert "--agent.model.total_cost_limit=4.0" in args


def test_problem_statement_is_removed_after_execution_error(instance, monkeypatch, tmp_path):
    paths = []

    def fail(args):
        paths.append(Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--problem_statement.path="))))
        assert paths[0].exists()
        raise RuntimeError("container failed")

    monkeypatch.setattr(runner, "sweagent_main", fail)
    with pytest.raises(RuntimeError, match="container failed"):
        runner.run_instance(instance, model_name="openai/model", api_key=None, output_dir=tmp_path)
    assert len(paths) == 1 and not paths[0].exists()


@pytest.mark.parametrize("budget", [None, 0])
def test_disabled_thinking_adds_no_completion_override(instance, dispatch, tmp_path, budget):
    calls, _ = dispatch
    runner.run_instance(instance, model_name="openai/model", api_key=None, output_dir=tmp_path, thinking_budget=budget)
    assert not any(arg.startswith("--agent.model.completion_kwargs=") for arg in calls[0])


def test_positive_gemini_thinking_budget_is_forwarded(instance, dispatch, tmp_path):
    calls, _ = dispatch
    runner.run_instance(instance, model_name="gemini/gemini-2.5-flash", api_key=None, output_dir=tmp_path, thinking_budget=1024)
    arg = next(arg for arg in calls[0] if arg.startswith("--agent.model.completion_kwargs="))
    assert json.loads(arg.split("=", 1)[1]) == {"thinking": {"type": "enabled", "budget_tokens": 1024}}


def test_unsupported_thinking_budget_fails_before_dispatch(instance, dispatch, tmp_path):
    calls, _ = dispatch
    with pytest.raises(ValueError, match="Gemini"):
        runner.run_instance(instance, model_name="openai/model", api_key=None, output_dir=tmp_path, thinking_budget=1024)
    assert calls == []


@pytest.mark.parametrize("mode", ["testgen", "reviewfix", "stylereview-java-checkstyle"])
def test_removed_modes_fail_before_dispatch(instance, dispatch, tmp_path, mode):
    calls, _ = dispatch
    with pytest.raises(ValueError, match="mode"):
        runner.run_instance(instance, model_name="openai/model", api_key=None, output_dir=tmp_path, mode=mode)
    assert calls == []


@pytest.mark.parametrize("file_format", ["json", "json-wrapper", "jsonl", "csv"])
def test_loads_supported_local_task_files(tmp_path, instance, file_format):
    suffix = "json" if file_format == "json-wrapper" else file_format
    path = tmp_path / f"renamed-tasks.{suffix}"
    if file_format == "json":
        path.write_text(json.dumps([instance]), encoding="utf-8")
    elif file_format == "json-wrapper":
        path.write_text(json.dumps({"test": [instance]}), encoding="utf-8")
    elif file_format == "jsonl":
        path.write_text("\n" + json.dumps(instance) + "\n\n", encoding="utf-8")
    else:
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=instance)
            writer.writeheader()
            writer.writerow(instance)
    assert runner.load_tasks(path) == [instance]


def test_loads_hugging_face_test_split(monkeypatch, instance):
    import datasets
    dataset = datasets.DatasetDict({"test": datasets.Dataset.from_list([instance])})
    monkeypatch.setattr(datasets, "load_dataset", lambda identifier: dataset)
    assert runner.load_tasks(Path("test-org/test-dataset")) == [instance]


@pytest.mark.parametrize("content", [{"wrong": []}, ["not a task"], None])
def test_rejects_invalid_task_data(tmp_path, content):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(ValueError, match="task|dataset|Dataset"):
        runner.load_tasks(path)


def test_rejects_unsupported_local_format(tmp_path):
    path = tmp_path / "tasks.txt"
    path.write_text("content", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported"):
        runner.load_tasks(path)


def test_batch_preserves_prediction_schema_resume_and_filters(monkeypatch, tmp_path, instance):
    done = {**instance, "instance_id": "owner__repo-done"}
    excluded = {**instance, "instance_id": "owner__repo-excluded"}
    task_path = tmp_path / "renamed.json"
    task_path.write_text(json.dumps([done, instance, instance, excluded]))
    output_file = tmp_path / "predictions" / "custom.jsonl"
    output_file.parent.mkdir()
    previous = {"instance_id": done["instance_id"], "full_output": None, "model_patch": {"model_patch": "old"}}
    output_file.write_text(json.dumps(previous) + "\n\n")
    prediction = {"model_patch": "new patch"}
    execute = Mock(return_value=prediction)
    monkeypatch.setattr(runner, "run_instance", execute)
    output_dir = tmp_path / "outputs"
    runner.run_tasks(
        task_path, output_dir, "openai/model", None,
        instance_ids=[done["instance_id"], instance["instance_id"]],
        mode="bugfixing-java", output_file=output_file,
    )
    assert execute.call_count == 1
    assert execute.call_args.args[0] == instance
    assert execute.call_args.kwargs["mode"] == "bugfixing-java"
    rows = [json.loads(line) for line in output_file.read_text().splitlines() if line.strip()]
    assert rows == [previous, {
        "instance_id": instance["instance_id"], "model_name_or_path": "openai/model",
        "full_output": None, "model_patch": prediction,
    }]
    assert not (output_dir / "all_preds.jsonl").exists()


def test_corrupt_resume_records_do_not_block_new_tasks(monkeypatch, tmp_path, instance):
    task_path = tmp_path / "tasks.json"
    task_path.write_text(json.dumps([instance]))
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    aggregate = output_dir / "all_preds.jsonl"
    aggregate.write_text('not-json\n{}\n[]\n')
    execute = Mock(return_value={"model_patch": ""})
    monkeypatch.setattr(runner, "run_instance", execute)
    runner.run_tasks(task_path, output_dir, "openai/model", None)
    execute.assert_called_once()
    assert json.loads(aggregate.read_text().splitlines()[-1])["instance_id"] == instance["instance_id"]


def test_failure_does_not_mark_task_completed(monkeypatch, tmp_path, instance):
    task_path = tmp_path / "tasks.json"
    task_path.write_text(json.dumps([instance]))
    monkeypatch.setattr(runner, "run_instance", Mock(side_effect=RuntimeError("execution failed")))
    output_dir = tmp_path / "outputs"
    with pytest.raises(RuntimeError, match="execution failed"):
        runner.run_tasks(task_path, output_dir, "openai/model", None)
    aggregate = output_dir / "all_preds.jsonl"
    assert not aggregate.exists() or not aggregate.read_text()


def test_g2_keeps_instance_outputs_in_requested_directory(monkeypatch, tmp_path, instance):
    task_path = tmp_path / "tasks.json"
    task_path.write_text(json.dumps([instance]))
    output_dir = tmp_path / "outputs"
    artifact_dir = output_dir / instance["instance_id"]

    def execute(*args, **kwargs):
        assert kwargs["g2"] is True
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "saved.traj").write_text("{}")
        return {"model_patch": "patch"}

    monkeypatch.setattr(runner, "run_instance", execute)
    runner.run_tasks(task_path, output_dir, "openai/model", None, g2=True)
    assert (artifact_dir / "saved.traj").exists()


def test_cli_forwards_positive_thinking_budget_and_agent_options(monkeypatch, tmp_path):
    execute = Mock()
    monkeypatch.setattr(runner, "run_tasks", execute)
    runner.main([
        "--input_tasks", "tasks.json", "--output_dir", str(tmp_path),
        "--model_name", "gemini/gemini-2.5-flash", "--thinking_budget", "1024",
        "--instance_ids", "one,two", "--use_apptainer", "false",
        "--agent.brain=multi_granularity", "--agent.hint=remind_only",
    ])
    kwargs = execute.call_args.kwargs
    assert kwargs["thinking_budget"] == 1024
    assert kwargs["instance_ids"] == ["one", "two"]
    assert kwargs["extra_args"] == ["--agent.brain=multi_granularity", "--agent.hint=remind_only"]
    assert kwargs["use_apptainer"] is False


@pytest.mark.parametrize("value,expected", [("true", True), ("YES", True), ("1", True), ("false", False), ("No", False), ("0", False)])
def test_cli_preserves_boolean_forms(monkeypatch, tmp_path, value, expected):
    execute = Mock()
    monkeypatch.setattr(runner, "run_tasks", execute)
    runner.main(["-i", "tasks.json", "-o", str(tmp_path), "--use_apptainer", value])
    assert execute.call_args.kwargs["use_apptainer"] is expected


@pytest.mark.parametrize("option,value", [("--use_apptainer", "invalid"), ("--g2", "invalid"), ("--thinking_budget", "-1"), ("--mode", "testgen")])
def test_cli_rejects_invalid_arguments(tmp_path, option, value):
    with pytest.raises(SystemExit) as exc:
        runner.main(["-i", "tasks.json", "-o", str(tmp_path), option, value])
    assert exc.value.code == 2


@pytest.mark.parametrize("previous", ['{"unfinished": "café', '{"instance_id": "old"}'])
def test_resume_append_preserves_unterminated_previous_record(monkeypatch, tmp_path, instance, previous):
    task_path = tmp_path / "tasks.json"
    task_path.write_text(json.dumps([instance]), encoding="utf-8")
    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    aggregate = output_dir / "all_preds.jsonl"
    aggregate.write_text(previous, encoding="utf-8")
    execute = Mock(return_value={"model_patch": "patch"})
    monkeypatch.setattr(runner, "run_instance", execute)
    runner.run_tasks(task_path, output_dir, "openai/model", None)
    contents = aggregate.read_text(encoding="utf-8")
    assert contents.startswith(previous + "\n")
    assert json.loads(contents.splitlines()[-1])["instance_id"] == instance["instance_id"]
    runner.run_tasks(task_path, output_dir, "openai/model", None)
    execute.assert_called_once()
