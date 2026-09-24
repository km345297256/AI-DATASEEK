"""Fixed nested-contract feedback improves recoverability, never acceptance rules."""
import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.domain.services import analysis_answer_review as review
from test_answer_review_schema_recovery import REQUIREMENT, observations
from test_text_scientific_review_integration import checked, grounded, run, verdict
from test_text_scientific_schema_recovery import broken

SECRET = 'PRIVATE_FAILED_CANDIDATE /Users/private/never-return'
NESTED = sorted(code for code in review._REVIEW_SCHEMA_CODES if code.startswith((
    'review_requirement_', 'review_answer_science_', 'review_answer_scope_')))
MUTATIONS = {
    'review_answer_science_count':'count', 'review_answer_science_fields':'fields',
    'review_answer_science_dimension':'dimension', 'review_answer_science_status':'status',
    'review_answer_science_scope':'evidence_scope',
    'review_answer_science_indices':'indices', 'review_answer_science_evidence_list':'evidence_list',
    'review_answer_science_evidence_item':'evidence_item', 'review_answer_scope_fields':'scope_fields',
    'review_answer_scope_status':'scope_status', 'review_answer_scope_indices':'scope_indices',
}
CHECK = {'index':0, 'status':'met', 'evidence':[{'source_id':'tool_0001_result','quote':'mean=3'}]}


def malformed(code):
    if code in MUTATIONS:
        first=broken(MUTATIONS[code]);requirements=[]
    else:
        first=checked();requirements=[REQUIREMENT]
        checks={
            'review_requirement_object':[SECRET],
            'review_requirement_fields':[{SECRET:SECRET}],
            'review_requirement_index':[{**CHECK,'index':True}],
            'review_requirement_status':[{**CHECK,'status':SECRET}],
            'review_requirement_evidence_type':[{**CHECK,'evidence':SECRET}],
            'review_requirement_duplicate':[CHECK,CHECK],
            'review_requirement_missing':[],
        }
        first['requirement_checks']=copy.deepcopy(checks[code])
    first['paragraphs'][0]['text']=SECRET
    return first,requirements


@pytest.mark.asyncio
@pytest.mark.parametrize('code',NESTED)
async def test_every_existing_nested_code_delivers_fixed_action_on_the_same_frozen_input(code):
    first,requirements=malformed(code)
    second=checked()
    if requirements:second['requirement_checks']=[copy.deepcopy(CHECK)]
    original_first=copy.deepcopy(first)
    evidence=observations();saved=copy.deepcopy(evidence.__dict__)
    responses=([grounded(first),grounded(second),verdict(second)] if requirements else
               [grounded(),verdict(first),verdict(second)])
    ask=AsyncMock(side_effect=[json.dumps(value) for value in responses])
    result=await review.review_answer(ask=ask, question='Describe the original observation',
        draft='Original immutable draft', files=[], evidence=evidence,
        requirements=requirements, answer_scientific_scope=True, language='en')
    assert result.status==('corrected' if requirements else 'verified') and ask.await_count==3
    assert (result.metadata['review_schema_error'] if requirements else
            result.metadata['final_candidate_review']['error'])==code
    recovery_calls=ask.await_args_list[:2] if requirements else ask.await_args_list[1:]
    a,b=[call.args[0] for call in recovery_calls]
    assert a[1] is b[1] and a[1].content==b[1].content and len(a)==len(b)==2
    assert evidence.__dict__==saved and first==original_first
    feedback=b[0].content
    assert 'Required structural correction:' in feedback
    if code=='review_answer_science_indices':
        assert 'supplied paragraph_indices' in feedback and 'candidate_paragraphs' in feedback
    elif code=='review_answer_scope_indices':
        assert 'supplied paragraph_indices' in feedback and 'subset' in feedback
    else:
        action=review._REVIEW_SCHEMA_ACTIONS.get(code,'')
        assert action and action in feedback
    assert SECRET not in feedback+json.dumps(result.metadata)+result.text


