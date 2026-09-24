"""Root-shape diagnostics describe only host-owned contract facts."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.services import analysis_answer_review as review
from test_answer_review_schema_recovery import (
    GOOD, covered_response, observations, assert_frozen_shape_recovery,
)
from test_analysis_answer_review import paragraph, response

SECRET = 'PRIVATE_PROVIDER_SECRET /Users/secret/private-source'
FIELDS = ['paragraphs', 'unsupported_claims', 'requirement_checks',
          'answer_scientific_checks', 'answer_scope_check']


def root_value(*, recovery=False):
    value = json.loads(response(paragraph(GOOD)).content)
    value.update(answer_scientific_checks=[], answer_scope_check={})
    if recovery:
        value['answer_complete'] = True
    return value


CASES = [(f'missing_{name}', 'review_schema_root_fields', [name], 0) for name in FIELDS] + [
    ('extra', 'review_schema_root_fields', [], 1),
    ('recovery_field_initial', 'review_schema_root_fields', [], 1),
    ('unsupported_bool', 'review_schema_unsupported_claims_type', None, None),
    ('paragraphs_type', 'review_schema_paragraphs_type', None, None),
    ('paragraphs_empty', 'review_schema_paragraphs_count', None, None),
    ('paragraphs_many', 'review_schema_paragraphs_count', None, None),
    ('requirements_type', 'review_schema_requirement_checks_type', None, None),
    ('recovery_missing_complete', 'review_schema_root_fields', ['answer_complete'], 0),
    ('recovery_complete_type', 'review_schema_answer_complete_type', None, None),
]


@pytest.mark.parametrize('case,code,missing,extra', CASES)
def test_fourteen_existing_root_failures_have_safe_actionable_categories(case, code, missing, extra):
    recovery = case.startswith('recovery_') and case != 'recovery_field_initial'
    value = root_value(recovery=recovery)
    if case.startswith('missing_'): value.pop(case.removeprefix('missing_'))
    elif case == 'extra': value[SECRET] = SECRET
    elif case == 'recovery_field_initial': value['answer_complete'] = True
    elif case == 'unsupported_bool': value['unsupported_claims'] = SECRET
    elif case == 'paragraphs_type': value['paragraphs'] = SECRET
    elif case == 'paragraphs_empty': value['paragraphs'] = []
    elif case == 'paragraphs_many': value['paragraphs'] *= 65
    elif case == 'requirements_type': value['requirement_checks'] = SECRET
    elif case == 'recovery_missing_complete': value.pop('answer_complete')
    elif case == 'recovery_complete_type': value['answer_complete'] = SECRET
    with pytest.raises(review.ReviewSchemaError) as caught:
        review._validate_review_shape(value, [], recovery=recovery, answer_scientific_scope=True)
    assert caught.value.code == code
    details = getattr(caught.value, 'diagnostics', {})
    if missing is not None:
        assert details == {'missing_fields': missing, 'extra_field_count': extra}
    else:
        assert details == {}
    assert SECRET not in json.dumps({'code': caught.value.code, 'details': details}) + str(caught.value)


@pytest.mark.asyncio
async def test_missing_and_malicious_extra_fields_are_repaired_without_echoing_provider_data():
    malformed = json.loads(response(paragraph(SECRET)).content)
    malformed.pop('unsupported_claims')
    malformed[SECRET] = {'secret_body': SECRET}
    evidence = observations()
    before = copy.deepcopy(evidence.__dict__)
    ask = AsyncMock(side_effect=[AIMessage(content=json.dumps(malformed)), covered_response(paragraph(GOOD))])
    result = await review.review_answer(ask=ask, question='Explain the original observation',
        draft='Original fixed draft', files=[], evidence=evidence)
    assert ask.await_count == 2 and result.status == 'corrected'
    assert result.metadata['review_schema_diagnostics'] == {
        'missing_fields': ['unsupported_claims'], 'extra_field_count': 1}
    assert_frozen_shape_recovery(ask)
    assert evidence.__dict__ == before
    feedback = ask.await_args_list[1].args[0][0].content
    assert 'Host structural details:' in feedback and '"missing_fields":["unsupported_claims"]' in feedback
    assert SECRET not in feedback + json.dumps(result.metadata) + result.text


@pytest.mark.asyncio
async def test_second_bad_candidate_closes_and_updates_diagnostics_without_third_request():
    first = json.loads(response(paragraph(GOOD)).content)
    first[SECRET] = SECRET
    second = json.loads(covered_response(paragraph(GOOD)).content)
    second['paragraphs'] = []
    ask = AsyncMock(side_effect=[AIMessage(content=json.dumps(first)), AIMessage(content=json.dumps(second)), response(paragraph(GOOD))])
    result = await review.review_answer(ask=ask, question='Explain', draft='', files=[], evidence=observations())
    assert ask.await_count == 2 and result.status == 'unavailable'
    assert result.metadata['review_schema_error'] == 'review_schema_paragraphs_count'
    assert 'review_schema_diagnostics' not in result.metadata  # no stale first-error details
    assert GOOD not in result.text and SECRET not in json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_diagnostics_are_filtered_again_at_feedback_and_metadata_boundary(monkeypatch):
    original = review._validate_review_shape
    count = 0
    def injected(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            error = review.ReviewSchemaError('review_schema_root_fields')
            error.diagnostics = {'missing_fields': ['paragraphs', SECRET, 'paragraphs'],
                'extra_field_count': 1000000, 'unknown_secret_key': SECRET}
            raise error
        return original(*args, **kwargs)
    monkeypatch.setattr(review, '_validate_review_shape', injected)
    ask = AsyncMock(side_effect=[response(paragraph(GOOD)), covered_response(paragraph(GOOD))])
    result = await review.review_answer(ask=ask, question='Explain', draft='', files=[], evidence=observations())
    assert ask.await_count == 2
    assert result.metadata['review_schema_diagnostics'] == {
        'missing_fields': ['paragraphs'], 'extra_field_count': 64, 'extra_field_count_capped': True}
    assert SECRET not in json.dumps(result.metadata) + '\n'.join(m.content for c in ask.await_args_list for m in c.args[0])


@pytest.mark.asyncio
@pytest.mark.parametrize('native', ['valid', 'invalid'])
async def test_native_tool_request_after_shape_failure_never_becomes_another_schema_attempt(native):
    bad = AIMessage(content='{}')
    forbidden = (AIMessage(content='{}', tool_calls=[{'name': 'shell_run', 'id': 'call', 'args': {'command': SECRET}}])
        if native == 'valid' else AIMessage(content='{}', invalid_tool_calls=[{'name': 'shell_run', 'id': 'call', 'args': '{', 'error': SECRET}]))
    ask = AsyncMock(side_effect=[bad, forbidden, response(paragraph(GOOD))])
    result = await review.review_answer(ask=ask, question='Explain', draft='', files=[], evidence=observations())
    assert result.status == 'unavailable' and ask.await_count == 2
    assert result.metadata['reason'] == 'review_requested_tools'
    assert SECRET not in json.dumps(result.metadata) + result.text


@pytest.mark.asyncio
async def test_cancellation_of_existing_recovery_is_not_swallowed():
    ask = AsyncMock(side_effect=[AIMessage(content='{}'), asyncio.CancelledError(), response(paragraph(GOOD))])
    with pytest.raises(asyncio.CancelledError):
        await review.review_answer(ask=ask, question='Explain', draft='', files=[], evidence=observations())
    assert ask.await_count == 2


@pytest.mark.parametrize('reports,scientific', [(True, False), (False, True), (True, True)])
def test_optional_report_and_science_checks_do_not_become_new_required_root_fields(reports, scientific):
    value = json.loads(response(paragraph(GOOD)).content)
    review._validate_review_shape(value, [], reports=reports, scientific=scientific)


@pytest.mark.parametrize('diagnostics,expected', [
    ({'missing_fields': [SECRET, 'answer_complete', 'answer_complete'], 'extra_field_count': 9999,
      SECRET: SECRET}, {'missing_fields': ['answer_complete'], 'extra_field_count': 64, 'extra_field_count_capped': True}),
    ({'missing_fields': SECRET, 'extra_field_count': 1}, {}),
    ({'missing_fields': ['paragraphs'], 'extra_field_count': True}, {}),
    ({'missing_fields': ['paragraphs'], 'extra_field_count': -1}, {}),
])
def test_constructor_projects_only_finite_host_details_and_codes(diagnostics, expected):
    error = review.ReviewSchemaError(SECRET, diagnostics=diagnostics)
    assert error.code == 'review_schema_root' and error.diagnostics == expected
    error.code = SECRET
    assert review._review_failure_code(error) == 'review_schema_root'
    assert SECRET not in str(error) + json.dumps(error.diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,bad,expected', [
    ('paragraphs', {}, 'JSON array containing 1–64 complete paragraph objects'),
    ('paragraphs', [], 'JSON array containing 1–64 complete paragraph objects'),
    ('unsupported_claims', 'false', 'strict JSON boolean (true or false)'),
    ('requirement_checks', {}, 'requirement_checks must be a JSON array'),
])
async def test_existing_recovery_gives_fixed_actionable_types_and_limits(field, bad, expected):
    first = json.loads(response(paragraph(GOOD)).content)
    first[field] = bad
    ask = AsyncMock(side_effect=[AIMessage(content=json.dumps(first)), covered_response(paragraph(GOOD))])
    result = await review.review_answer(ask=ask, question='Explain', draft='', files=[], evidence=observations())
    assert result.status == 'corrected' and ask.await_count == 2
    assert expected in ask.await_args_list[1].args[0][0].content
    assert_frozen_shape_recovery(ask)


def test_action_table_has_only_fixed_host_codes_and_boolean_recovery_instruction():
    assert set(review._REVIEW_SCHEMA_ACTIONS) <= review._REVIEW_SCHEMA_CODES
    assert 'strict JSON boolean' in review._REVIEW_SCHEMA_ACTIONS['review_schema_answer_complete_type']
    assert 'exact host field set' in review._REVIEW_SCHEMA_ACTIONS['review_schema_root_fields']
    assert review._REVIEW_SCHEMA_ACTIONS.get(review._safe_review_schema_code(SECRET), '') == ''
