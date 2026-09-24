"""Versioned structured targets use the existing bounded review, not a new run.

These tests assert the contract and failure boundary. Scripted reviewer replies
are not a test of a provider's ability to discover statistical errors.
"""
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services import analysis_answer_review as review
from app.domain.services.analysis_report_review import load_report_targets, fit_report_targets
from app.domain.services.analysis_scientific_review import SCIENTIFIC_DIMENSIONS
from test_analysis_report_content import manifest, OwnerRangeStore, USER, SESSION, load
from test_analysis_answer_review import tool
from test_report_answer_review import answer, GOOD


async def prepared(body=b'{"mean":3}', suffix='json'):
    info = manifest(body, path=f'/home/ubuntu/output/results.{suffix}')
    store = OwnerRangeStore([(info, body)])
    target = await load(info, store)
    return info, store, target


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix,body', [
    ('json', b'{"mean":3,"nested":{"interval":[1,5]}}'),
    ('csv', '\ufeffgroup,value,note\r\nA,3,"quoted,comma"\r\nB,,"multi\nline"\r\n'.encode()),
    ('tsv', 'group\tvalue\tnote\nA\t3\t"quoted\ttab"\nB\t\t"many\nlines"\n'.encode()),
])
async def test_whole_structure_and_original_bytes_are_preserved(suffix, body):
    info, store, target = await prepared(body, suffix)
    assert target.read_complete and target.reason == 'ready'
    assert target.text.encode() == body
    assert target.payload()['target_kind'] == 'structured'
    assert target.payload()['format'] == suffix
    assert ''.join(block['text'] for block in target.blocks()).encode() == body
    assert target.sha256 == hashlib.sha256(body).hexdigest()
    assert store.range_calls == [(info.file_id, USER, 0, len(body))]
    assert fit_report_targets([target], text_budget=len(target.text))[0].text == target.text


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix,body', [
    ('json', b'{"mean":3,"mean":8}'),
    ('json', b'{"mean":NaN}'), ('json', b'{"mean":Infinity}'),
    ('json', b'{"mean":3} trailing'), ('json', b'{"mean":'),
    ('csv', b'a,b\n1,2,3\n'), ('csv', b'a,b\n1,"unterminated'),
    ('tsv', b'a\tb\n1\t2\t3\n'),
])
async def test_invalid_complete_structure_is_not_a_reviewed_prefix(suffix, body):
    _, _, target = await prepared(body, suffix)
    assert target.read_complete and target.text is None
    assert target.reason == 'invalid_structured_content'
    assert target.blocks() == [] and target.payload()['coverage'] == 'unverified'


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    b'{"path":"\\u002fUsers\\u002ffixture\\u002fdata"}',
    b'{"path":"\\/Users\\/fixture\\/data"}',
    b'{"\\u0061pi_key":"secret-test-placeholder"}',
])
async def test_decoded_json_privacy_cannot_be_bypassed_with_escapes(body):
    _, _, target = await prepared(body)
    assert target.reason == 'protected_report_content'
    assert target.text is None and target.read_complete
    assert 'placeholder' not in json.dumps(target.payload())


def checked_answer(messages, *, scientific_status='verified', include=True, mutation=None):
    value = json.loads(answer(messages))
    payload = json.loads(messages[-1].content)
    if include:
        value['scientific_checks'] = [{
            'dimension': dimension, 'status': scientific_status,
            'target_ids': [item['report_id'] for item in payload['report_targets']
                           if item.get('target_kind') == 'structured'],
            'evidence': [{'source_id': 'tool_0001_result', 'quote': 'mean=3; unit=mg'}]
                        if scientific_status == 'verified' else [],
        } for dimension in SCIENTIFIC_DIMENSIONS]
    if mutation:
        mutation(value)
    return json.dumps(value)


async def run_structured(ask, body=b'{"mean":3}', suffix='json'):
    info, store, target = await prepared(body, suffix)
    evidence = review.AnswerEvidence()
    evidence.observe(tool())
    result = await review.review_answer(ask=ask, question='Explain the observed data', draft=GOOD,
        files=[info], evidence=evidence, report_targets=[target])
    return result, store


