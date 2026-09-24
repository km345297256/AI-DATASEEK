"""Answer construction and fixed-candidate science are separate read-only phases."""
import copy
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services import analysis_answer_review as review
from app.domain.services.analysis_scientific_review import SCIENTIFIC_DIMENSIONS
from test_analysis_answer_review import tool, paragraph


def checked(*, science='verified', scope='complete', text='Observed mean is 3 mg.', kind='analysis'):
    citation = [{'source_id': 'tool_0001_result', 'quote': 'mean=3; unit=mg'}]
    return {'unsupported_claims': False, 'paragraphs': [paragraph(text, kind=kind)],
            'requirement_checks': [],
            'answer_scientific_checks': [
                {'dimension': dimension, 'status': science, 'paragraph_indices': [0],
                 'evidence': citation if science == 'verified' else []}
                for dimension in SCIENTIFIC_DIMENSIONS],
            'answer_scope_check': {'status': scope, 'paragraph_indices': [0]}}


async def run(*answers, scope=True, draft='Observed mean is 3 mg.', question='Describe the observed data'):
    evidence = review.AnswerEvidence()
    evidence.begin_step('current')
    evidence.observe(tool())
    before = copy.deepcopy(evidence.__dict__)
    ask = AsyncMock(side_effect=[json.dumps(answer) for answer in answers])
    result = await review.review_answer(ask=ask, question=question, draft=draft,
        evidence=evidence, files=[], language='en', answer_scientific_scope=scope)
    assert evidence.__dict__ == before
    return result, ask


def grounded(value=None):
    value = copy.deepcopy(checked() if value is None else value)
    return {key: item for key, item in value.items()
            if key not in {'answer_scientific_checks', 'answer_scope_check', 'answer_complete'}}


def verdict(value=None):
    value = copy.deepcopy(checked() if value is None else value)
    return {key: item for key, item in value.items()
            if key in {'answer_scientific_checks', 'answer_scope_check'}}


@pytest.mark.asyncio
async def test_no_artifacts_can_pass_descriptive_science_in_separate_readonly_phases():
    result, ask = await run(grounded(), verdict())
    assert result.status == 'verified'
    assert result.metadata['answer_scientific_review']['status'] == 'verified'
    assert result.metadata['answer_scope_review']['status'] == 'verified'
    assert result.metadata['file_count'] == 0 and 'report_review' not in result.metadata
    assert ask.await_count == 2
    payload = json.loads(ask.await_args_list[0].args[0][-1].content)
    assert payload['publication_review']['phase'] == 'grounded_answer'
    assert 'report_targets' not in payload
    final = json.loads(ask.await_args_list[1].args[0][-1].content)
    assert final['candidate_paragraphs'] == [{'index': 0, 'text': result.text}]
    assert final['paragraph_indices'] == [0]
    assert final['sources'] == payload['sources']
    assert 'Reports remain' not in result.text and 'Artifacts remain' not in result.text


@pytest.mark.asyncio
@pytest.mark.parametrize('science', ['rejected', 'unclear'])
async def test_valid_citation_is_not_scientific_pass(science):
    result, ask = await run(grounded(), verdict(checked(science=science)))
    assert ask.await_count == 2
    assert result.status == 'unavailable' and result.missing_requirement_indices == ()
    assert result.metadata['reason'] == ('scientific_validation_rejected' if science == 'rejected' else 'scientific_validation_unavailable')
    assert 'Reports remain' not in result.text and 'Artifacts remain' not in result.text


@pytest.mark.asyncio
async def test_admitted_uninspected_tables_do_not_complete_the_full_request():
    value = checked(scope='incomplete', text='Only table A was inspected; other requested tables remain uninspected.')
    result, ask = await run(grounded(value), verdict(value),
        question='Read table A and inspect structural differences in all the remaining tables.')
    assert ask.await_count == 2
    assert result.status == 'unavailable' and result.metadata['reason'] == 'answer_coverage_incomplete'
    assert result.metadata['answer_scope_review']['status'] == 'incomplete'


@pytest.mark.asyncio
async def test_answer_construction_recovery_does_not_add_a_new_required_field():
    missing = grounded()
    missing.pop('requirement_checks')
    result, ask = await run(missing, grounded(), verdict())
    assert result.status == 'corrected' and ask.await_count == 3
    a, b = [call.args[0] for call in ask.await_args_list[:2]]
    assert a[-1] is b[-1]
    assert 'three fields: unsupported_claims, paragraphs, requirement_checks' in b[0].content
    assert 'schema is unchanged' in b[0].content
    assert 'report_checks' not in b[0].content


@pytest.mark.asyncio
async def test_citation_rewrite_is_reviewed_only_after_final_text_is_frozen():
    correction = {'answer_complete': True, 'paragraph_corrections': [
        {'index': 0, 'paragraph': {'text': 'The observed mean is 3 mg.', 'kind': 'analysis',
                                 'evidence': ['tool_0001_result:excerpt_0001']}}],
        'requirement_corrections': []}
    initial = grounded()
    initial['paragraphs'][0]['evidence'][0]['quote'] = 'An absent quote'
    result, ask = await run(initial, correction, verdict())
    assert ask.await_count == 3
    assert result.status == 'corrected'
    final = json.loads(ask.await_args.args[0][-1].content)
    assert final['frozen_paragraphs'] == ['The observed mean is 3 mg.']
    assert result.metadata['answer_scientific_review']['status'] == 'verified'


@pytest.mark.asyncio
async def test_citation_kind_correction_preserving_exact_text_keeps_candidate_binding():
    correction = {'answer_complete': True, 'paragraph_corrections': [
        {'index': 0, 'paragraph': {'text': 'Observed mean is 3 mg.', 'kind': 'analysis',
                                 'evidence': ['tool_0001_result:excerpt_0001']}}],
        'requirement_corrections': []}
    result, ask = await run(grounded(checked(kind='context')), correction, verdict())
    assert ask.await_count == 3
    assert result.status == 'corrected'
    assert result.metadata['answer_scientific_review']['status'] == 'verified'


@pytest.mark.asyncio
async def test_truncated_draft_is_not_whole_answer_scientific_coverage():
    result, ask = await run(grounded(), verdict(), draft='x' * (review.MAX_DRAFT_CHARS + 1))
    assert ask.await_count == 2
    assert result.status == 'unavailable'
    assert result.metadata['answer_scientific_review']['status'] == 'unavailable'


@pytest.mark.asyncio
async def test_non_dataset_default_retains_existing_plain_answer_protocol():
    value = checked()
    del value['answer_scientific_checks'], value['answer_scope_check']
    result, ask = await run(value, scope=False)
    ask.assert_awaited_once()
    assert result.status == 'verified'
    assert 'answer_scientific_review' not in result.metadata
