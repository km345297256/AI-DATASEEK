"""The scope repair decision must never create a general replay permission."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from app.domain.services.analysis_scope_repair import (
    ScopeRepairGuards, ScopeRepairTracker, ScopeReviewBinding,
)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def fixture():
    request = 'Compare the supplied observations and test sensitivity; preserve unknown uncertainty.'
    paragraphs = ('The requested sensitivity comparison has not been calculated.',)
    scope = dict(version=1, review_kind='answer_scope', enabled=True, status='incomplete',
                 reason='answer_scope_incomplete', candidate_version_status='original',
                 completion_blocker='none',
                 request_sha256=digest(request), paragraph_count=1, checked_paragraph_count=1,
                 candidate_sha256=digest(json.dumps(list(paragraphs), ensure_ascii=False, separators=(',', ':'))),
                 candidate_text_sha256=digest('\n\n'.join(paragraphs)))
    binding = ScopeReviewBinding(input_seq=7, step_id='analysis', request=request,
                                 paragraphs=paragraphs, metadata=scope)
    execution = dict(code='completed', has_unconfirmed_tool_execution=False, side_effect_state='confirmed',
        execution_evidence=dict(execution_confirmed=True, pending_execution=False, unresolved_call_count=0,
            tracked_operation_count=0, nonreplayable_call_count=0, has_observable_pending=False,
            has_unresolvable_pending=False))
    guards = ScopeRepairGuards(analysis_authorized=True, current_input=True, original_request_complete=True,
        candidate_complete=True, read_only_observations=True, prerequisites_met=True,
        no_user_input_required=True, no_policy_refusal=True, runtime_live=True,
        no_artifacts_or_requirements=True, no_review_repair=True)
    tracker = ScopeRepairTracker(input_seq=7, step_id='analysis', request=request)
    return tracker, binding, execution, guards


def decide(tracker, binding, execution, guards, **kwargs):
    return tracker.review(binding=binding, execution=execution, guards=guards,
        now=datetime(2026, 9, 20, tzinfo=timezone.utc), deadline_at=None, **kwargs)


def test_one_current_incomplete_read_only_attempt_without_forcing_program_or_file():
    tracker, binding, execution, guards = fixture()
    result = decide(tracker,binding,execution,guards)
    assert result.allowed and result.reason == 'scope_completion_allowed'
    assert result.feedback['schema'] == 'answer_scope_completion/v1'
    assert 'program_run' not in str(result.feedback)
    assert 'output_paths' not in str(result.feedback)
    assert binding.request not in str(result.feedback)
    assert not decide(tracker,binding,execution,guards).allowed


def test_candidate_and_new_read_do_not_restore_consumed_attempt():
    tracker, binding, execution, guards = fixture()
    assert decide(tracker,binding,execution,guards).allowed
    _, other, _, _ = fixture()
    assert not decide(tracker,other,execution,guards).allowed
    assert tracker.snapshot()['consumed'] is True


@pytest.mark.parametrize('field', list(ScopeRepairGuards.__dataclass_fields__))
@pytest.mark.parametrize('value', [False,None,1,'true'])
def test_each_guard_requires_affirmative_host_boolean(field,value):
    tracker,binding,execution,guards=fixture()
    values=guards.__dict__ | {field:value}
    assert not decide(tracker,binding,execution,ScopeRepairGuards(**values)).allowed


@pytest.mark.parametrize('field,value', [
    ('status','unavailable'),('status','verified'),('reason','answer_scope_check_invalid'),
    ('candidate_version_status','changed'),('candidate_sha256','a'*64),
    ('candidate_text_sha256','a'*64),('request_sha256','a'*64),('enabled',1),
    ('version',True),('paragraph_count',2),('checked_paragraph_count',2),
    ('checked_paragraph_count',True),('review_kind','answer_scientific'),
    ('completion_blocker','unknown'),('completion_blocker','missing_input'),
    ('completion_blocker','user_input_required'),('completion_blocker','permission'),
    ('completion_blocker','unsupported'),('completion_blocker',None),
])
def test_invalid_or_stale_scope_never_authorizes_repair(field,value):
    tracker,binding,execution,guards=fixture()
    scope=dict(binding.metadata);scope[field]=value
    altered=ScopeReviewBinding(input_seq=binding.input_seq,step_id=binding.step_id,
                              request=binding.request,paragraphs=binding.paragraphs,metadata=scope)
    assert not decide(tracker,altered,execution,guards).allowed


@pytest.mark.parametrize('changes', [dict(input_seq=8),dict(step_id='another'),
    dict(request='A different request'),dict(paragraphs=('A different candidate',)),dict(paragraphs=())])
def test_review_is_bound_to_same_original_input_step_request_and_candidate(changes):
    tracker,binding,execution,guards=fixture()
    altered=ScopeReviewBinding(**(binding.__dict__ | changes))
    assert not decide(tracker,altered,execution,guards).allowed


@pytest.mark.parametrize('changes', [dict(code='running'),dict(code='cancelled'),dict(code='execution_failed'),
    dict(has_unconfirmed_tool_execution=True),dict(side_effect_state='unknown')])
def test_uncertain_or_noncompleted_execution_is_blocked(changes):
    tracker,binding,execution,guards=fixture()
    assert not decide(tracker,binding,execution|changes,guards).allowed


@pytest.mark.parametrize('changes', [dict(execution_confirmed=False),dict(pending_execution=True),
    dict(unresolved_call_count=1),dict(tracked_operation_count=1),dict(nonreplayable_call_count=1),
    dict(has_observable_pending=True),dict(has_unresolvable_pending=True)])
def test_current_processes_and_prior_side_effects_are_not_replayed(changes):
    tracker,binding,execution,guards=fixture()
    execution['execution_evidence'].update(changes)
    assert not decide(tracker,binding,execution,guards).allowed


def test_deadline_is_original_absolute_deadline_not_new_allowance():
    tracker,binding,execution,guards=fixture()
    now=datetime(2026,9,20,tzinfo=timezone.utc)
    assert not tracker.review(binding=binding,execution=execution,guards=guards,now=now,deadline_at=now).allowed
    assert tracker.review(binding=binding,execution=execution,guards=guards,now=now,deadline_at=now+timedelta(seconds=1)).allowed


def test_resume_is_conservative_even_unconsumed_legacy_checkpoint():
    tracker,binding,execution,guards=fixture()
    restored=ScopeRepairTracker(input_seq=7,step_id='analysis',request=binding.request,restored=True)
    assert not decide(restored,binding,execution,guards).allowed
    assert restored.snapshot()['restored'] is True


def test_no_mutation_of_request_candidate_metadata_or_execution_and_no_raw_data_in_snapshot():
    tracker,binding,execution,guards=fixture()
    original=(deepcopy(binding.metadata),deepcopy(execution))
    decide(tracker,binding,execution,guards)
    assert original==(binding.metadata,execution)
    state=str(tracker.snapshot())
    assert binding.request not in state and binding.paragraphs[0] not in state
