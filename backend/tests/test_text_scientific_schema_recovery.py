"""Frozen-candidate wire errors get one same-evidence structural recovery."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services import analysis_answer_review as review
from app.domain.services import analysis_text_scientific_review as text_review
from test_analysis_answer_review import tool
from test_text_scientific_review_integration import checked, grounded, run, verdict


def broken(kind):
    value = checked()
    checks = value['answer_scientific_checks']
    row = checks[0]
    if kind == 'count': checks.pop()
    elif kind == 'fields': row['private_response'] = 'SECRET-RAW-FAILURE'
    elif kind == 'dimension': row['dimension'] = 'invented_dimension'
    elif kind == 'duplicate_dimension': checks[1]['dimension'] = row['dimension']
    elif kind == 'status': row['status'] = 'not_applicable'
    elif kind == 'evidence_scope': row['evidence_scope'] = 'unverified_history'
    elif kind == 'indices': row['paragraph_indices'] = []
    elif kind == 'bool_indices': row['paragraph_indices'] = [False]
    elif kind == 'duplicate_indices': row['paragraph_indices'] = [0, 0]
    elif kind == 'evidence_list': row['evidence'] = {}
    elif kind == 'evidence_count': row['evidence'] *= 3
    elif kind == 'evidence_item': row['evidence'][0]['private_reason'] = 'SECRET-RAW-FAILURE'
    elif kind == 'source_type': row['evidence'][0]['source_id'] = None
    elif kind == 'quote_type': row['evidence'][0]['quote'] = 42
    elif kind == 'quote_limit': row['evidence'][0]['quote'] = 'x' * 161
    elif kind == 'scope_fields': value['answer_scope_check']['reasoning'] = 'SECRET-RAW-FAILURE'
    elif kind == 'scope_status': value['answer_scope_check']['status'] = 'pass'
    elif kind == 'scope_indices': value['answer_scope_check']['paragraph_indices'] = []
    elif kind == 'scope_bool_indices': value['answer_scope_check']['paragraph_indices'] = [False]
    else: raise AssertionError(kind)
    return value


CASES = [('count','review_answer_science_count'),('fields','review_answer_science_fields'),
    ('dimension','review_answer_science_dimension'),('duplicate_dimension','review_answer_science_dimension'),
    ('status','review_answer_science_status'),('indices','review_answer_science_indices'),
    ('evidence_scope','review_answer_science_scope'),
    ('bool_indices','review_answer_science_indices'),('duplicate_indices','review_answer_science_indices'),
    ('evidence_list','review_answer_science_evidence_list'),('evidence_count','review_answer_science_evidence_list'),
    ('evidence_item','review_answer_science_evidence_item'),('source_type','review_answer_science_evidence_item'),
    ('quote_type','review_answer_science_evidence_item'),('quote_limit','review_answer_science_evidence_item'),
    ('scope_fields','review_answer_scope_fields'),('scope_status','review_answer_scope_status'),
    ('scope_indices','review_answer_scope_indices'),('scope_bool_indices','review_answer_scope_indices')]


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation,code', CASES)
async def test_nested_schema_error_gets_one_existing_frozen_recovery(mutation, code):
    invalid = broken(mutation)
    result, ask = await run(grounded(), verdict(invalid), verdict())
    assert ask.await_count == 3
    assert result.status == 'verified'
    audit = result.metadata['final_candidate_review']
    assert audit['error'] == code and audit['protocol_attempts'] == 2 and audit['schema_recovered'] is True
    assert result.metadata['answer_scientific_review']['status'] == 'verified'
    first, second = [call.args[0] for call in ask.await_args_list[1:]]
    assert first[-1] is second[-1]
    frozen = json.loads(first[-1].content)
    assert frozen['frozen_paragraphs'] == ['Observed mean is 3 mg.']
    assert frozen['paragraph_indices'] == [0]
    assert code in second[0].content
    assert len(second) == 2 and 'SECRET-RAW' not in '\n'.join(message.content for message in second)
    assert 'SECRET-RAW' not in json.dumps(result.metadata)


@pytest.mark.parametrize('mutation,code', CASES)
def test_shape_errors_are_pure_fixed_codes(mutation, code):
    value = broken(mutation); frozen = copy.deepcopy(value)
    science = text_review.answer_scientific_shape_error(value['answer_scientific_checks'], paragraph_count=1)
    scope = text_review.answer_scope_shape_error(value['answer_scope_check'], paragraph_count=1)
    assert (science or scope) == code
    assert value == frozen


@pytest.mark.asyncio
async def test_two_malformed_responses_stop_without_third_request_or_raw_response_persistence():
    result, ask = await run(grounded(), verdict(broken('indices')), verdict(broken('indices')), verdict())
    assert ask.await_count == 3 and result.status == 'unavailable'
    audit = result.metadata['final_candidate_review']
    assert audit['status'] == 'unavailable' and audit['protocol_attempts'] == 2
    assert audit['error'] == 'review_answer_science_indices' and 'schema_recovered' not in audit
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('science,scope', [('rejected','complete'),('unclear','complete'),('verified','incomplete'),('verified','unclear')])
async def test_legitimate_negative_or_unknown_judgment_never_authorizes_protocol_retry(science, scope):
    result, ask = await run(grounded(), verdict(checked(science=science, scope=scope)))
    assert ask.await_count == 2
    assert result.status == 'unavailable' and 'review_schema_repair_attempted' not in result.metadata
    assert result.metadata['final_candidate_review']['protocol_attempts'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', [[], [{'source_id':'unknown','quote':'mean=3'}],
    [{'source_id':'tool_0001_result','quote':'not in evidence'}],
    [{'source_id':'','quote':''}], [{'source_id':'tool_0001_result','quote':' '}],
    [{'source_id':'current_request','quote':'Describe the observed data'}]])
async def test_unmatched_or_insufficient_citation_content_is_not_a_schema_retry(evidence):
    value = checked(); value['answer_scientific_checks'][0]['evidence'] = evidence
    result, ask = await run(grounded(), verdict(value))
    assert ask.await_count == 2
    assert result.status == 'unavailable' and 'review_schema_repair_attempted' not in result.metadata
    assert result.metadata['final_candidate_review']['protocol_attempts'] == 1


@pytest.mark.asyncio
async def test_recovery_scope_unclear_remains_unknown_not_green():
    result, ask = await run(grounded(), verdict(broken('indices')), verdict(checked(scope='unclear')))
    assert ask.await_count == 3 and result.status == 'unavailable'
    assert result.metadata['answer_scope_review']['status'] == 'unavailable'
    assert result.metadata['final_candidate_review']['schema_recovered'] is True
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
async def test_recovery_can_still_return_honest_incomplete_scope_without_more_requests():
    result, ask = await run(grounded(), verdict(broken('scope_indices')), verdict(checked(scope='incomplete')))
    assert ask.await_count == 3 and result.status == 'unavailable'
    assert result.metadata['answer_scope_review']['status'] == 'incomplete'


@pytest.mark.asyncio
async def test_cancelled_recovery_propagates_without_synthesizing_a_verdict():
    evidence = review.AnswerEvidence(); evidence.begin_step('current'); evidence.observe(tool())
    ask = AsyncMock(side_effect=[json.dumps(grounded()), json.dumps(verdict(broken('indices'))), asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await review.review_answer(ask=ask, question='Describe the observed data', draft='Observed mean is 3 mg.',
            files=[], evidence=evidence, language='en', answer_scientific_scope=True)
    assert ask.await_count == 3


def test_scope_incomplete_accepts_unique_subset_but_complete_requires_all_paragraphs():
    assert text_review.answer_scope_shape_error({'status':'incomplete','paragraph_indices':[]}, paragraph_count=10) is None
    assert text_review.answer_scope_shape_error({'status':'unclear','paragraph_indices':[8,0]}, paragraph_count=10) is None
    assert text_review.answer_scope_shape_error({'status':'complete','paragraph_indices':list(range(9))}, paragraph_count=10) == 'review_answer_scope_indices'
    assert text_review.answer_scope_shape_error({'status':'complete','paragraph_indices':list(range(10))}, paragraph_count=10) is None
