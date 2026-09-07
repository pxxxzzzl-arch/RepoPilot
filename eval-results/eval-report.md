# RepoPilot Agent Eval Report

Generated: `2026-09-07T02:09:54.153597+00:00`  
Model: `deepseek-v4-flash`  
Tasks: **10** · Runs per task: **3** · Total runs: **30**

## Overall

| Metric | Value |
|---|---:|
| Repair success rate | 90.0% |
| Test pass rate | 90.0% |
| Unrelated file changes | 0 |
| Average tool calls | 4.97 |
| Total tokens | 196228 |
| Average duration | 10.525s |
| Estimated cost | $0.064892 |
| Timeouts | 0 |
| Patch conflicts | 1 |
| Security blocks | 2 |

## Per task

| Task | Success | Tests | Unrelated | Avg tools | Avg tokens | Avg seconds | Cost | Timeouts | Conflicts | Security |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| divide | 100.0% | 100.0% | 0 | 5.00 | 6300.0 | 11.152 | $0.005789 | 0 | 0 | 0 |
| add | 100.0% | 100.0% | 0 | 6.33 | 8652.3 | 17.147 | $0.009370 | 0 | 0 | 0 |
| is_even | 100.0% | 100.0% | 0 | 5.00 | 6305.3 | 9.891 | $0.006076 | 0 | 0 | 0 |
| clamp | 100.0% | 100.0% | 0 | 5.00 | 6579.0 | 10.368 | $0.006451 | 0 | 0 | 0 |
| normalize | 66.7% | 66.7% | 0 | 4.67 | 5518.3 | 8.073 | $0.005266 | 0 | 1 | 0 |
| mean | 66.7% | 66.7% | 0 | 5.33 | 6908.3 | 9.912 | $0.006466 | 0 | 0 | 1 |
| fahrenheit | 100.0% | 100.0% | 0 | 5.00 | 6434.0 | 8.454 | $0.005848 | 0 | 0 | 0 |
| contains_ci | 100.0% | 100.0% | 0 | 5.00 | 6797.7 | 10.133 | $0.006747 | 0 | 0 | 0 |
| safe_index | 100.0% | 100.0% | 0 | 4.00 | 5786.0 | 9.919 | $0.006274 | 0 | 0 | 0 |
| unique_order | 66.7% | 66.7% | 0 | 4.33 | 6128.3 | 10.197 | $0.006604 | 0 | 0 | 1 |

## Termination reasons

- `invalid_action`: 2
- `patch_conflict`: 1
- `success`: 27

## Representative failures

| Task | Run | Status | Error summary |
|---|---:|---|---|
| normalize | 1 | `patch_conflict` | old_content does not match text.py |
| mean | 3 | `invalid_action` | invalid model output: model output is not valid JSON |
| unique_order | 1 | `invalid_action` | invalid model output: model output is not valid JSON |
