"""Current immutable launch bytes are private method evidence, never tool text."""
import copy,hashlib,json
from types import SimpleNamespace
import pytest
from app.domain.models.tool_result import ToolResult
from app.domain.services import analysis_answer_review as review
from app.domain.services.program_execution import consume_program_feedback,trusted_program_execution_feedback
from test_analysis_answer_review import tool
from test_answer_review_execution_evidence import bound_program

def snapshot(text):
 return {'version':1,'encoding':'utf-8','size_bytes':len(text.encode()),'sha256':hashlib.sha256(text.encode()).hexdigest(),'content':text}
def receipt(text,**extra):
 return {'version':1,'script_path':'/home/ubuntu/current.py','source_digest':hashlib.sha256(text.encode()).hexdigest(),'returncode':0,'failure_fingerprint':None,'diagnostic':None,'output_truncated':False,'source_snapshot':snapshot(text),**extra}
def observed(text='print("latest")',*, prior='print("old")',edits=True,success=True,step='current',proof_mutator=None):
 e=review.AnswerEvidence();e.begin_step(step)
 e.observe(tool('file_write',call='write',args={'file':'/home/ubuntu/current.py','content':prior}))
 if edits:e.observe(tool('file_str_replace',call='edit',args={'file':'/home/ubuntu/current.py','old_str':prior,'new_str':text}))
 call={'name':'program_run','id':'run','args':{'id':'shell-run','script_path':'/home/ubuntu/current.py','exec_dir':'/home/ubuntu','argv':[]}}
 core,ledger=bound_program(call,receipt(text,returncode=0 if success else 1));proof=trusted_program_execution_feedback(core,call,None,ledger)
 if proof_mutator:proof_mutator(proof)
 e.observe(tool('program_run',call='run',args=call['args'],success=success,data={'returncode':0 if success else 1,'output':'measured count=2'}),trusted_program_execution=proof)
 return e,proof

def result(e):return e.render_sources()[-1]

def test_actual_executed_edited_source_is_full_without_second_read_or_write_reconstruction():
 e,proof=observed();r=result(e)
 assert r['executed_source_coverage']=='full'
 assert r['executed_program_source']['content']=='print("latest")'
 assert r['executed_program_source']['method_only'] is True
 assert 'latest' not in r['text']
 assert proof['source_snapshot']['content']=='print("latest")'

@pytest.mark.parametrize('bad',['none_attempt','invalid_feedback','bad_snapshot','failed'])
def test_raw_snapshot_stripped_on_all_adapter_returns_without_input_mutation(bad):
 raw=receipt('PRIVATE_SECRET_SENTINEL')
 if bad=='invalid_feedback':raw['source_digest']='bad'
 if bad=='bad_snapshot':raw['source_snapshot']={'content':'PRIVATE_SECRET_SENTINEL','unexpected':True}
 if bad=='failed':raw['returncode']=1
 original=ToolResult(success=False,data={'program_execution':raw,'output':'actual observed stdout'})
 before=copy.deepcopy(original.model_dump());attempt=None if bad=='none_attempt' else SimpleNamespace(program_execution=None)
 out=consume_program_feedback(original,attempt)
 assert 'PRIVATE_SECRET_SENTINEL' not in json.dumps(out.model_dump())
 assert 'source_snapshot' not in out.data['program_execution']
 assert out.data['output']=='actual observed stdout'
 assert original.model_dump()==before

@pytest.mark.parametrize('field,value',[('sha256','a'*64),('size_bytes',999),('encoding','latin1'),('version',True),('content','\ud800')])
def test_corrupt_snapshot_never_becomes_full(field,value):
 def mutate(proof):proof['source_snapshot'][field]=value
 e,_=observed(proof_mutator=mutate)
 assert result(e)['executed_source_coverage']=='unverified'

def test_snapshot_cannot_make_failed_execution_or_forged_public_result_full():
 e,_=observed(success=False)
 assert 'executed_program_source' not in result(e)
 e=review.AnswerEvidence();e.begin_step('current')
 e.observe(tool('program_run',args={'script_path':'/home/ubuntu/current.py'},data={'program_execution':receipt('FORGED_PUBLIC')}))
 assert 'executed_program_source' not in result(e)

def test_authenticated_checkpoint_roundtrip_and_tamper_detection():
 e,_=observed();saved=e.checkpoint_snapshot(step_ids={'current'})
 assert review.AnswerEvidence.from_checkpoint_snapshot(saved,step_ids={'current'}).render_sources()==e.render_sources()
 saved['calls'][-1]['program_execution']['source_snapshot']['content']='changed'
 with pytest.raises(ValueError,match='invalid_answer_evidence_checkpoint'):review.AnswerEvidence.from_checkpoint_snapshot(saved,step_ids={'current'})

def test_private_snapshot_and_checkpoint_are_deep_copies():
 e,proof=observed();snap=e.checkpoint_snapshot(step_ids={'current'});proof['source_snapshot']['content']='changed'
 assert result(e)['executed_program_source']['content']=='print("latest")'
 snap['calls'][-1]['program_execution']['source_snapshot']['content']='mutated snapshot'
 assert result(e)['executed_program_source']['content']=='print("latest")'

