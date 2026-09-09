"""Offline comparison against a source revision supplied on stdin.

Example (an environment with backend dependencies is required):
    git show 3a16b01:backend/app/domain/services/context_budget.py | \
      python backend/scripts/benchmark_context_preparation.py

No model/provider, user data, secrets or database are used. Timings measure
only context preparation, not total analysis latency or billing tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import statistics
import sys
import time
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from langchain.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from app.domain.services import context_budget as current


def private_digest(value):
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hmac.new(b"offline-benchmark-not-a-secret", payload, hashlib.sha256).hexdigest()


def signature(prepared):
    return {
        "messages": [message.model_dump(mode="json") for message in prepared.messages],
        "before": prepared.input_tokens_before, "after": prepared.input_tokens_after,
        "tools": prepared.tool_tokens, "limit": prepared.input_limit,
        "records": [record.model_dump(mode="json") for record in prepared.records],
    }


def main():
    baseline = types.ModuleType("dataseek_budget_baseline")
    sys.modules[baseline.__name__] = baseline
    exec(compile(sys.stdin.read(), "<trusted-repository-baseline>", "exec"), baseline.__dict__)
    baseline.private_identity_hmac = current.private_identity_hmac = private_digest
    report = []
    for count in (10, 100, 500):
        messages = [SystemMessage(content="Preserve policy"), HumanMessage(content="Analyze this fixture")]
        for index in range(count):
            call_id = f"call-{index}"
            messages.extend([
                AIMessage(content="", tool_calls=[{"id": call_id, "name": "read", "args": {"column": index}}]),
                ToolMessage(content="数值," * 1500, tool_call_id=call_id, name="read"),
            ])
        kwargs = dict(tool_schemas=[{"name": "read", "description": "schema" * 400}],
                      response_format={"type": "json_object"}, capacity_tokens=3000 + count * 170,
                      max_output_tokens=300, safety_tokens=100, max_tool_text_tokens=250)
        expected = signature(baseline.prepare_context(messages, **kwargs))
        assert signature(current.prepare_context(messages, **kwargs)) == expected
        samples = {"baseline": [], "current": []}
        for _ in range(3):
            for name, module in (("baseline", baseline), ("current", current)):
                started = time.perf_counter()
                actual = module.prepare_context(messages, **kwargs)
                samples[name].append((time.perf_counter() - started) * 1000)
                assert signature(actual) == expected
        report.append({"tool_exchanges": count, "messages": len(messages), "identical_output": True,
                       **{f"{name}_median_ms": round(statistics.median(values), 3) for name, values in samples.items()}})
    print(json.dumps({"scope": "offline-context-preparation-only", "rounds": 3, "results": report}, indent=2))


if __name__ == "__main__":
    main()
