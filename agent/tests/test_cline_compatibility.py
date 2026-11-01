"""Cline data-envelope compatibility on actual OpenAI SDK request paths."""
from __future__ import annotations

import json
import asyncio
import httpx
import pytest
from src.providers.llm import ChatOpenAIWithReasoning

BASE = 'https://api.cline.bot/api/v1'
MODEL = 'cline-pass/deepseek-v4.1-flash'


def completion():
    return {
        'id':'test', 'object':'chat.completion', 'created':0, 'model':MODEL,
        'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':'ok','reasoning':'fixture reasoning'}}],
        'usage':{'prompt_tokens':2,'completion_tokens':3,'total_tokens':5},
    }


@pytest.mark.parametrize('envelope', [False, True])
def test_invoke_preserves_text_reasoning_usage_and_effort(envelope):
    def handler(request):
        payload = json.loads(request.content)
        assert payload['model'] == MODEL
        assert payload['reasoning_effort'] == 'xhigh'
        body = completion()
        return httpx.Response(200,json={'data':body} if envelope else body)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        llm = ChatOpenAIWithReasoning(model=MODEL,api_key='sk-test',base_url=BASE,
            http_client=client,vibe_provider='deepseek',reasoning_effort='xhigh',use_responses_api=False)
        result = llm.invoke('hello')
    assert result.content == 'ok'
    assert result.additional_kwargs['reasoning_content'] == 'fixture reasoning'
    assert result.usage_metadata['total_tokens'] == 5


def test_async_invoke_unwraps_completion():
    async def run():
        async def handler(request):
            return httpx.Response(200,json={'data':completion()})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            llm = ChatOpenAIWithReasoning(model=MODEL,api_key='sk-test',base_url=BASE,
                http_async_client=client,vibe_provider='deepseek',use_responses_api=False)
            result = await llm.ainvoke('hello')
        assert result.content == 'ok'
        assert result.additional_kwargs['reasoning_content'] == 'fixture reasoning'
    asyncio.run(run())


@pytest.mark.parametrize('envelope', [False, True])
def test_stream_unwraps_text_reasoning_and_tools(envelope):
    deltas = [
        {'role':'assistant','reasoning':'fixture reasoning'},
        {'content':'ok'},
        {'tool_calls':[{'index':0,'id':'call-test','type':'function','function':{'name':'lookup_price','arguments':'{"symbol":"TSM"}'}}]},
    ]
    events=[]
    for delta in deltas:
        body={'id':'test','object':'chat.completion.chunk','created':0,'model':MODEL,
            'choices':[{'index':0,'delta':delta,'finish_reason':None}]}
        events.append({'data':body} if envelope else body)
    def handler(request):
        content=''.join('data: '+json.dumps(event)+'\n\n' for event in events)+'data: [DONE]\n\n'
        return httpx.Response(200,text=content,headers={'content-type':'text/event-stream'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        llm=ChatOpenAIWithReasoning(model=MODEL,api_key='sk-test',base_url=BASE,
            http_client=client,vibe_provider='deepseek',use_responses_api=False)
        chunks=list(llm.stream('hello'))
    assert ''.join(c.content for c in chunks) == 'ok'
    assert any(c.additional_kwargs.get('reasoning_content') == 'fixture reasoning' for c in chunks)
    assert any(c.tool_calls and c.tool_calls[0]['name'] == 'lookup_price' for c in chunks)


def test_envelope_handling_is_scoped_to_cline():
    llm=ChatOpenAIWithReasoning(model='deepseek-flash',api_key='sk-test',
        base_url='https://api.deepseek.com/v1',vibe_provider='deepseek')
    body={'data':completion()}
    assert llm._unwrap_cline_response(body) is body
