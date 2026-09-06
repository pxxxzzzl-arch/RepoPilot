"""Command-line entry point for Issue2Patch."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from issue2patch import __version__
from issue2patch.evals import (
    DEFAULT_EVAL_RUNS,
    EvalRunner,
    EvalTask,
    EvalValidationError,
    load_eval_suite,
    write_eval_reports,
)
from issue2patch.models import (
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_OPENAI_MODEL,
    DeepSeekResponsesModelClient,
    ModelClientError,
    OpenAIResponsesModelClient,
)
from issue2patch.orchestrator import (
    AgentConfig,
    AgentOrchestrator,
    AgentProgressEvent,
    ModelClient,
    TestRunner,
)
from issue2patch.sandbox import DockerSandboxRunner
from issue2patch.trace import TraceRecorder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue2patch",
        description=(
            "RepoPilot: turn an issue into an auditable patch in a temporary copy."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    subparsers = parser.add_subparsers(dest="command")
    run = subparsers.add_parser(
        "run", help="run the autonomous repair agent against a temporary copy"
    )
    run.add_argument("--repo", required=True, type=Path, help="target repository root")
    run.add_argument("--issue", required=True, help="issue description")
    run.add_argument(
        "--provider",
        choices=("openai", "deepseek"),
        default="openai",
        help="model API provider (default: openai)",
    )
    run.add_argument(
        "--model",
        help="provider model ID (default depends on --provider)",
    )
    run.add_argument("--api-timeout", type=float, default=30.0)
    run.add_argument("--api-retries", type=int, default=2)
    run.add_argument("--test-timeout", type=float, default=60.0)
    run.add_argument("--max-steps", type=int, default=12)
    run.add_argument("--max-tool-calls", type=int, default=10)
    run.add_argument("--test-path", default="tests")
    run.add_argument(
        "--allow-regex-search",
        action="store_true",
        help="allow model-requested regular-expression searches",
    )
    run.add_argument(
        "--trace",
        type=Path,
        help="optional JSONL audit path; it must be outside the target repository",
    )
    run.add_argument(
        "--approve",
        action="store_true",
        help="explicitly approve API charges and execution without an interactive prompt",
    )
    evaluate = subparsers.add_parser(
        "eval", help="run a fixed repair suite repeatedly and write JSON/Markdown reports"
    )
    evaluate.add_argument(
        "--suite", type=Path, default=Path("evals/suite.json")
    )
    evaluate.add_argument("--runs", type=int, default=DEFAULT_EVAL_RUNS)
    evaluate.add_argument(
        "--output", type=Path, default=Path("eval-results")
    )
    evaluate.add_argument(
        "--provider",
        choices=("openai", "deepseek"),
        default="openai",
        help="model API provider (default: openai)",
    )
    evaluate.add_argument(
        "--model",
        help="provider model ID (default depends on --provider)",
    )
    evaluate.add_argument("--api-timeout", type=float, default=30.0)
    evaluate.add_argument("--api-retries", type=int, default=2)
    evaluate.add_argument("--test-timeout", type=float, default=60.0)
    evaluate.add_argument("--max-steps", type=int, default=12)
    evaluate.add_argument("--max-tool-calls", type=int, default=10)
    evaluate.add_argument("--allow-regex-search", action="store_true")
    evaluate.add_argument(
        "--approve",
        action="store_true",
        help="explicitly approve repeated API charges without an interactive prompt",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    model_factory: Callable[[argparse.Namespace], ModelClient] | None = None,
    eval_model_factory: Callable[[EvalTask, int, argparse.Namespace], ModelClient]
    | None = None,
    test_runner: TestRunner | None = None,
) -> int:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in {"run", "eval"}:
        parser.print_help(error_stream)
        return 2
    args.model = _resolve_model(args.provider, args.model)

    if args.command == "eval":
        try:
            tasks = load_eval_suite(args.suite)
        except EvalValidationError as error:
            print(f"Suite error: {error}", file=error_stream)
            return 2
        if not _obtain_approval(
            args, input_stream, error_stream, eval_task_count=len(tasks)
        ):
            print("Approval denied; no model request was sent.", file=error_stream)
            return 2
        return _run_eval(
            args,
            tasks,
            error_stream,
            eval_model_factory=eval_model_factory,
            test_runner=test_runner,
        )

    if not _obtain_approval(args, input_stream, error_stream):
        print("Approval denied; no model request was sent.", file=error_stream)
        return 2

    try:
        model = (
            model_factory(args)
            if model_factory
            else _create_model(args)
        )
        config = AgentConfig(
            max_steps=args.max_steps,
            max_tool_calls=args.max_tool_calls,
            test_timeout=args.test_timeout,
            baseline_test_path=args.test_path,
            allow_regex_search=args.allow_regex_search,
        )
    except (ModelClientError, ValueError) as error:
        print(f"Configuration error: {error}", file=error_stream)
        return 2

    recorder = TraceRecorder(args.trace) if args.trace else None
    progress = _progress_printer(error_stream)
    result = AgentOrchestrator(
        model,
        config=config,
        test_runner=test_runner,
        progress_callback=progress,
    ).run(args.repo, issue=args.issue, trace_recorder=recorder)

    usage = result.model_usage
    cost = (
        "unavailable"
        if usage.estimated_cost_usd is None
        else f"${usage.estimated_cost_usd:.6f}"
    )
    print(
        f"Status: {result.status.value}; steps={result.steps}; "
        f"tool_calls={result.tool_calls}; elapsed={result.duration_seconds:.3f}s",
        file=error_stream,
    )
    print(
        f"Model: requests={usage.request_count}; retries={usage.retry_count}; "
        f"input_tokens={usage.input_tokens}; output_tokens={usage.output_tokens}; "
        f"total_tokens={usage.total_tokens}; model_time={usage.duration_seconds:.3f}s; "
        f"estimated_cost={cost}",
        file=error_stream,
    )
    if result.error:
        print(f"Error: {result.error}", file=error_stream)
    if result.diff:
        output_stream.write(result.diff)
        if not result.diff.endswith("\n"):
            output_stream.write("\n")
    output_stream.flush()
    return 0 if result.success else 1


def _run_eval(
    args: argparse.Namespace,
    tasks: tuple[EvalTask, ...],
    stderr: TextIO,
    *,
    eval_model_factory: Callable[[EvalTask, int, argparse.Namespace], ModelClient]
    | None,
    test_runner: TestRunner | None,
) -> int:
    try:
        output = args.output.resolve(strict=False)
        if any(
            output == task.repository or output.is_relative_to(task.repository)
            for task in tasks
        ):
            raise EvalValidationError(
                "report output must be outside all task repositories"
            )
        config = AgentConfig(
            max_steps=args.max_steps,
            max_tool_calls=args.max_tool_calls,
            test_timeout=args.test_timeout,
            allow_regex_search=args.allow_regex_search,
        )

        def create_model(task: EvalTask, run_number: int) -> ModelClient:
            if eval_model_factory:
                return eval_model_factory(task, run_number, args)
            return _create_model(args)

        def create_test_runner() -> TestRunner:
            return test_runner if test_runner is not None else DockerSandboxRunner()

        runner = EvalRunner(
            create_model,
            agent_config=config,
            test_runner_factory=create_test_runner,
            progress_callback=_eval_progress_printer(stderr),
        )
        report = runner.run(
            tasks,
            runs_per_task=args.runs,
            suite_path=args.suite,
            model_name=args.model,
        )
        json_path, markdown_path = write_eval_reports(report, args.output)
    except (EvalValidationError, ModelClientError, OSError, ValueError) as error:
        print(f"Evaluation error: {error}", file=stderr)
        return 2

    aggregate = report.aggregate
    cost = (
        "unavailable"
        if aggregate.total_cost_usd is None
        else f"${aggregate.total_cost_usd:.6f}"
    )
    print(
        f"Eval complete: repair_success={aggregate.repair_success_rate * 100:.1f}%; "
        f"tests={aggregate.test_pass_rate * 100:.1f}%; "
        f"runs={aggregate.total_runs}; tokens={aggregate.total_tokens}; cost={cost}",
        file=stderr,
    )
    print(f"JSON report: {json_path}", file=stderr)
    print(f"Markdown report: {markdown_path}", file=stderr)
    return 0


def _obtain_approval(
    args: argparse.Namespace,
    stdin: TextIO,
    stderr: TextIO,
    *,
    eval_task_count: int | None = None,
) -> bool:
    print("Human approval required before autonomous execution:", file=stderr)
    if args.command == "eval":
        total_runs = (eval_task_count or 0) * args.runs
        maximum_steps = total_runs * args.max_steps
        print(f"  suite: {args.suite}", file=stderr)
        print(
            f"  tasks: {eval_task_count}; runs per task: {args.runs}; "
            f"total runs: {total_runs}",
            file=stderr,
        )
        print(f"  maximum model steps: {maximum_steps}", file=stderr)
        print(f"  reports: {args.output}", file=stderr)
    else:
        print(f"  repository: {args.repo}", file=stderr)
    print(f"  provider: {args.provider}", file=stderr)
    print(f"  model: {args.model}", file=stderr)
    print("  tests run in the configured sandbox; model API usage may incur charges", file=stderr)
    print("  only temporary copies may be modified; source repositories stay unchanged", file=stderr)
    if args.approve:
        print("Approval: granted by --approve", file=stderr)
        return True
    if not stdin.isatty():
        print("Non-interactive input requires the explicit --approve flag.", file=stderr)
        return False
    stderr.write("Approve this run? [y/N] ")
    stderr.flush()
    answer = stdin.readline().strip().lower()
    return answer in {"y", "yes"}


def _resolve_model(provider: str, model: str | None) -> str:
    if model:
        return model
    if provider == "deepseek":
        return DEFAULT_DEEPSEEK_MODEL
    return DEFAULT_OPENAI_MODEL


def _create_model(args: argparse.Namespace) -> ModelClient:
    client_type = (
        DeepSeekResponsesModelClient
        if args.provider == "deepseek"
        else OpenAIResponsesModelClient
    )
    return client_type(
        model=args.model,
        timeout=args.api_timeout,
        max_retries=args.api_retries,
    )


def _progress_printer(stream: TextIO) -> Callable[[AgentProgressEvent], None]:
    def report(event: AgentProgressEvent) -> None:
        if event.event_type == "baseline":
            marker = "PASS" if event.success else "FAIL"
            print(f"[baseline] {marker} {event.summary}", file=stream, flush=True)
        elif event.event_type == "model":
            print(f"[step {event.step}] requesting model action", file=stream, flush=True)
        elif event.event_type == "action":
            print(f"[step {event.step}] action {event.action_type}", file=stream, flush=True)
        elif event.event_type == "tool":
            marker = "OK" if event.success else "ERROR"
            print(
                f"[step {event.step}] {event.action_type} {marker}: {event.summary}",
                file=stream,
                flush=True,
            )

    return report


def _eval_progress_printer(
    stream: TextIO,
) -> Callable[[EvalTask, int, AgentProgressEvent], None]:
    def report(task: EvalTask, run_number: int, event: AgentProgressEvent) -> None:
        prefix = f"[{task.task_id} run {run_number}]"
        if event.event_type == "baseline":
            marker = "PASS" if event.success else "FAIL"
            print(f"{prefix} baseline {marker}: {event.summary}", file=stream, flush=True)
        elif event.event_type == "model":
            print(f"{prefix} step {event.step}: requesting model", file=stream, flush=True)
        elif event.event_type == "action":
            print(
                f"{prefix} step {event.step}: {event.action_type}",
                file=stream,
                flush=True,
            )
        elif event.event_type == "tool":
            marker = "OK" if event.success else "ERROR"
            print(
                f"{prefix} step {event.step}: {event.action_type} {marker}",
                file=stream,
                flush=True,
            )

    return report


if __name__ == "__main__":
    raise SystemExit(main())
