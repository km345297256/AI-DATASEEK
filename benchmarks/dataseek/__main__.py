"""Run from repository root: python -m benchmarks.dataseek --help."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from .http_client import DataSeekClient
from .protocol import load_json, validate_tasks
from .runner import build_plan, public_task, run_dataseek, write_json
from .scoring import score_run, aggregate_results


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def prepare(args):
    tasks = validate_tasks(load_json(args.gold))
    if args.task_ids:
        wanted = set(args.task_ids.split(","))
        selected = [task for task in tasks if task["id"] in wanted]
        if {task["id"] for task in selected} != wanted:
            raise ValueError("unknown task ids")
        tasks = selected
    root = Path(args.experiment_dir)
    root.mkdir(parents=True, exist_ok=False)
    root.chmod(0o700)
    private = root / "private"
    private.mkdir(mode=0o700)
    write_json(private / "tasks.json", tasks)
    write_json(root / "tasks.public.json", [public_task(task) for task in tasks])
    methods = list(dict.fromkeys(args.methods.split(",")))
    if not methods or set(methods) - {"dataseek_default", "generic_react"}:
        raise ValueError("Only implemented methods dataseek_default,generic_react are available")
    config = {"protocol_version": "pilot-objective-v1", "experiment_id": root.name,
              "task_count": len(tasks), "gold_digest": digest(tasks), "base_url": args.base_url,
              "backbone_version": args.backbone, "repeats": args.repeats,
              "wall_seconds": args.wall_seconds, "methods": methods,
              "strict_token_admission": False,
              "scope": "development_pilot_not_blind_test",
              "repo_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
    if args.snapshot:
        from .environment import public_runtime_snapshot
        snapshot = public_runtime_snapshot(repo_root=str(Path.cwd()))
        write_json(root / "environment.json", snapshot)
        config["environment_digest"] = digest(snapshot)
    write_json(root / "config.json", config)
    plan = [run for method in methods for run in build_plan(tasks, args.repeats, args.backbone, root.name, method)]
    import random
    random.Random(root.name).shuffle(plan)
    write_json(root / "plan.json", plan)
    print(json.dumps({"prepared_tasks": len(tasks), "planned_runs": len(plan), "gold_hidden": True}))


def _load(root):
    config = load_json(root / "config.json")
    tasks = validate_tasks(load_json(root / "private/tasks.json"))
    if digest(tasks) != config["gold_digest"]:
        raise ValueError("frozen tasks/gold changed; create a new experiment")
    return config, tasks, load_json(root / "plan.json")


def run(args):
    root = Path(args.experiment_dir)
    config, tasks, plan = _load(root)
    client = DataSeekClient(config["base_url"])
    from .environment import public_runtime_snapshot
    from .input_check import verify_service_inputs, verify_api_origin
    write_json(root / "api-origin.verified.json", verify_api_origin(config["base_url"]))
    snapshot = public_runtime_snapshot(repo_root=str(Path.cwd()))
    write_json(root / "environment.observed.json", snapshot)
    if not snapshot["all_key_files_match"]:
        raise ValueError("running backend code differs from the repository")
    settings = snapshot["settings"]
    if settings["model_name"] != config["backbone_version"]:
        raise ValueError("running model differs from the frozen backbone label")
    if (settings["tool_preset_id"] != "general" or settings["tool_selection_mode"] != "on_demand"
            or settings["code_mode_enabled"] or settings["domain_subagents_enabled"]):
        raise ValueError("running configuration is not the default experimental variant")
    if (root / "environment.json").exists():
        frozen = load_json(root / "environment.json")
        if frozen["settings"] != settings or frozen["backend_image_id"] != snapshot["backend_image_id"]:
            raise ValueError("environment changed since experiment freeze")
    write_json(root / "inputs.verified.json", verify_service_inputs(tasks))
    presets = client.api("GET", "/api/v1/plugins/presets")
    write_json(root / "presets.observed.json", presets)
    catalog = client.api("GET", "/api/v1/datasets")
    available = {d["dataset_id"] for d in catalog["datasets"]}
    if any(task["dataset_id"] not in available for task in tasks):
        raise ValueError("pilot datasets missing from service")
    task_map = {t["id"]: t for t in tasks}
    runs_root = root / "runs"
    runs_root.mkdir(exist_ok=True)
    completed = 0
    for planned in plan:
        record_path = runs_root / planned["run_id"] / "run.json"
        if record_path.exists():
            # Existing work is never silently replayed, including uncertain/incomplete records.
            continue
        if completed >= args.limit:
            break
        task = task_map[planned["task_id"]]
        harness_hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in Path(__file__).parent.glob("*.py")}
        write_json(root / "harness-provenance" / (planned["run_id"] + ".json"),
                   {"file_sha256": harness_hashes, "python_version": sys.version})
        print(json.dumps({"starting": task["id"], "method": planned["method"], "run_index": planned["run_index"]}), flush=True)
        if planned["method"] == "dataseek_default":
            record = run_dataseek(client, task, planned, runs_root, config["wall_seconds"])
        elif planned["method"] == "generic_react":
            from .baseline_runner import run_baseline
            record = run_baseline(task, planned, runs_root, wall_seconds=config["wall_seconds"])
        else:
            raise ValueError("unsupported planned method")
        scored = score_run(task, record, runs_root / planned["run_id"] / "artifacts")
        write_json(runs_root / planned["run_id"] / "score.json", scored)
        completed += 1
        print(json.dumps({"finished": task["id"], "method": planned["method"], "status": record["status"],
                          "independent_success": scored["independent_success"],
                          "elapsed_seconds": round(record["elapsed_seconds"], 1)}), flush=True)
        if record["metadata"].get("stop_confirmed") is False:
            print("Stopped batch: cancellation of the owned session is unconfirmed.", file=sys.stderr)
            break
        if record["metadata"].get("traced_backbone_matches_label") is False:
            print("Stopped batch: observed model differs from the frozen label.", file=sys.stderr)
            break
        if (record["metadata"].get("sandbox_removed") is False
                or record["metadata"].get("sandbox_cleanup_confirmed") is False):
            print("Stopped batch: baseline sandbox cleanup is unconfirmed.", file=sys.stderr)
            break
    report(args)


def report(args):
    root = Path(args.experiment_dir)
    config, tasks, plan = _load(root)
    task_map = {t["id"]: t for t in tasks}
    records, scored = [], []
    for planned in plan:
        path = root / "runs" / planned["run_id"]
        if not (path / "run.json").exists():
            continue
        record = load_json(path / "run.json")
        records.append(record)
        result = score_run(task_map[record["task_id"]], record, path / "artifacts")
        scored.append(result)
        write_json(path / "score.json", result)
    observed = aggregate_results(tasks, scored) if scored else {"groups": []}
    all_planned = aggregate_results(tasks, scored, expected_runs=plan)
    summary = {"scope": config["scope"], "planned_runs": len(plan), "started_runs": len(records),
               "not_started_runs": len(plan) - len(records), "observed_only": observed,
               "planned_denominator_incomplete_as_failure": all_planned,
               "limitations": ["Development tasks; not blind-test performance.",
                               "Implemented methods are DataSeek default and a minimal generic ReAct; same-tool B2 is not implemented.",
                               "DataSeek API: wall-clock cancellation only; no pre-request token/call admission.",
                               "Generic ReAct B1: estimated-input plus reserved-output token admission, physical-call cap and wall-clock deadline; resource controls differ from DataSeek API.",
                               "Trace usage excludes some bootstrap/helper calls; costs remain unknown until reconciled.",
                               "No independent code rerun or subjective visualization scoring in this pilot."]}
    write_json(root / "summary.json", summary)
    lines = ["# DataSeek 首轮预实验记录", "", "这是开发预实验，不能据此声称优于其他方法。", "",
             "- 计划运行：%s；已启动：%s；尚未启动：%s。" % (len(plan), len(records), len(plan) - len(records)),
             "- 基础模型：`%s`；每题重复：%s；每次墙钟上限：%s 秒。" % (config["backbone_version"], config["repeats"], config["wall_seconds"]),
             "- 标准答案与模型隔离，逐字段检查实际下载的 answer.json。", "",
             "| 任务 | 方法 | 重复 | 运行状态 | 独立答案与成果验收 | 秒 |", "| --- | --- | ---: | --- | --- | ---: |"]
    for result in scored:
        lines.append("| %s | %s | %s | %s | %s | %.1f |" % (result["task_id"], result["method"], result["run_index"], result["status"],
                      "通过" if result["independent_success"] else "未通过", result.get("elapsed_seconds") or 0))
    lines += ["", "## 解释边界", "",
              "尚未开始的运行在进度中单列；summary.json 同时保留已观测分母和完整计划分母。", "",
              "本批是 DataSeek 与最小通用 ReAct 的入口、答案和计量校准。同工具 ReAct 尚未接入；小样本、治理与资源控制差异均限制结论，不能生成正式论文的优越性结论。", "",
              "DataSeek API 模式只能按墙钟取消，不能在每次模型请求前保证 Token/调用上限。通用 ReAct B1 在每次物理模型调用前按估算输入与预留输出执行 Token 准入，并控制物理调用次数与墙钟；两者资源控制不等同。模型 trace 的数值是部分阶段用量，不能当完整费用；未知费用保留为空。", "",
              "现有数据说明可能含人工注释，且这些内置数据参与过开发；正式盲测需要重新划分独立来源并清理输入泄漏。", ""]
    (root / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"planned": len(plan), "started": len(records),
                      "passed": sum(s["independent_success"] for s in scored), "report": str(root / "report.md")}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="freeze private gold and a public task/run plan")
    p.add_argument("--gold", required=True)
    p.add_argument("--experiment-dir", required=True)
    p.add_argument("--base-url", required=True)
    p.add_argument("--backbone", required=True)
    p.add_argument("--repeats", type=int, default=2)
    p.add_argument("--methods", default="dataseek_default")
    p.add_argument("--wall-seconds", type=int, default=300)
    p.add_argument("--task-ids")
    p.add_argument("--snapshot", action="store_true")
    p.set_defaults(func=prepare)
    p = sub.add_parser("run", help="submit new paid-model analyses to the existing DataSeek service")
    p.add_argument("--experiment-dir", required=True)
    p.add_argument("--limit", type=int, default=1, help="maximum NEW runs; never retries existing run ids")
    p.set_defaults(func=run)
    p = sub.add_parser("report", help="independently rescore downloaded artifacts without model calls")
    p.add_argument("--experiment-dir", required=True)
    p.set_defaults(func=report)
    args = parser.parse_args()
    if getattr(args, "limit", 1) < 1:
        parser.error("--limit must be positive")
    if getattr(args, "wall_seconds", 1) <= 0:
        parser.error("--wall-seconds must be positive")
    args.func(args)


if __name__ == "__main__":
    main()