def final_response(count):
    value=checked()
    value['paragraphs']*=count
    for check in value['answer_scientific_checks']:check['paragraph_indices']=list(range(count))
    value['answer_scope_check']['paragraph_indices']=list(range(count))
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize('count',[1,3])
async def test_indices_recovery_uses_actual_returned_paragraph_count_not_original_draft(count):
    first=final_response(count)
    first['answer_scientific_checks'][0]['paragraph_indices']=[0,1]  # invalid for both actual lengths
    result,ask=await run(grounded(first),verdict(first),verdict(final_response(count)),draft='One original draft paragraph.')
    assert result.status=='verified' and ask.await_count==3
    recovery=ask.await_args_list[-1].args[0]
    frozen=json.loads(recovery[-1].content)
    assert frozen['paragraph_indices']==list(range(count))
    assert len(frozen['candidate_paragraphs'])==count and len(frozen['frozen_paragraphs'])==count
    assert recovery[-1] is ask.await_args_list[-2].args[0][-1]
    assert 'supplied paragraph_indices' in recovery[0].content
    assert 'candidate_paragraphs' in recovery[0].content
    assert 'Do not' in recovery[0].content
    returned=final_response(count)
    assert len(returned['paragraphs'])==count
    assert result.metadata['answer_scientific_review']['paragraph_count']==count


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['incomplete','unclear'])
async def test_scope_recovery_preserves_valid_empty_subset_without_claiming_completion(status):
    second=final_response(2)
    second['answer_scope_check']={'status':status,'paragraph_indices':[]}
    first=final_response(2);first['answer_scope_check']['paragraph_indices']=[]
    result,ask=await run(grounded(first),verdict(first),verdict(second))
    assert ask.await_count==3 and result.status=='unavailable'
    assert result.metadata['answer_scope_review']['status'] in {'incomplete','unavailable'}
    action=ask.await_args_list[-1].args[0][0].content
    assert 'incomplete/unclear' in action and 'subset' in action and '[]' in action


@pytest.mark.asyncio
async def test_guidance_does_not_fill_indices_or_add_third_attempt():
    first=broken('indices');second=copy.deepcopy(first)
    before=copy.deepcopy(second)
    result,ask=await run(grounded(),verdict(first),verdict(second),verdict())
    assert ask.await_count==3 and result.status=='unavailable'
    assert second==before and second['answer_scientific_checks'][0]['paragraph_indices']==[]
    assert result.metadata['final_candidate_review']['error']=='review_answer_science_indices'


@pytest.mark.asyncio
@pytest.mark.parametrize('science,scope',[('unclear','complete'),('rejected','complete'),('verified','incomplete')])
async def test_legitimate_scientific_or_scope_negative_never_receives_structure_feedback(science,scope):
    result,ask=await run(grounded(),verdict(checked(science=science,scope=scope)))
    assert ask.await_count==2 and result.status=='unavailable'
    assert 'review_schema_repair_attempted' not in result.metadata
    assert result.metadata['final_candidate_review']['protocol_attempts']==1


@pytest.mark.asyncio
@pytest.mark.parametrize('native',['valid','invalid'])
async def test_native_call_after_nested_error_stays_forbidden_without_dispatch_or_extra_attempt(native):
    native_message=(AIMessage(content='{}',tool_calls=[{'id':'forbidden','name':'shell_run','args':{'command':SECRET}}])
        if native=='valid' else AIMessage(content='{}',invalid_tool_calls=[{'id':'forbidden','name':'shell_run','args':'{','error':SECRET}]))
    ask=AsyncMock(side_effect=[json.dumps(grounded()),json.dumps(verdict(broken('indices'))),
                              native_message,json.dumps(verdict())])
    result=await review.review_answer(ask=ask,question='Describe',draft='',files=[],evidence=observations(),answer_scientific_scope=True)
    assert ask.await_count==3 and result.status=='unavailable'
    assert result.metadata['final_candidate_review']['error']=='review_requested_tools'
    assert SECRET not in result.text+json.dumps(result.metadata)


@pytest.mark.asyncio
async def test_nested_recovery_cancellation_still_propagates():
    ask=AsyncMock(side_effect=[json.dumps(grounded()),json.dumps(verdict(broken('indices'))),asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await review.review_answer(ask=ask,question='Describe',draft='',files=[],evidence=observations(),answer_scientific_scope=True)
    assert ask.await_count==3


@pytest.mark.asyncio
async def test_final_reviewer_cannot_replace_frozen_answer_or_persist_rejected_text():
    invalid=verdict()
    invalid['paragraphs']=[{'text':SECRET}]
    result,ask=await run(grounded(),invalid,verdict())
    assert ask.await_count==3 and result.status=='verified'
    assert result.metadata['final_candidate_review']['error']=='final_review_schema_root'
    assert result.metadata['final_candidate_review']['schema_recovered'] is True
    a,b=[call.args[0] for call in ask.await_args_list[1:]]
    assert a[-1] is b[-1]
    assert SECRET not in result.text+json.dumps(result.metadata)+'\n'.join(message.content for message in b)
