"""Bound complete executed methods, not arbitrary long results or code evidence."""
import copy
import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services import analysis_answer_review as review
from app.domain.services.analysis_checkpoint import prepare_continuation
from test_analysis_answer_review import tool, paragraph, response
from test_analysis_checkpoint import checkpoint_fixture
from test_answer_review_executed_method_metadata import add_program
from test_answer_review_execution_evidence import bound_program
from app.domain.services.program_execution import trusted_program_execution_feedback


LONG_SOURCE = "# Complete method body; scientific constants are not observations\n" + "value = 123456789\n" * 600 + "print('END_OF_COMPLETE_METHOD')\n"


def observed(content=LONG_SOURCE, **kwargs):
    evidence = review.AnswerEvidence(); evidence.begin_step("plot")
    add_program(evidence, content=content, **kwargs)
    return evidence


def result(evidence):
    return next(s for s in reversed(evidence.render_sources()) if s['kind']=='tool_result' and s['function']=='program_run')


def run_saved(evidence, content, *, path='/home/ubuntu/code.py', call_id='run'):
    call={'id':call_id,'name':'program_run','args':{'id':'shell-'+call_id,'exec_dir':'/home/ubuntu','script_path':path,'argv':[]}}
    receipt={'version':1,'script_path':path,'source_digest':hashlib.sha256(content.encode()).hexdigest(),'returncode':0}
    core,ledger=bound_program(call,receipt)
    proof=trusted_program_execution_feedback(core,call,None,ledger)
    evidence.observe(tool('program_run',call=call_id,args=call['args'],data={'returncode':0,'output':'completed original observations'}),trusted_program_execution=proof)


@pytest.mark.parametrize("length", [7568, 11200])
def test_long_complete_method_survives_bounded_request_with_exact_host_receipt(length):
    content = '#'+('x'*(length-34))+"\nprint('COMPLETE_TAIL_METHOD')\n"
    evidence = observed(content)
    before = copy.deepcopy(evidence._calls)
    rendered = evidence.render_sources(); program = result(evidence)
    assert rendered[0]['truncated'] and len(rendered[0]['text']) <= review.MAX_SOURCE_CHARS
    assert program['executed_source_coverage']=='full'
    assert program['executed_program_source']['content']==content
    assert program['executed_program_source']['method_only'] is True
    assert 'COMPLETE_TAIL_METHOD' not in program['text']
    assert len(review._json(rendered)) <= review.MAX_EVIDENCE_CHARS
    assert evidence._calls==before


def test_larger_result_is_not_cut_to_make_room_for_complete_source():
    evidence=observed(output='observed results '+('x'*5500))
    saved=evidence._calls['run']['sources'][1]['text']
    assert result(evidence)['text']==saved
    assert result(evidence)['executed_program_source']['content']==LONG_SOURCE


def test_source_snapshot_is_immutable_and_newline_hash_matches_actual_bytes():
    evidence=review.AnswerEvidence(); evidence.begin_step('plot')
    args={'file':'/home/ubuntu/code.py','content':LONG_SOURCE,'leading_newline':True,'trailing_newline':True}
    event=tool('file_write',call='write',args=args); evidence.observe(event)
    event.function_args['content']='MUTATED_AFTER_OBSERVATION'
    raw='\n'+LONG_SOURCE+'\n'
    call={'id':'run','name':'program_run','args':{'id':'shell','exec_dir':'/home/ubuntu','script_path':args['file'],'argv':[]}}
    receipt={'version':1,'script_path':args['file'],'source_digest':hashlib.sha256(raw.encode()).hexdigest(),'returncode':0}
    core,ledger=bound_program(call,receipt)
    proof=trusted_program_execution_feedback(core,call,None,ledger)
    evidence.observe(tool('program_run',call='run',args=call['args'],data={'returncode':0,'output':'observed'}),trusted_program_execution=proof)
    assert result(evidence)['executed_program_source']['content']==raw
    assert 'MUTATED_AFTER_OBSERVATION' not in review._json(evidence.render_sources())


