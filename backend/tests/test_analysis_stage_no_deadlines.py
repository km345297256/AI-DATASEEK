"""Analysis/review stages must not reintroduce elapsed-time failure wrappers."""
import ast
import inspect
import textwrap

import pytest

from app.application.services.dataset_request_resolver import DatasetRequestResolver
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.agents.base import BaseAgent
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.analysis_answer_coverage import review_coverage
from app.domain.services.analysis_answer_review import _correct_citations, _review_answer
from app.domain.services.analysis_final_answer_review import review_final_candidate
from app.domain.services.analysis_report_review import load_report_targets
from app.domain.services.code_mode import CodeModeInterpreter
from app.domain.services.model_runtime import _invoke_provider, _store
from app.domain.services.safety.reviewer import SafetyReviewAgent
from app.infrastructure.repositories.mongo_model_trace_repository import MongoModelTraceRepository


@pytest.mark.parametrize("operation", [
    DatasetRequestResolver._invoke_with_transport_retry,
    SafetyReviewAgent.review,
    BaseAgent.invoke_tool,
    BaseAgent._ask_without_tools,
    ExecutionAgent.review_delivery_answer,
    AgentTaskRunner._validate_staged_artifact_files,
    AgentTaskRunner._review_analysis_budget,
    _review_answer,
    _correct_citations,
    review_coverage,
    review_final_candidate,
    load_report_targets,
    CodeModeInterpreter.run,
    _invoke_provider,
    _store,
    MongoModelTraceRepository.put,
])
def test_no_stage_installs_a_time_based_failure_wrapper(operation):
    tree = ast.parse(textwrap.dedent(inspect.getsource(operation)))
    prohibited = {"wait_for", "timeout", "timeout_at", "model_request_timeout"}
    calls = [node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
             for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, (ast.Attribute, ast.Name))]
    assert not (set(calls) & prohibited)