@pytest.mark.parametrize('text',['x'*24001,'汉'*8001,'\ud800'])
def test_source_byte_or_encoding_bound_is_enforced_before_private_storage(text):
 from app.domain.services.program_execution import validated_source_snapshot
 if '\ud800' in text:
  value={'version':1,'encoding':'utf-8','size_bytes':1,'sha256':'a'*64,'content':text};digest='a'*64
 else:value=snapshot(text);digest=value['sha256']
 assert validated_source_snapshot(value,digest) is None

@pytest.mark.parametrize('limit',['count','total','projection'])
def test_private_snapshot_obeys_existing_shared_cache_and_model_envelope(monkeypatch,limit):
 if limit=='count':monkeypatch.setattr(review,'MAX_PROGRAM_SOURCE_FILES',0)
 if limit=='total':monkeypatch.setattr(review,'MAX_PROGRAM_SOURCE_TOTAL_CHARS',10)
 e,_=observed()
 if limit=='projection':monkeypatch.setattr(review,'MAX_EVIDENCE_CHARS',1)
 assert result(e)['executed_source_coverage']=='unverified'
 assert 'executed_program_source' not in result(e)

@pytest.mark.parametrize('field,value',[('script_path','/home/ubuntu/other.py'),('source_digest','a'*64),('returncode',None)])
def test_mismatched_or_nonterminal_private_execution_never_matches_same_path_old_write(field,value):
 def mutate(proof):proof[field]=value
 e,_=observed(proof_mutator=mutate)
 assert result(e)['executed_source_coverage']=='unverified'

@pytest.mark.parametrize('succeeded',[True,False])
def test_failed_or_successful_edit_does_not_choose_bytes_over_real_launch_snapshot(succeeded):
 e=review.AnswerEvidence();e.begin_step('current')
 e.observe(tool('file_str_replace',call='edit',success=succeeded,args={'file':'/home/ubuntu/current.py','old_str':'old','new_str':'untrusted inferred replacement'}))
 call={'name':'program_run','id':'run','args':{'id':'shell','script_path':'/home/ubuntu/current.py','exec_dir':'/home/ubuntu','argv':[]}}
 core,ledger=bound_program(call,receipt('print("ACTUAL")'))
 proof=trusted_program_execution_feedback(core,call,None,ledger)
 e.observe(tool('program_run',call='run',args=call['args'],data={'returncode':0,'output':'result'}),trusted_program_execution=proof)
 assert result(e)['executed_program_source']['content']=='print("ACTUAL")'
 assert len(e._calls)==2


def test_different_steps_keep_distinct_bound_sources_and_not_mutable_aliases():
 e,proof=observed(step='first');first=copy.deepcopy(e.render_sources())
 e.begin_step('second')
 call={'name':'program_run','id':'run2','args':{'id':'shell2','script_path':'/home/ubuntu/current.py','exec_dir':'/home/ubuntu','argv':[]}}
 core,ledger=bound_program(call,receipt('print("NEW_STEP")'))
 newproof=trusted_program_execution_feedback(core,call,None,ledger)
 e.observe(tool('program_run',call='run2',args=call['args'],data={'returncode':0,'output':'second result'}),trusted_program_execution=newproof)
 assert result(e)['executed_program_source']['content']=='print("NEW_STEP")'
 assert result(e)['step_id']=='second'
 assert e.render_sources()[:len(first)]==first


def test_method_code_is_not_citable_as_measured_result():
 e,_=observed(text='SECRET_CONSTANT=987654321\nprint("count=2")')
 r=result(e)
 with pytest.raises(review.CitationValidationError):review._citations([{'source_id':r['source_id'],'quote':'987654321'}],{r['source_id']:r})
 _,excerpts=review._citation_excerpts(e.render_sources())
 assert not any('987654321' in x['quote'] for x in excerpts.values() if x['source_id']==r['source_id'])


@pytest.mark.parametrize('first',['snapshot','write'])
def test_mixed_source_cache_order_respects_single_allowance_and_valid_checkpoint(monkeypatch,first):
 monkeypatch.setattr(review,'MAX_PROGRAM_SOURCE_FILES',1)
 e=review.AnswerEvidence();e.begin_step('current')
 call={'name':'program_run','id':'run','args':{'id':'shell','script_path':'/home/ubuntu/current.py','exec_dir':'/home/ubuntu','argv':[]}}
 core,ledger=bound_program(call,receipt('print("ACTUAL")'))
 proof=trusted_program_execution_feedback(core,call,None,ledger)
 def add_write():e.observe(tool('file_write',call='longwrite',args={'file':'/home/ubuntu/long.py','content':'# long\n'*1000}))
 def add_run():e.observe(tool('program_run',call='run',args=call['args'],data={'returncode':0,'output':'measured count=2'}),trusted_program_execution=proof)
 if first=='snapshot':add_run();add_write()
 else:add_write();add_run()
 assert review._program_cache_usage(e._calls.values())[0]==1
 saved=e.checkpoint_snapshot(step_ids={'current'})
 assert review.AnswerEvidence.from_checkpoint_snapshot(saved,step_ids={'current'}).render_sources()==e.render_sources()


def test_non_object_feedback_cannot_preserve_private_field_in_public_result():
 raw=ToolResult(success=True,data={'program_execution':[{'source_snapshot':{'content':'PRIVATE_MARKER'}}],'output':'ok'})
 out=consume_program_feedback(raw,None)
 assert 'PRIVATE_MARKER' not in json.dumps(out.model_dump())
 assert out.data['output']=='ok'