@pytest.mark.parametrize('fault',['no_private_receipt','wrong_digest','different_step','append','write_failed'])
def test_long_source_requires_same_step_successful_write_and_private_exact_execution(fault):
    evidence=review.AnswerEvidence(); evidence.begin_step('plot')
    path='/home/ubuntu/code.py'
    evidence.observe(tool('file_write',call='write',args={'file':path,'content':LONG_SOURCE,'append':fault=='append'},success=fault!='write_failed'))
    if fault=='different_step': evidence.begin_step('other')
    call={'id':'run','name':'program_run','args':{'id':'shell','exec_dir':'/home/ubuntu','script_path':path,'argv':[]}}
    receipt={'version':1,'script_path':path,'source_digest':('a'*64 if fault=='wrong_digest' else hashlib.sha256(LONG_SOURCE.encode()).hexdigest()),'returncode':0}
    core,ledger=bound_program(call,receipt); proof=trusted_program_execution_feedback(core,call,None,ledger)
    evidence.observe(tool('program_run',call='run',args=call['args'],data={'returncode':0,'program_execution':receipt,'executed_program_source':{'content':LONG_SOURCE}}),trusted_program_execution=None if fault=='no_private_receipt' else proof)
    assert result(evidence)['executed_source_coverage']=='unverified'
    assert 'executed_program_source' not in result(evidence)


def test_overwrite_versions_only_join_the_observed_executed_digest():
    evidence=review.AnswerEvidence(); evidence.begin_step('plot')
    newer=LONG_SOURCE.replace('123456789','987654321')
    for call_id,content in [('first-write',LONG_SOURCE),('second-write',newer)]:
        evidence.observe(tool('file_write',call=call_id,args={'file':'/home/ubuntu/code.py','content':content}))
    run_saved(evidence,LONG_SOURCE,call_id='old-execution')
    run_saved(evidence,newer,call_id='new-execution')
    rendered=evidence.render_sources()
    assert [r['executed_program_source']['content'] for r in rendered if 'executed_program_source' in r]==[LONG_SOURCE,newer]
    assert all(r['executed_source_coverage']=='full' for r in rendered if 'executed_program_source' in r)


def test_count_budget_is_shared_and_does_not_revoke_retained_exact_sources():
    evidence=review.AnswerEvidence(); evidence.begin_step('plot')
    content='#'+'x'*7000+'\n'
    for i in range(5):
        path=f'/home/ubuntu/code{i}.py'
        evidence.observe(tool('file_write',call=f'write{i}',args={'file':path,'content':content}))
        run_saved(evidence,content,path=path,call_id=f'run{i}')
    programs=[s for s in evidence.render_sources() if s['kind']=='tool_result' and s['function']=='program_run']
    assert [p['executed_source_coverage'] for p in programs]==['full']*4+['unverified']
    assert sum('program_source' in c for c in evidence._calls.values())==4


def test_cache_total_budget_and_call_eviction_never_resurrect_missing_source():
    evidence=review.AnswerEvidence(); evidence.begin_step('plot')
    content='#'+'x'*17000+'\n'
    for i in range(3):
        path=f'/home/ubuntu/code{i}.py'
        evidence.observe(tool('file_write',call=f'write{i}',args={'file':path,'content':content}))
    assert sum('program_source' in c for c in evidence._calls.values())==2
    for i in range(3): run_saved(evidence,content,path=f'/home/ubuntu/code{i}.py',call_id=f'run{i}')
    assert result(evidence)['executed_source_coverage']=='unverified'
    for i in range(review.MAX_CALLS): evidence.observe(tool('file_read',call=f'extra{i}',args={'file':'/home/ubuntu/input.csv'},data={'content':'n,x\n1,2'}))
    run_saved(evidence,content,path='/home/ubuntu/code0.py',call_id='after-eviction')
    assert result(evidence)['executed_source_coverage']=='unverified'
    assert not any('program_source' in c for c in evidence._calls.values())


