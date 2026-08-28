"""Command-line entry point for Issue2Patch."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from issue2patch import __version__
from issue2patch.models import (
    DEFAULT_OPENAI_MODEL,
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
from issue2patch.trace import TraceRecorder


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="issue2patch",
        description="Turn an issue into an auditable patch in a temporary copy.",
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
    run.add_argument("--model", default=DEFAULT_OPENAI_MODEL)
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
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    model_factory: Callable[[argparse.Namespace], ModelClient] | None = None,
    test_runner: TestRunner | None = None,
) -> int:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "run":
        parser.print_help(error_stream)
        return 2

    if not _obtain_approval(args, input_stream, error_stream):
        print("Approval denied; no model request was sent.", file=error_stream)
        return 2

    try:
        model = (
            model_factory(args)
            if model_factory
            else OpenAIResponsesModelClient(
                model=args.model,
                timeout=args.api_timeout,
                max_retries=args.api_retries,
            )
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


def _obtain_approval(args: argparse.Namespace, stdin: TextIO, stderr: TextIO) -> bool:
    print("Human approval required before autonomous execution:", file=stderr)
    print(f"  repository: {args.repo}", file=stderr)
    print(f"  model: {args.model}", file=stderr)
    print("  tests run in the configured sandbox; model API usage may incur charges", file=stderr)
    print("  only a temporary copy may be modified; the original repository stays unchanged", file=stderr)
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


if __name__ == "__main__":
    raise SystemExit(main())
