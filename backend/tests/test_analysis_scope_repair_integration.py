"""Real reviewer and runner gates; no external model, files, or execution."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.analysis_outcome import AnalysisOutcome, DeliverableRequirement
from app.domain.models.dataset import MountedDataset
from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.message import Message
from app.domain.models.plan import Step
from app.domain.models.tool_result import ToolResult
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.analysis_answer_review import AnswerEvidence, review_answer
from app.domain.services.analysis_budget import BudgetSnapshot
from app.domain.services.analysis_scope_repair_audit import ScopeRepairAuditStore
from app.domain.services.analysis_scientific_review import SCIENTIFIC_DIMENSIONS
from app.domain.services.analysis_text_scientific_review import answer_scope_review_metadata, answer_scope_shape_error
from app.domain.services.execution_evidence import ToolExecutionLedger
from app.domain.services.model_runtime import analysis_budget_scope
from app.domain.services.tools.file import FileToolkit

PATH = '/home/ubuntu/datasets/current/observations.csv'
REQUEST = 'Compare the supplied x and y observations, including sensitivity.'
CONTENT = 'x,y\n1,2\n2,4\n'
TEXT = 'The file contains x and y columns.'


async def reviewed(blocker='none', scope_status='incomplete', *, final_repair=False):
    evidence=AnswerEvidence();evidence.begin_step('analysis')
    evidence.observe(ToolEvent(tool_call_id='read',tool_name='file',function_name='file_read',
        function_args={'file':PATH},status=ToolStatus.CALLED,function_result=ToolResult(success=True,data={'content':CONTENT})))
    final_calls = 0
    async def ask(messages):
        nonlocal final_calls
        payload=json.loads(messages[-1].content)
        source=next(s for s in payload['sources'] if s.get('function')=='file_read' and s.get('kind')=='tool_result')
        scope={'status':scope_status,'paragraph_indices':[0]}
        if blocker is not None:scope['completion_blocker']=blocker
        if 'frozen_paragraphs' in payload:
            final_calls += 1
            indices = [99] if final_repair and final_calls == 1 else payload['paragraph_indices']
            return json.dumps({'answer_scientific_checks':[{'dimension':d,'status':'unclear',
                'paragraph_indices':indices,'evidence':[]} for d in SCIENTIFIC_DIMENSIONS],
                'answer_scope_check':scope})
        return json.dumps({'unsupported_claims':False,'paragraphs':[{'text':TEXT,'kind':'analysis',
            'evidence':[{'source_id':source['source_id'],'quote':'x,y'}]}], 'requirement_checks':[]})
    callback=AsyncMock(side_effect=ask)
    result=await review_answer(ask=callback,question=REQUEST,draft=TEXT,files=[],evidence=evidence,answer_scientific_scope=True)
    assert callback.await_count==(3 if final_repair else 2)
    return result


def fixture():
    runner=object.__new__(AgentTaskRunner)
    runner._user_id='owner';runner._session_id='session'
    record={'path':PATH,'size':len(CONTENT),'sha256':'a'*64}
    runner._analysis_source_fingerprints=[record]
    runner._sandbox=SimpleNamespace(analysis_fingerprints=AsyncMock(return_value=ToolResult(success=True,
        data={'version':1,'errors':[],'files':[record]})))
    toolkit=FileToolkit(runner._sandbox)
    tools={tool.name:tool for tool in toolkit.get_tools()}
    ledger=ToolExecutionLedger()
    executor=SimpleNamespace(_tool_execution_ledger=ledger,get_tool=tools.get)
    step=Step(id='analysis',description='Analyze current observations',inputs={'dataset_intent':'analysis'},result=TEXT,success=True)
    runner._flow=SimpleNamespace(plan=SimpleNamespace(steps=[step]),executor=executor,_domain_agents={},_artifact_repair_requests={})
    runner._front_controller_resolution=SimpleNamespace(mode='sandbox',decision=SimpleNamespace(safety=SimpleNamespace(allowed=True)))
    runner._analysis_scope_observations={}
    runner._analysis_scope_trackers={}
    runner._scope_repair_audit_store=SimpleNamespace(claim=AsyncMock(return_value=True))
    runner._input_delivery=SimpleNamespace(_require_live=AsyncMock())
    runner._accepted_input_key='current-lease'
    message=Message(message=REQUEST,datasets=[MountedDataset(dataset_id='current',data_center_id='dc',data_center_name='DC',name='Current',sandbox_path='/home/ubuntu/datasets/current')])
    message._budget_lineage_id='lineage'
    budget=SimpleNamespace(snapshot=AsyncMock(return_value=BudgetSnapshot('lineage',1,None,None,0,1,100,None,None,None)))
    execution={'code':'completed','has_unconfirmed_tool_execution':False,'side_effect_state':'confirmed_terminal','execution_evidence':ledger.summary()}
    outcome=AnalysisOutcome(status='failed',reason_code='scientific_validation_unavailable')
    event=ToolEvent(tool_call_id='read',tool_name='file',function_name='file_read',function_args={'file':PATH},
        status=ToolStatus.CALLED,function_result=ToolResult(success=True,data={'content':CONTENT}))
    runner._observe_scope_repair_tool(event,step.id)
    return runner,step,message,budget,execution,outcome,event


async def attempt(ctx,result,**changes):
    runner,step,message,budget,execution,outcome,_=ctx
    with analysis_budget_scope(budget):
        return await runner._review_scope_completion(step,message,changes.get('files',[]),changes.get('requirements',[]),
            outcome,execution,result,source_seq=7)


@pytest.mark.asyncio
async def test_real_review_retains_notice_free_candidate_and_one_original_input_queue():
    result=await reviewed()
    assert result.scope_completion_candidate==(TEXT,)
    assert result.scope_completion_request==REQUEST
    assert result.text!=TEXT and result.status=='unavailable'
    assert 'scope_completion_candidate' not in result.metadata
    ctx=fixture();runner,step,*_=ctx
    assert await attempt(ctx,result)
    assert runner._flow._artifact_repair_requests[step.id]['schema']=='answer_scope_completion/v1'
    assert runner._flow._artifact_repair_requests[step.id]['previous_analysis']==TEXT
    runner._flow._artifact_repair_requests.clear() # queue consumed; compaction cannot reset opportunity
    assert not await attempt(ctx,result)
    step.id='renamed'
    runner._analysis_scope_observations['renamed']={'reads':{PATH},'blocked':False}
    assert not await attempt(ctx,result)
    assert runner._scope_repair_audit_store.claim.await_count==1


@pytest.mark.asyncio
async def test_final_protocol_recovery_cannot_authorize_analytical_reexecution():
    result = await reviewed(final_repair=True)
    assert result.metadata['answer_scope_review']['status'] == 'incomplete'
    assert result.metadata['final_candidate_review']['schema_recovered'] is True
    assert result.scope_completion_candidate == (TEXT,)
    ctx = fixture()
    assert not await attempt(ctx, result)
    ctx[0]._scope_repair_audit_store.claim.assert_not_awaited()
    assert not ctx[0]._flow._artifact_repair_requests


@pytest.mark.asyncio
@pytest.mark.parametrize('blocker',[None,'unknown','missing_input','user_input_required','permission','unsupported'])
async def test_legacy_and_blocked_scope_do_not_request_more_work(blocker):
    result=await reviewed(blocker=blocker)
    assert result.metadata['answer_scope_review']['completion_blocker']==(blocker or 'unknown')
    ctx=fixture()
    assert not await attempt(ctx,result)
    ctx[0]._scope_repair_audit_store.claim.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('key',['answer_scientific_review','scientific_review','report_review'])
async def test_rejection_anywhere_cannot_hide_behind_scope_reason(key):
    result=await reviewed();metadata=deepcopy(result.metadata)
    metadata['reason']='answer_coverage_incomplete'
    metadata[key]={'status':'unavailable','children':[{'status':'rejected'}]}
    assert not await attempt(fixture(),replace(result,metadata=metadata))


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['candidate','request','scope','science_only','schema_repair','citation_repair','final_schema_repair','final_error','final_missing','final_bool_attempts','files','requirements','resume','denied','not_started','unknown','lease','deadline','input_version','no_read','side_effect'])
async def test_actual_runner_does_not_turn_missing_or_uncertain_evidence_into_permission(change):
    result=await reviewed();ctx=fixture();runner,step,message,budget,execution,_,event=ctx
    kwargs={}
    if change=='candidate':result=replace(result,scope_completion_candidate=('Changed text',))
    elif change=='request':result=replace(result,scope_completion_request='Other request')
    elif change=='scope':result.metadata['answer_scope_review']['candidate_version_status']='changed'
    elif change=='science_only':result.metadata['answer_scope_review']['status']='unavailable'
    elif change=='schema_repair':result.metadata['review_schema_repair_attempted']=True
    elif change=='citation_repair':result.metadata['citation_repair_attempted']=True
    elif change=='final_schema_repair':result.metadata['final_candidate_review'].update(protocol_attempts=2,schema_recovered=True)
    elif change=='final_error':result.metadata['final_candidate_review']['error']='review_answer_science_indices'
    elif change=='final_missing':result.metadata.pop('final_candidate_review')
    elif change=='final_bool_attempts':result.metadata['final_candidate_review']['protocol_attempts']=True
    elif change=='files':kwargs['files']=[object()]
    elif change=='requirements':kwargs['requirements']=[DeliverableRequirement(kind='table')]
    elif change=='resume':message._resume_checkpoint={'version':1}
    elif change=='denied':runner._front_controller_resolution.mode='reject'
    elif change=='not_started':execution['code']='execution_failed'
    elif change=='unknown':execution['has_unconfirmed_tool_execution']=True
    elif change=='lease':runner._input_delivery._require_live.side_effect=RuntimeError('not live')
    elif change=='deadline':budget.snapshot.return_value=replace(budget.snapshot.return_value,deadline_at=datetime.now(timezone.utc)-timedelta(seconds=1))
    elif change=='input_version':runner._sandbox.analysis_fingerprints.return_value=ToolResult(success=True,data={'version':1,'errors':[],'files':[{'path':PATH,'size':len(CONTENT),'sha256':'b'*64}]})
    elif change=='no_read':runner._analysis_scope_observations={}
    else:runner._analysis_scope_observations[step.id]['blocked']=True
    assert not await attempt(ctx,result,**kwargs)
    assert not runner._flow._artifact_repair_requests


@pytest.mark.asyncio
async def test_schema_rejection_before_dispatch_is_not_unknown_or_an_executed_shell():
    ctx=fixture();runner,step,_,_,execution,_,event=ctx
    ledger=runner._flow.executor._tool_execution_ledger
    ledger.record_no_effect('invalid',state='not_started')
    failed=event.model_copy(update={'tool_call_id':'invalid','function_name':'shell_run','tool_name':'shell',
        'function_result':ToolResult(success=False,message='invalid tool arguments')})
    runner._observe_scope_repair_tool(failed,step.id)
    execution['execution_evidence']=ledger.summary()
    assert await attempt(ctx,await reviewed())
    other=fixture();failed=failed.model_copy(update={'tool_call_id':'unproven'})
    other[0]._observe_scope_repair_tool(failed,other[1].id)
    assert not await attempt(other,await reviewed())


@pytest.mark.asyncio
async def test_queue_after_cancellation_or_audit_ambiguity_cannot_be_retried():
    result=await reviewed();ctx=fixture();runner=ctx[0]
    runner._scope_repair_audit_store.claim.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):await attempt(ctx,result)
    assert not runner._flow._artifact_repair_requests
    runner._scope_repair_audit_store.claim.side_effect=None
    assert not await attempt(ctx,result)
    other=fixture();other[0]._scope_repair_audit_store.claim.return_value=False
    assert not await attempt(other,result)
    assert not await attempt(other,result)
    assert other[0]._scope_repair_audit_store.claim.await_count==1


@pytest.mark.asyncio
async def test_review_metadata_is_frozen_before_runtime_await_and_never_copied_to_feedback():
    result=await reviewed();ctx=fixture();budget=ctx[3]
    expected=result.metadata['answer_scope_review']['candidate_sha256']
    snapshot=budget.snapshot.return_value
    async def mutate():
        result.metadata['answer_scope_review']['candidate_sha256']='b'*64
        result.metadata['answer_scope_review']['untrusted_extra']='PRIVATE_SENTINEL'
        return snapshot
    budget.snapshot.side_effect=mutate
    assert await attempt(ctx,result)
    feedback=ctx[0]._flow._artifact_repair_requests[ctx[1].id]
    assert feedback['candidate_sha256']==expected
    assert 'PRIVATE_SENTINEL' not in str(feedback)


@pytest.mark.asyncio
async def test_late_original_request_change_consumes_but_never_enqueues():
    result=await reviewed();ctx=fixture();runner,_,message,*_=ctx
    async def changed(**kwargs):
        message.message='A different current request'
        return True
    runner._scope_repair_audit_store.claim.side_effect=changed
    assert not await attempt(ctx,result)
    assert runner._analysis_scope_trackers[7].snapshot()['consumed'] is True
    assert not runner._flow._artifact_repair_requests


@pytest.mark.asyncio
async def test_durable_once_identity_is_original_input_not_candidate_or_step_and_holds_no_text():
    from app.domain.services.analysis_scope_repair import ScopeRepairTracker
    records={}
    async def insert(record):
        if record['_id'] in records:raise RuntimeError('already inserted')
        records[record['_id']]=record
        return SimpleNamespace(acknowledged=True)
    store=ScopeRepairAuditStore(SimpleNamespace(insert_one=AsyncMock(side_effect=insert)))
    for index,step in enumerate(['first','renamed']):
        tracker=ScopeRepairTracker(input_seq=7,step_id=step,request=REQUEST)
        snap=tracker.snapshot()|{'consumed':True}
        assert await store.claim(user_id='owner',session_id='session',snapshot=snap) is (index==0)
    assert len(records)==1 and REQUEST not in str(records) and TEXT not in str(records)


@pytest.mark.parametrize('value',[[],{},False,'invented','/private/example'])
def test_blocker_is_a_fixed_optional_enum_not_a_new_instruction_channel(value):
    check={'status':'incomplete','paragraph_indices':[0],'completion_blocker':value}
    assert answer_scope_shape_error(check,paragraph_count=1)=='review_answer_scope_fields'
    metadata=answer_scope_review_metadata(check,request=REQUEST,paragraphs=[TEXT])
    assert metadata['status']=='unavailable' and metadata['completion_blocker']=='unknown'


@pytest.mark.asyncio
async def test_real_flow_drains_then_completes_once_with_same_runtime_and_no_new_files():
    from unittest.mock import Mock
    from app.domain.models.event import MessageEvent, DoneEvent
    from app.domain.services.model_runtime import current_analysis_budget
    from test_analysis_repair_flow import scenario
    runner,flow,step,message,state=scenario([[],[]],[])
    template=fixture(); source_runner,_,source_message,budget,execution,_,read_event=template
    message.message=REQUEST;message.datasets=source_message.datasets
    message._budget_lineage_id='lineage'
    step.inputs.update(artifact_policy='optional',execution_mode='dataset_fast_path')
    flow._dataset_fast_path_active=True
    runner._sandbox.analysis_fingerprints=source_runner._sandbox.analysis_fingerprints
    runner._scope_repair_audit_store=source_runner._scope_repair_audit_store
    runner._delivery_audit_store=SimpleNamespace(record=AsyncMock())
    runner._handle_tool_event=AsyncMock();runner._remember_private_tool_output=Mock()
    # Manifest discovery is host-side; this fixture has one authorized source.
    from app.domain.models.dataset import DatasetFile
    message.datasets[0].files=[DatasetFile(path='observations.csv',size=len(CONTENT))]
    agent=flow.executor
    agent._tool_execution_ledger=ToolExecutionLedger()
    toolkit=FileToolkit(runner._sandbox)
    agent.toolkits=[toolkit]
    original=agent._execute_with_tool_scope
    observed_budgets=[]
    async def execute(prompt,**kwargs):
        observed_budgets.append(current_analysis_budget())
        async for event in original(prompt,**kwargs):
            if isinstance(event,MessageEvent):
                agent.last_execution_outcome=deepcopy(execution)
                yield read_event.model_copy(update={'tool_call_id':f'read-{len(observed_budgets)}'})
            yield event
    agent._execute_with_tool_scope=execute
    async def real_review(**_kwargs):
        return await reviewed()
    agent.review_delivery_answer=AsyncMock(side_effect=real_review)
    events=[]
    with analysis_budget_scope(budget):
        async for event in runner._run_flow(message,trigger_event_seq=7):
            events.append(event.model_copy(deep=True))
    assert len(state['prompts'])==2 and state['drained']==2
    assert observed_budgets==[budget,budget]
    assert 'host_scope_completion_feedback' in state['prompts'][1]
    assert 'This does not require a program' in state['prompts'][1]
    assert message._artifact_repair_context is None and not message.deliverables
    assert runner._scope_repair_audit_store.claim.await_count==1
    assert sum(isinstance(e,DoneEvent) for e in events)==1
    assert not any(e.attachments for e in events if isinstance(e,MessageEvent))
    public=json.dumps([e.model_dump(mode='json') for e in events])
    assert 'host_scope_completion_feedback' not in public and 'previous_analysis' not in public