def test_reusing_call_id_in_later_step_cannot_inherit_source_or_private_execution():
    evidence=review.AnswerEvidence(); evidence.begin_step('plot')
    evidence.observe(tool('file_write',call='write',args={'file':'/home/ubuntu/code.py','content':LONG_SOURCE}))
    run_saved(evidence,LONG_SOURCE)
    evidence.begin_step('another')
    run_saved(evidence,LONG_SOURCE)
    assert result(evidence)['executed_source_coverage']=='unverified'
    assert 'program_execution' not in evidence._calls['run']


def test_truncated_stdout_does_not_erase_host_verified_method_coverage():
    evidence=observed(output='first observed output\n'+'z'*9000)
    program=result(evidence)
    assert program['truncated'] is True
    assert program['executed_source_coverage']=='full'
    assert program['executed_program_source']['content']==LONG_SOURCE
    assert 'first observed output' in program['text']
    assert len(program['text'])<=review.MAX_SOURCE_CHARS


def test_unrelated_large_write_does_not_make_a_complete_execution_method_unverified():
    evidence=observed()
    evidence.observe(tool('file_write',call='unrelated',args={'file':'/home/ubuntu/notes.txt','content':'noise'*4000}))
    assert evidence.truncated
    assert result(evidence)['executed_source_coverage']=='full'


@pytest.mark.parametrize('flag',['append','leading_newline','trailing_newline','sudo'])
def test_malformed_write_flags_cannot_be_used_to_invent_exact_bytes(flag):
    evidence=review.AnswerEvidence(); evidence.begin_step('plot')
    evidence.observe(tool('file_write',call='write',args={'file':'/home/ubuntu/code.py','content':LONG_SOURCE,flag:'false'}))
    run_saved(evidence,LONG_SOURCE)
    assert result(evidence)['executed_source_coverage']=='unverified'


@pytest.mark.parametrize('limit',['file','total','count','render'])
def test_cache_or_render_limit_never_produces_partial_method_as_full(monkeypatch,limit):
    if limit=='file': monkeypatch.setattr(review,'MAX_PROGRAM_SOURCE_CHARS',7000)
    elif limit=='total': monkeypatch.setattr(review,'MAX_PROGRAM_SOURCE_TOTAL_CHARS',7000)
    elif limit=='count': monkeypatch.setattr(review,'MAX_PROGRAM_SOURCE_FILES',0)
    evidence=observed()
    if limit=='render': monkeypatch.setattr(review,'MAX_EVIDENCE_CHARS',12000)
    program=result(evidence)
    assert program['executed_source_coverage']=='unverified'
    assert isinstance(program['executed_source_reason'],str)
    assert 'executed_program_source' not in program
    assert program['text']==evidence._calls['run']['sources'][1]['text']


def test_no_join_legacy_snapshot_is_explicitly_unverified_not_silently_reconstructed():
    evidence=observed(); snapshot=evidence.checkpoint_snapshot(step_ids={'plot'})
    for c in snapshot['calls']: c.pop('program_source',None)
    restored=review.AnswerEvidence.from_checkpoint_snapshot(snapshot,step_ids={'plot'})
    assert result(restored)['executed_source_coverage']=='unverified'
    assert 'executed_program_source' not in result(restored)


@pytest.mark.parametrize('change',['content','digest','request','step','shape','too_large'])
def test_checkpoint_rejects_changed_cache_or_unbounded_content(change):
    evidence=observed(); snapshot=evidence.checkpoint_snapshot(step_ids={'plot'})
    call=snapshot['calls'][0]; cache=call['program_source']
    if change=='content': cache['arguments_json']=cache['arguments_json'].replace('123456789','987654321')
    elif change=='digest': cache['source_digest']='f'*64
    elif change=='request': call['sources'][0]['text']='{}'
    elif change=='step': call['step_id']='another'
    elif change=='shape': cache['untrusted_execution']=True
    elif change=='too_large': cache['arguments_json']='x'*(review.MAX_PROGRAM_SOURCE_CHARS+1)
    with pytest.raises(ValueError,match='invalid_answer_evidence_checkpoint'):
        review.AnswerEvidence.from_checkpoint_snapshot(snapshot,step_ids={'plot'})


