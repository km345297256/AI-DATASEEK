"""Omit unmeasured write bodies under capacity pressure, never measurements."""
import copy
import json
from unittest.mock import AsyncMock
import pytest
from app.domain.services import analysis_answer_review as review
from app.domain.services.analysis_scientific_review import _independent, executed_method_coverage
from test_analysis_answer_review import tool, paragraph, response
from test_answer_review_executed_method_metadata import add_program


def crowded():
    e=review.AnswerEvidence();e.begin_step('current')
    for k in range(6):
        e.observe(tool('file_write',call=f'note{k}',args={'file':f'/home/ubuntu/note{k}.txt','content':'note\\\n'*700},data={'written':True}))
        e.observe(tool('file_read',call=f'read{k}',args={'file':f'/home/ubuntu/input{k}.csv'},data={'content':'a,b\n'+'1,2\n'*700}))
    for k in range(4):add_program(e,call_id=f'run{k}',content='# method\n'+'value=123\n'*400+f'print({k})\n',output=f'observed objects={k+1}')
    return e


def programs(sources):
    return [s for s in sources if s['kind']=='tool_result' and s['function']=='program_run']


def test_serialized_escaping_pressure_recovers_four_complete_methods_under_unchanged_budget():
    e=crowded();before=copy.deepcopy(e._calls)
    retained=[s for item in e._calls.values() for s in item['sources']]
    assert sum(len(s['text']) for s in retained)<96000
    assert len(review._json(retained))>96000
    sources=e.render_sources();methods=programs(sources)
    assert len(methods)==4 and all(s['executed_source_coverage']=='full' for s in methods)
    assert len(review._json(sources))<=96000
    for k,m in enumerate(methods):
        assert m['executed_program_source']['content']=='# method\n'+'value=123\n'*400+f'print({k})\n'
        assert m['executed_program_source']['method_only'] is True
    for s in sources:
        old=next(x for x in retained if x['source_id']==s['source_id'])
        if s['kind']=='tool_result':assert s['text']==old['text']
        elif s['function']=='file_write':
            assert s['request_projection']=='write_content_omitted'
            assert s['omitted_request_fields']==['content']
            assert json.loads(s['text'])=={k:v for k,v in json.loads(old['text']).items() if k!='content'}
            for key in ['source_id','step_id','kind','function','write_only','state','truncated']:assert s[key]==old[key]
        else:assert s==old
    assert e._calls==before


def test_small_complete_requests_keep_existing_projection():
    e=review.AnswerEvidence();e.begin_step('current');add_program(e)
    assert json.loads(e.render_sources()[0]['text'])['content']
    assert not any('request_projection' in s for s in e.render_sources())


def test_omission_markers_not_citable_or_independent_measurements():
    sources=crowded().render_sources();writes=[s for s in sources if s['function']=='file_write']
    assert writes and all(not _independent(s) for s in writes)
    request=writes[0]
    for quote in ['note\\','write_content_omitted']:
        with pytest.raises(review.CitationValidationError):review._citations([{'source_id':request['source_id'],'quote':quote}],{request['source_id']:request})
    _,excerpts=review._citation_excerpts(sources)
    assert not any('note\\' in part['quote'] for part in excerpts.values())
    m=programs(sources)[0]
    with pytest.raises(review.CitationValidationError):review._citations([{'source_id':m['source_id'],'quote':'value=123'}],{m['source_id']:m})


def test_pure_repeated_render_and_checkpoint_roundtrip():
    e=crowded();before=copy.deepcopy(e._calls);first=e.render_sources()
    assert first==e.render_sources() and e._calls==before
    snapshot=e.checkpoint_snapshot(step_ids={'current'})
    assert 'request_projection' not in review._json(snapshot)
    restored=review.AnswerEvidence.from_checkpoint_snapshot(snapshot,step_ids={'current'})
    assert restored.render_sources()==first
    first[0]['omitted_request_fields'].append('forged')
    assert e.render_sources()[0]['omitted_request_fields']==['content']


