import json
import pytest
from agent_monitor import news_kimi


def test_summaries_bind_to_original_ids_and_cannot_change_status_or_sources():
    for bad in [{'id':'invented','summary':'A sufficiently long made-up summary.'},
                {'id':'a','summary':'A sufficiently long summary.','status':'Solved'},
                {'id':'a','summary':'A sufficiently long summary.','url':'https://evil.example'}]:
        with pytest.raises(news_kimi.NewsSummaryError):
            news_kimi.parse_summaries(json.dumps({'items':[bad]}), {'a'})
    row={'id':'a','summary':'The authors announce a claimed proof requiring review.'}
    with pytest.raises(news_kimi.NewsSummaryError):
        news_kimi.parse_summaries(json.dumps({'items':[row,row]}), {'a','b'})


def test_fenced_json_is_allowed_but_incomplete_or_oversized_output_is_not():
    row={'id':'a','summary':'The authors announce a claimed proof requiring review.'}
    assert news_kimi.parse_summaries('```json\n'+json.dumps({'items':[row]})+'\n```',{'a'}) == [row]
    for text in ['not JSON', '{}', '{"items": null}', 'x'*32001,
                 json.dumps({'items':[{'id':'a','summary':'x'*421}]})]:
        with pytest.raises(news_kimi.NewsSummaryError):news_kimi.parse_summaries(text, {'a'})


def test_broker_receives_one_bounded_tool_free_call_and_returns_no_credentials(monkeypatch):
    seen=[]
    def issue(**kwargs):
        assert kwargs['engine']=='plain' and kwargs['client_id']=='ansatze-news'
        return {'KIMI_API_KEY':'ephemeral-fixture-token'}
    def request(path, **kwargs):
        seen.append((path,kwargs))
        assert path=='/internal/v1/chat/completions'
        p=kwargs['payload']
        assert p['model']=='kimi-k3' and p['max_tokens']==4096
        assert 'tools' not in p and p['stream'] is False
        articles=json.loads(p['messages'][1]['content'])['articles']
        assert len(articles)==8 and all(len(a['excerpt'])==1800 for a in articles)
        rows=[{'id':a['id'],'summary':'The source reports new research on this topic.'} for a in articles]
        return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'items':rows})}}]},''
    monkeypatch.setattr(news_kimi.client,'issue_credentials',issue)
    monkeypatch.setattr(news_kimi.client,'_request',request)
    assert news_kimi.summarize([])==[] and not seen
    result=news_kimi.summarize([{'id':str(i),'title':'Announcement','excerpt':'A'*4000} for i in range(12)])
    assert len(result)==8 and len(seen)==1
    assert 'ephemeral-fixture-token' not in json.dumps(result)


def test_provider_error_details_are_not_leaked(monkeypatch):
    def fail(**kwargs):raise RuntimeError('secret-fixture-value')
    monkeypatch.setattr(news_kimi.client,'issue_credentials',fail)
    with pytest.raises(news_kimi.NewsSummaryError,match='temporarily unavailable') as error:
        news_kimi.summarize([{'id':'a'}])
    assert 'secret-fixture-value' not in str(error.value)