@pytest.mark.asyncio
async def test_legacy_citation_only_green_cannot_skip_scientific_checks():
    ask = AsyncMock(side_effect=lambda messages: checked_answer(messages, include=False))
    result, _ = await run_structured(ask, b'{"threshold":2,"difference":3,"significant":false}')
    assert ask.await_count == 1
    assert result.text.startswith(GOOD) and result.status == 'unavailable'
    assert result.metadata['reason'] == 'scientific_validation_unavailable'
    assert result.metadata['scientific_review']['status'] == 'unavailable'
    assert result.missing_requirement_indices == ()


@pytest.mark.asyncio
@pytest.mark.parametrize('status,expected', [('rejected','scientific_validation_rejected'),
                                           ('unclear','scientific_validation_unavailable')])
async def test_semantic_verdict_is_preserved_without_new_computation(status, expected):
    ask = AsyncMock(side_effect=lambda messages: checked_answer(messages, scientific_status=status))
    result, _ = await run_structured(ask)
    assert ask.await_count == 1 and result.metadata['reason'] == expected
    assert result.status == 'unavailable' and result.text.startswith(GOOD)
    assert result.metadata['report_review']['status'] == 'verified'


@pytest.mark.asyncio
async def test_raw_csv_export_has_a_scoped_review_path_without_invented_model():
    ask = AsyncMock(side_effect=checked_answer)
    result, _ = await run_structured(ask, b'group,value\nA,3\n', 'csv')
    assert result.status == 'verified' and ask.await_count == 1
    assert result.metadata['scientific_review']['checked_dimensions'] == 4
    assert 'Do not require fitting a model' in ask.await_args.args[0][0].content


@pytest.mark.asyncio
async def test_correct_checks_cannot_upgrade_budget_omission():
    # Even otherwise idle evidence space cannot fit this whole serialized body.
    body = json.dumps({'values': 'x' * (review.MAX_DRAFT_CHARS + review.MAX_EVIDENCE_CHARS)}).encode()
    ask = AsyncMock(side_effect=checked_answer)
    result, _ = await run_structured(ask, body)
    target = json.loads(ask.await_args.args[0][-1].content)['report_targets'][0]
    assert target['blocks'] == [] and target['coverage'] == 'unverified'
    assert result.status == 'unavailable' and ask.await_count == 1


@pytest.mark.asyncio
async def test_schema_recovery_keeps_complete_scientific_contract():
    calls = 0
    def respond(messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            return '{"invalid_schema":true}'
        return checked_answer(messages, mutation=lambda value: value.update(answer_complete=True))
    ask = AsyncMock(side_effect=respond)
    result, _ = await run_structured(ask)
    assert calls == 2 and result.status == 'corrected'
    assert ask.await_args_list[0].args[0][-1] is ask.await_args_list[1].args[0][-1]
    assert 'exactly six fields' in ask.await_args_list[1].args[0][0].content


@pytest.mark.asyncio
async def test_real_runner_reads_current_csv_and_retains_download_without_semantic_green():
    from types import SimpleNamespace
    from test_analysis_repair_flow import scenario, output, collect
    from test_analysis_answer_review_flow import with_observed_results, real_reviewer
    body = b'group,mean\nA,3\n'
    record, file = output('summary.csv', 'table', digest=hashlib.sha256(body).hexdigest())
    record['size'] = file.size = len(body)
    file.user_id = 'fixture-user'
    file.metadata.update(source='sandbox_artifact', session_id='fixture-session', artifact_size=len(body))
    runner, flow, step, message, state = scenario([[(record,file)]], [{'kind':'table'}])
    runner._user_id = 'fixture-user'
    runner._file_storage = SimpleNamespace(get_file_info=AsyncMock(return_value=file),
                                          download_file_range=AsyncMock(return_value=(body,file)))
    agent = with_observed_results(runner, flow, [GOOD], facts=['mean=3; unit=mg'])
    ask = AsyncMock(side_effect=lambda messages: checked_answer(messages, include=False))
    real_reviewer(agent, ask)
    await collect(runner, message)
    assert step.outcome.status == 'partial' and step.outcome.reason_code == 'scientific_validation_unavailable'
    assert len(state['prompts']) == 1 and ask.await_count == 1
    assert not step.outcome.can_resume and not flow._artifact_repair_requests
    assert file.file_id in {item.file_id for item in runner._generated_files}
    runner._file_storage.download_file_range.assert_awaited_once()
