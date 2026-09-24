"""Native BaseAgent dispatch must not persist host memory receipts as source."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage
from langchain.tools import tool

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.base import BaseToolkit
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.shell import ShellToolkit
from test_tool_response_protocol_recovery import make_agent

PATH = '/home/ubuntu/output/report.md'
MARKER = '[content persisted successfully; 67 bytes; sha256:0123456789abcdef]'
CHANGED = '[content persisted successfully; 9999 bytes; sha256:fedcba9876543210]'
REPLACEMENT = '[replacement text persisted; 67 bytes; sha256:0123456789abcdef]'


def write(identifier, content=MARKER, **kwargs):
    return AIMessage(content='', tool_calls=[{'id': identifier, 'name': 'file_write',
        'args': {'file': PATH, 'content': content, 'append': True, **kwargs}}])


def listing(identifier):
    return AIMessage(content='', tool_calls=[{'id': identifier, 'name': 'shell_run',
        'args': {'id': identifier, 'exec_dir': '/home/ubuntu/output', 'command': 'ls -la report.md'}}])


def agent_and_sandbox(monkeypatch, responses):
    agent, _, requests, bindings = make_agent(monkeypatch, responses)
    sandbox = SimpleNamespace(id='receipt-fixture',
        file_write=AsyncMock(return_value=ToolResult(success=True, data={'content': 'actual body'})),
        file_replace=AsyncMock(return_value=ToolResult(success=True, data={'replaced_count': 1})),
        exec_command=AsyncMock(return_value=ToolResult(success=True, data={
            'status': 'completed', 'returncode': 0, 'output': 'report.md 100 bytes'})),
        wait_for_process=AsyncMock())
    agent.toolkits=[FileToolkit(sandbox), ShellToolkit(sandbox)]
    return agent,sandbox,requests,bindings


def blocked(events):
    return [e for e in events if isinstance(e, ToolEvent) and e.status==ToolStatus.CALLED
            and isinstance(e.function_result,dict)
            and e.function_result.get('blocked_by_policy')=='compacted_content_not_source']


@pytest.mark.asyncio
async def test_changed_receipt_nonce_with_successful_ls_cannot_keep_native_loop_alive(monkeypatch):
    agent,sandbox,requests,bindings=agent_and_sandbox(monkeypatch,
        [write('bad1'),listing('ls'),write('bad2',CHANGED),AIMessage(content='Incomplete: actual content is missing.')])
    events=[e async for e in agent.execute('Complete the original analysis report')]
    sandbox.file_write.assert_not_awaited()
    assert sandbox.exec_command.await_count==1
    assert len(blocked(events))==2 and agent._analysis_progress.should_stop
    assert agent.last_execution_outcome['code']=='analysis_no_progress_loop'
    assert len(requests)==4 and bindings[-1]['tools']==[]
    assert all(e.function_result['success'] is False for e in blocked(events))


@pytest.mark.asyncio
async def test_one_real_body_correction_continues_without_new_request_or_replay(monkeypatch):
    fixed='Measured values and their verified limitations.'
    corrected=write('actual',fixed); original_args=copy.deepcopy(corrected.tool_calls[0]['args'])
    agent,sandbox,requests,_=agent_and_sandbox(monkeypatch,
        [write('bad'),corrected,AIMessage(content='Verified body saved.')])
    events=[e async for e in agent.execute('Complete the original analysis report')]
    assert sandbox.file_write.await_count==1
    assert sandbox.file_write.await_args.kwargs['content']==fixed
    assert len(blocked(events))==1 and agent.last_execution_outcome['code']=='completed'
    # Historical memory compaction must not alter the emitted original args.
    recorded=next(e for e in events if isinstance(e,ToolEvent) and e.tool_call_id=='actual' and e.status==ToolStatus.CALLED)
    assert recorded.function_args==original_args
    assert corrected.tool_calls[0]['args']['content']!=fixed
    assert len(requests)==3


@pytest.mark.asyncio
@pytest.mark.parametrize('body',[MARKER,REPLACEMENT,' \n'+MARKER+'\t\n'+REPLACEMENT+' '])
async def test_real_core_replace_refuses_receipt_only_new_content(monkeypatch,body):
    call=AIMessage(content='',tool_calls=[{'id':'replace','name':'file_str_replace',
        'args':{'file':PATH,'old_str':'old text','new_str':body}}])
    agent,sandbox,_,_=agent_and_sandbox(monkeypatch,[call,AIMessage(content='Actual text still needed.')])
    events=[e async for e in agent.execute('Update the report')]
    sandbox.file_replace.assert_not_awaited();assert len(blocked(events))==1


@pytest.mark.asyncio
async def test_cancellation_after_one_block_propagates_without_write(monkeypatch):
    agent,sandbox,requests,_=agent_and_sandbox(monkeypatch,[write('bad'),asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        _=[e async for e in agent.execute('Complete the report')]
    sandbox.file_write.assert_not_awaited();assert len(requests)==2
    assert agent.last_execution_outcome['code']=='cancelled'


class SameNamePlugin(BaseToolkit):
    @tool
    async def file_write(self,file:str,content:str,append:bool=True)->ToolResult:
        """Record arbitrary literal text using a non-core plugin capability."""
        return ToolResult(success=True,data={'received':content})


@pytest.mark.asyncio
async def test_same_named_plugin_is_not_reclassified_as_core_file_tool(monkeypatch):
    agent,_,requests,_=agent_and_sandbox(monkeypatch,[write('plugin'),AIMessage(content='Done.')])
    agent.toolkits=[SameNamePlugin()]
    events=[e async for e in agent.execute('Use plugin data operation')]
    assert not blocked(events) and len(requests)==2
    assert agent.last_execution_outcome['code']=='completed'


@pytest.mark.asyncio
async def test_prior_unknown_effect_stays_unknown_and_receipt_guard_does_not_replay(monkeypatch):
    ordinary=write('uncertain','Actual data')
    first=AIMessage(content='',tool_calls=[ordinary.tool_calls[0],write('receipt').tool_calls[0]])
    agent,sandbox,requests,_=agent_and_sandbox(monkeypatch,[first,AIMessage(content='Prior write could not be confirmed.')])
    sandbox.file_write.side_effect=TimeoutError('synthetic post-dispatch response loss')
    events=[e async for e in agent.execute('Complete the report')]
    assert sandbox.file_write.await_count==1 and len(blocked(events))==1
    assert agent._tool_execution_ledger.summary()['has_unresolvable_pending'] is True
    assert agent.last_execution_outcome['code']=='tool_execution_unknown'
    assert len(requests)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('body',[
    'Verified report\n'+MARKER,
    'Example: '+MARKER,
    '```text\n'+MARKER+'\n```',
    '"'+MARKER+'"',
    '[content persisted successfully; not-a-number bytes; sha256:0123456789abcdef]',
    '[content persisted successfully; 67 bytes; sha256:0123456789abcde]',
    '[content persisted successfully; 67 bytes; sha256:0123456789ABCDEF]',
    '[content persisted successfully; 067 bytes; sha256:0123456789abcdef]',
    '[content persisted successfully; -1 bytes; sha256:0123456789abcdef]',
    '[ordinary log summary]', '',
])
async def test_embedded_examples_and_nonreceipt_text_keep_original_dispatch(monkeypatch,body):
    agent,sandbox,_,_=agent_and_sandbox(monkeypatch,[write('actual',body),AIMessage(content='Done.')])
    events=[e async for e in agent.execute('Write the actual report')]
    sandbox.file_write.assert_awaited_once()
    assert sandbox.file_write.await_args.kwargs['content']==body and not blocked(events)


@pytest.mark.asyncio
async def test_existing_bad_marker_can_be_removed_using_literal_old_str(monkeypatch):
    call=AIMessage(content='',tool_calls=[{'id':'remove','name':'file_str_replace',
        'args':{'file':PATH,'old_str':MARKER,'new_str':''}}])
    agent,sandbox,_,_=agent_and_sandbox(monkeypatch,[call,AIMessage(content='Removed the invalid marker.')])
    events=[e async for e in agent.execute('Remove the previously corrupted marker from the report')]
    sandbox.file_replace.assert_awaited_once()
    assert sandbox.file_replace.await_args.kwargs['old_str']==MARKER
    assert sandbox.file_replace.await_args.kwargs['new_str']=='' and not blocked(events)


def test_only_registered_exact_core_method_gets_the_guard():
    from app.domain.services.compacted_content_dispatch import compacted_content_write_reason
    class SubclassedFileToolkit(FileToolkit):
        pass
    sandbox=SimpleNamespace(id='synthetic')
    call=write('bad').tool_calls[0]
    real=FileToolkit(sandbox)
    assert compacted_content_write_reason(real.get_tool('file_write'),call)
    assert compacted_content_write_reason(SubclassedFileToolkit(sandbox).get_tool('file_write'),call) is None
    assert compacted_content_write_reason(SameNamePlugin().get_tool('file_write'),call) is None
    spoof=SimpleNamespace(toolkit=real,_tool=FileToolkit.file_write)
    assert compacted_content_write_reason(spoof,call) is None
    assert compacted_content_write_reason(real.get_tool('file_read'),call) is None


@pytest.mark.asyncio
async def test_reject_message_is_fixed_not_a_channel_for_receipt_values(monkeypatch):
    agent,sandbox,requests,_=agent_and_sandbox(monkeypatch,[write('bad',CHANGED),AIMessage(content='Need actual content.')])
    events=[e async for e in agent.execute('Complete the report')]
    fault=blocked(events)[0].function_result
    assert 'NOT executed' in fault['message'] and 'actual complete text' in fault['message']
    assert CHANGED not in fault['message'] and 'fedcba9876543210' not in fault['message']
    assert fault['success'] is False and sandbox.file_write.await_count==0
