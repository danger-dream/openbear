import pytest

from app.llm.anthropic import AnthropicBackend
from app.llm.base import AgentResult, OpenBearLLMError, aggregate
from app.llm.events import StreamEvent
from app.llm.retry import RetryPolicy
from app.runtime.model_call import ModelCallDriver, execute_attempt
from tests.test_protocol_audit_regressions import backend, frame, request
from tests.test_runtime_model_call import FakeComplete, FakeStream, req

EXPLANATION = "This request triggered restrictions on violative cyber content and was blocked under Anthropic's Usage Policy. To learn more, see https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback."
DETAILS = {'type': 'refusal', 'category': 'cyber', 'explanation': EXPLANATION}


def refusal_backend(stream, details=DETAILS):
    obj = {'stop_reason': 'refusal', 'stop_details': details, 'content': [], 'usage': {'input_tokens': 7, 'output_tokens': 11}}
    events = [frame('message_start', message={'usage': {'input_tokens': 7}}),
              frame('message_delta', delta={'stop_reason': 'refusal', 'stop_details': details}, usage={'output_tokens': 11}),
              frame('message_stop')]
    return backend(AnthropicBackend, events if stream else None, obj=obj)


@pytest.mark.parametrize('stream', [True, False])
async def test_wire_refusal_details_reach_user_and_settle_without_retry(stream):
    b = refusal_backend(stream)
    settled, retries = [], []
    prepared = request(b)
    prepared.metadata['mode'] = 'stream' if stream else 'complete'
    with pytest.raises(OpenBearLLMError) as caught:
        await ModelCallDriver(RetryPolicy(max_retries=3, base_delay_s=0)).call(
            lambda tail: prepared, settled.append, on_retry=retries.append)
    error = caught.value
    assert '网络安全（cyber）' in error.user_message()
    assert EXPLANATION in error.user_message()
    assert error.reason == 'refusal' and not error.retryable
    assert retries == [] and len(settled) == 1
    assert settled[0].response.finish_details == DETAILS
    assert settled[0].response.usage.input_tokens == 7
    assert settled[0].response.usage.output_tokens == 11
    assert error.public_detail()['details']['stopDetails']['explanation'] == EXPLANATION


async def test_stream_aggregate_preserves_stop_details():
    b = refusal_backend(True)
    result = await aggregate(b.stream([], model='fixture'))
    assert result.finish_details == DETAILS


@pytest.mark.parametrize('details', [None, [], 'bad', {}, {'category': 1, 'explanation': []}])
@pytest.mark.parametrize('stream', [True, False])
async def test_absent_or_malformed_wire_details_keep_existing_refusal_message(details, stream):
    b = refusal_backend(stream, details)
    result = await execute_attempt(request(b), mode='stream' if stream else 'complete')
    assert result.error.reason == 'refusal'
    assert result.error.user_message() == '上游拒绝生成（refusal），未返回正文，本轮未完成。未自动重试。'


@pytest.mark.parametrize('stream', [True, False])
async def test_unknown_category_explanation_is_bounded_redacted_and_partial_text_kept(stream):
    details = {'category': 'future-policy', 'explanation': 'api_key=sk-secretsecret ' + 'explanation ' * 300,
               'thinking': 'must never be shown'}
    result = AgentResult(text='partial answer', finish_reason='refusal', finish_details=details)
    b = FakeStream([[StreamEvent('content', text=result.text), StreamEvent('finish', finish_reason='refusal', details={'stopDetails': details})]]) if stream else FakeComplete(result)
    outcome = await execute_attempt(req(b), mode='stream' if stream else 'complete')
    message = outcome.error.user_message()
    assert 'future-policy' in message and '网络安全' not in message
    assert 'partial answer' == outcome.response.text
    assert '已保留返回的正文' in message
    assert 'sk-secretsecret' not in message and 'must never be shown' not in message
    assert len(message) <= 1201
    assert outcome.error.details['stopDetails'].keys() == {'category', 'explanation'}