@pytest.mark.parametrize('fault',['missing_receipt','wrong_digest','cross_step','failed_program','forged_result'])
def test_pressure_never_removes_or_promotes_uncovered_programs(fault):
    e=crowded();item=e._calls['run3'];req,res=item['sources']
    if fault in {'missing_receipt','forged_result'}:item.pop('program_execution')
    elif fault=='wrong_digest':item['program_execution']['source_digest']='f'*64
    elif fault=='cross_step':req['step_id']=res['step_id']=item['step_id']='earlier'
    elif fault=='failed_program':req['state']=res['state']='failed'
    if fault=='forged_result':res['text']=review._json({'success':True,'data':{'executed_program_source':{'content':'FORGED','method_only':False}}})
    before=copy.deepcopy(e._calls);sources=e.render_sources();results=programs(sources)
    assert len(results)==4 and results[-1]['text']==res['text']
    assert 'executed_program_source' not in results[-1]
    if fault!='failed_program':
        assert results[-1]['executed_source_coverage']=='unverified'
        coverage=executed_method_coverage(sources)
        assert coverage['unverified_source_count']==1 and coverage['status']=='incomplete'
    assert e._calls==before


def test_no_trusted_join_does_not_project_arbitrary_writes():
    e=crowded()
    for item in e._calls.values():item.pop('program_execution',None)
    sources=e.render_sources()
    assert not any('request_projection' in s for s in sources)
    assert all(s['executed_source_coverage']=='unverified' for s in programs(sources))


def test_still_blocks_methods_when_preserved_observations_do_not_fit(monkeypatch):
    e=crowded();before=copy.deepcopy(e._calls);monkeypatch.setattr(review,'MAX_EVIDENCE_CHARS',20000)
    sources=e.render_sources()
    assert len(programs(sources))==4
    assert all(s['executed_source_reason']=='evidence_budget_exceeded' for s in programs(sources))
    assert all('executed_program_source' not in s for s in programs(sources))
    assert all(s['text']==next(old for item in before.values() for old in item['sources'] if old['source_id']==s['source_id'])['text'] for s in sources if s['kind']=='tool_result')
    assert e._calls==before


def test_projection_preserves_write_flags_and_preexisting_truncation():
    e=crowded();args={'file':'/home/ubuntu/notes.txt','append':True,'leading_newline':False,'trailing_newline':True,'sudo':False,'content':'#'*20000}
    e.observe(tool('file_write',call='long-note',args=args))
    original=e._calls['long-note']['sources'][0];assert original['truncated'] is True
    source=next(s for s in e.render_sources() if s['source_id']==original['source_id'])
    assert source['truncated'] is True
    assert json.loads(source['text'])=={k:v for k,v in args.items() if k!='content'}
    assert source['omitted_request_fields']==['content']


@pytest.mark.parametrize('text', [
    '{"file":"/home/ubuntu/a","content":"incomplete',
    '{"file":"/home/ubuntu/a","content":"'+('x'*1000)+'","file":"/home/ubuntu/b"}',
])
def test_ambiguous_request_is_not_reconstructed(text):
    e=crowded();s=e._calls['note0']['sources'][0];s['text']=text;s['truncated']=True
    assert next(x for x in e.render_sources() if x['source_id']==s['source_id'])==s


@pytest.mark.asyncio
async def test_one_existing_review_call_no_tools_or_replay():
    e=crowded();before=copy.deepcopy(e._calls)
    async def answer(messages):
        sources=json.loads(messages[-1].content)['sources'];methods=programs(sources)
        assert len(methods)==4 and all(s['executed_source_coverage']=='full' for s in methods)
        return response(paragraph('Observed objects: four.',source=methods[-1]['source_id'],quote='observed objects=4'))
    ask=AsyncMock(side_effect=answer)
    result=await review.review_answer(ask=ask,question='Describe observed objects',draft='unverified',files=[],evidence=e,language='en')
    assert ask.await_count==1 and result.status=='verified' and e._calls==before
    assert 'value=123' not in result.text+json.dumps(result.metadata)