def test_checkpoint_roundtrip_rebuilds_private_projection_without_aliases():
    evidence=observed(); before=evidence.render_sources()
    snapshot=evidence.checkpoint_snapshot(step_ids={'plot'})
    restored=review.AnswerEvidence.from_checkpoint_snapshot(snapshot,step_ids={'plot'})
    snapshot['calls'][0]['program_source']['arguments_json']='changed'
    assert restored.render_sources()==before
    assert result(restored)['executed_source_coverage']=='full'
    assert 'executed_program_source' not in review._json(restored.checkpoint_snapshot(step_ids={'plot'}))


@pytest.mark.parametrize('limit',['total','count'])
def test_restore_enforces_aggregate_source_cache_bounds(monkeypatch,limit):
    evidence=observed(); snapshot=evidence.checkpoint_snapshot(step_ids={'plot'})
    monkeypatch.setattr(review,'MAX_PROGRAM_SOURCE_TOTAL_CHARS' if limit=='total' else 'MAX_PROGRAM_SOURCE_FILES',0)
    with pytest.raises(ValueError,match='invalid_answer_evidence_checkpoint'):
        review.AnswerEvidence.from_checkpoint_snapshot(snapshot,step_ids={'plot'})


def test_code_visible_only_in_method_context_cannot_satisfy_result_quotes():
    evidence=observed(content=LONG_SOURCE+'\nTAIL_ONLY_VALUE = 1212121212\n')
    program=result(evidence)
    assert '1212121212' in program['executed_program_source']['content']
    with pytest.raises(review.CitationValidationError):
        review._citations([{'source_id':program['source_id'],'quote':'1212121212'}],{program['source_id']:program})
    rendered,excerpts=review._citation_excerpts(evidence.render_sources())
    assert not any('1212121212' in e['quote'] for e in excerpts.values())

@pytest.mark.asyncio
@pytest.mark.parametrize('tamper',[False,True])
async def test_authenticated_delivery_checkpoint_covers_full_private_method(tamper):
    evidence=observed(); repository,sandbox,message,token=await checkpoint_fixture(reason_code='delivery_failed',answer_evidence=evidence)
    repository.checkpoint['claimed_by']='resume-input'; message.resume_from=token; message.client_message_id='resume-input'; message._accepted_event_seq=2
    if tamper:
        repository.checkpoint['answer_evidence']['calls'][0]['program_source']['arguments_json']+=' '
        before=len(sandbox.calls)
        with pytest.raises(ValueError): await prepare_continuation(repository,sandbox,'session-a','owner-a',message)
        assert len(sandbox.calls)==before
    else:
        checkpoint=await prepare_continuation(repository,sandbox,'session-a','owner-a',message)
        restored=review.AnswerEvidence.from_checkpoint_snapshot(checkpoint['answer_evidence'],step_ids={'inspect','plot'})
        assert result(restored)['executed_program_source']['content']==LONG_SOURCE


def test_method_constants_are_never_result_citation_text():
    evidence=observed(); program=result(evidence)
    with pytest.raises(review.CitationValidationError):
        review._citations([{'source_id':program['source_id'],'quote':'123456789'}],{program['source_id']:program})
    assert '123456789' not in program['text']


@pytest.mark.asyncio
async def test_complete_long_method_reaches_existing_review_call_without_public_exposure():
    evidence=observed(output='observed count=6')
    async def answer(messages):
        payload=json.loads(messages[-1].content)
        program=next(s for s in payload['sources'] if s.get('executed_source_coverage')=='full')
        assert program['executed_program_source']['content']==LONG_SOURCE
        return response(paragraph('Observed count is six.',source=program['source_id'],quote='observed count=6'))
    ask=AsyncMock(side_effect=answer)
    reviewed=await review.review_answer(ask=ask,question='Explain the observed results',draft='unverified draft',files=[],evidence=evidence,language='en')
    assert ask.await_count==1 and reviewed.status=='verified'
    public=reviewed.text+json.dumps(reviewed.metadata)
    assert 'END_OF_COMPLETE_METHOD' not in public and 'program_source' not in public
    assert 'executed_source_coverage' not in public and '123456789' not in public
