import json
import pytest
from agent_monitor import news_kimi
from agent_monitor.kimi_tasks import DailyBudget


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
    for text in ['not JSON', '{}', '{"items": null}', 'x'*32001]:
        with pytest.raises(news_kimi.NewsSummaryError):news_kimi.parse_summaries(text, {'a'})


def test_length_markup_and_number_violations_reject_only_that_row():
    rows = [{'id':'long','summary':'x'*421}, {'id':'short','summary':'Too short.'},
            {'id':'link','summary':'Read more at https://example.org about this result.'},
            {'id':'tex','summary':'The authors bound $H(d)$ by a new construction here.'},
            {'id':'number','summary':'OpenAI revised 15 manuscripts in its repository.'},
            {'id':'good','summary':'OpenAI revised 14 manuscripts, the repository says.'}]
    texts = {row['id']: 'We have revised 14 other manuscripts.' for row in rows}
    edits = news_kimi.parse_summaries(json.dumps({'items': rows}), set(texts), texts)
    assert edits == [{'id':'good','summary':'OpenAI revised 14 manuscripts, the repository says.'}]
    assert news_kimi.numbers_consistent('300 of 719 (about 42%) and 1,000 cases', '300 / 719 = ~42%; 1000 cases')
    assert not news_kimi.numbers_consistent('In 2026, 3 papers', '3 papers')


@pytest.mark.parametrize('text', [
    # Character references would be decoded into a link, markup or TeX when the summary is published.
    'Researchers describe a result; details at https&colon;&sol;&sol;evil&period;example&sol;x today.',
    'A &lt;b&gt;bold&lt;&sol;b&gt; claim about the theorem.', 'The authors use &bsol;frac for the main bound.',
    'A double &amp;lt;b&amp;gt; encoded claim.', 'A legacy &lt reference without a semicolon.',
    # Full-width forms read as a link after NFKC normalization.
    'Details at ｈｔｔｐｓ：／／evil．example／x today.', 'Visit WWW.example.org for the proof.',
    'Formulas such as $x^2+y^2=z^2$ hold.', 'For every prime $p$ the bound holds.', 'The bound $H(d)$ is finite.',
    'The bound uses \\frac and other macros.', 'A [link](x) in Markdown.'])
def test_plain_text_check_rejects_links_markup_tex_and_character_references(text):
    assert not news_kimi.plain_text_ok(text)


@pytest.mark.parametrize('text', [
    'Erdős offered $100 for a proof and $250 for a disproof of the conjecture.',
    'A $5,000 prize remains unclaimed, and $1.5 million was raised.', 'Researchers at AT&T discuss R&D and Q&A sessions.',
    'The bound improves from 7/8 to 11/12, the authors say.', 'For all n < m the claim holds, the authors report.'])
def test_plain_text_check_accepts_money_ampersands_and_plain_comparisons(text):
    assert news_kimi.plain_text_ok(text)


def test_a_summary_with_two_prize_amounts_is_kept(monkeypatch):
    excerpt = 'Erdős offered $100 for a proof and $250 for a disproof.'
    summary = 'Erdős offered $100 for a proof and $250 for a disproof, the article recalls.'
    Broker(monkeypatch, lambda data: {'items': [{'id': data['articles'][0]['id'], 'summary': summary}]})
    assert news_kimi.summarize([{'id': 'a', 'title': 'Prize problem', 'excerpt': excerpt}]) == [{'id': 'a', 'summary': summary}]


class Broker:
    """Stands in for the loopback broker: records each Chat Completions call."""

    def __init__(self, monkeypatch, *replies):
        self.replies, self.calls = list(replies), []
        monkeypatch.setattr(news_kimi.client, 'issue_credentials', self.issue)
        monkeypatch.setattr(news_kimi.client, '_request', self.request)

    def issue(self, **kwargs):
        assert kwargs['engine'] == 'plain' and kwargs['client_id'] == 'ansatze-news'
        return {'KIMI_API_KEY': 'ephemeral-fixture-token'}

    def request(self, path, **kwargs):
        assert path == '/internal/v1/chat/completions'
        # A short timeout, which the broker learns so it gives up first.
        assert kwargs['timeout'] == 90 and kwargs['extra_headers'] == {'X-Ansatze-Timeout': '90'}
        payload = kwargs['payload']
        assert payload['model'] == 'kimi-k3' and payload['reasoning_effort'] == 'low' and payload['stream'] is False
        assert 'tools' not in payload and 'tool_choice' not in payload
        data = json.loads(payload['messages'][1]['content'])
        self.calls.append((data['task'], data['data'], payload))
        reply = self.replies.pop(0)
        content = reply(data['data']) if callable(reply) else reply
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(content)}}]}, ''


def test_broker_receives_one_bounded_tool_free_call_and_returns_no_credentials(monkeypatch):
    broker = Broker(monkeypatch, lambda data: {'items': [{'id': a['id'], 'summary': 'The source reports new research on this topic.'}
                                                        for a in data['articles']]})
    assert news_kimi.summarize([]) == [] and not broker.calls
    result = news_kimi.summarize([{'id': str(i), 'title': 'Announcement', 'excerpt': 'A' * 4000} for i in range(12)])
    assert len(result) == 6 and len(broker.calls) == 1
    task, data, payload = broker.calls[0]
    assert task == 'summarize' and payload['max_tokens'] == 4096
    assert len(data['articles']) == 6 and all(len(a['excerpt']) == 1800 for a in data['articles'])
    assert 'untrusted' in payload['messages'][0]['content'] and 'attribute opinions' in payload['messages'][0]['content']
    assert 'ephemeral-fixture-token' not in json.dumps(result)


def test_provider_error_details_are_not_leaked(monkeypatch):
    def fail(**kwargs):raise RuntimeError('secret-fixture-value')
    monkeypatch.setattr(news_kimi.client,'issue_credentials',fail)
    with pytest.raises(news_kimi.NewsSummaryError,match='temporarily unavailable') as error:
        news_kimi.summarize([{'id':'a'}])
    assert 'secret-fixture-value' not in str(error.value) and error.value.kind == 'unavailable'


def test_triage_is_id_bound_enumerated_and_link_free(monkeypatch):
    rows = [{'id': 'a', 'title': 'Erdős #522 resolved', 'excerpt': 'We resolve it.'}, {'id': 'b', 'title': 'Office party'}]
    Broker(monkeypatch, {'items': [
        {'id': 'a', 'decision': 'include', 'importance': 3, 'topic': 'problem-status', 'reason': 'Claims a named problem.'},
        {'id': 'b', 'decision': 'exclude', 'importance': 0, 'topic': 'other', 'reason': 'See https://evil.example'}]})
    assert news_kimi.triage(rows) == [{'id': 'a', 'decision': 'include', 'importance': 3, 'topic': 'problem-status'}]
    for bad in [{'id': 'z', 'decision': 'include', 'importance': 1, 'topic': 'other', 'reason': 'Unknown id.'},
                {'id': 'a', 'decision': 'promote', 'importance': 1, 'topic': 'other', 'reason': 'Bad enum.'},
                {'id': 'a', 'decision': 'include', 'importance': 9, 'topic': 'other', 'reason': 'Bad score.'},
                {'id': 'a', 'decision': 'include', 'importance': True, 'topic': 'other', 'reason': 'Bool score.'},
                {'id': 'a', 'decision': 'include', 'importance': 1, 'topic': 'other', 'reason': 'ok', 'title': 'Solved!'}]:
        Broker(monkeypatch, {'items': [bad]})
        with pytest.raises(news_kimi.NewsSummaryError) as error:
            news_kimi.triage(rows)
        assert error.value.kind == 'invalid'


def test_problem_links_use_supplied_candidates_and_verbatim_evidence(monkeypatch):
    text = 'We resolve Erdős Problem #522, posed in 1961, by proving that the proportion converges.'
    rows = [{'id': 'x', 'text': text, 'candidates': [{'problem_id': 'erdos-0522', 'title': 'Erdős problem #522'}]}]
    broker = Broker(monkeypatch, {'links': [
        {'id': 'x', 'problem_id': 'erdos-0522', 'relation': 'claims-resolution', 'evidence': 'We resolve Erdős Problem #522, posed in 1961'},
        {'id': 'x', 'problem_id': 'erdos-0522', 'relation': 'mentions', 'evidence': 'The problem is now solved.'}]})
    assert news_kimi.link_problems(rows) == [{'id': 'x', 'problem_id': 'erdos-0522', 'relation': 'claims-resolution',
                                              'evidence': 'We resolve Erdős Problem #522, posed in 1961'}]
    assert broker.calls[0][1]['records'][0]['candidates'] == rows[0]['candidates']
    for bad in [{'id': 'x', 'problem_id': 'erdos-0523', 'relation': 'mentions', 'evidence': 'We resolve Erdős Problem #522'},
                {'id': 'x', 'problem_id': 'erdos-0522', 'relation': 'solved', 'evidence': 'We resolve Erdős Problem #522'},
                {'id': 'y', 'problem_id': 'erdos-0522', 'relation': 'mentions', 'evidence': 'We resolve Erdős Problem #522'}]:
        Broker(monkeypatch, {'links': [bad]})
        with pytest.raises(news_kimi.NewsSummaryError):
            news_kimi.link_problems(rows)
    assert news_kimi.link_problems([{'id': 'x', 'text': text, 'candidates': []}]) == []


def test_story_groups_need_two_supplied_members_and_a_primary_or_journalism_source(monkeypatch):
    items = [{'id': 'a', 'title': 'OpenAI releases 722 manuscripts', 'role': 'primary'},
             {'id': 'b', 'title': 'Reaction to the release', 'role': 'commentary'},
             {'id': 'c', 'title': 'Another reaction', 'role': 'commentary'},
             {'id': 'd', 'title': 'Unrelated', 'role': 'preprint'},
             {'id': 'e', 'title': 'A third reaction', 'role': 'commentary'},
             {'id': 'f', 'title': 'A preprint', 'role': 'preprint'}]
    payload = {'stories': [{'story_id': 'openai-math', 'label': 'Release', 'members': []}], 'items': items}
    Broker(monkeypatch, {'assign': [{'id': 'd', 'story_id': 'openai-math'}], 'new': [
        {'key': 'n1', 'label': 'OpenAI releases 722 manuscripts', 'member_ids': ['a', 'b']},
        {'key': 'n2', 'label': 'Two reactions', 'member_ids': ['c', 'e']},
        {'key': 'n3', 'label': 'Release of 999 papers', 'member_ids': ['f']}]})
    assert news_kimi.group_stories(payload) == {'assign': [{'id': 'd', 'story_id': 'openai-math'}],
                                                'new': [{'label': 'OpenAI releases 722 manuscripts', 'member_ids': ['a', 'b']}]}
    for bad in [{'assign': [{'id': 'zz', 'story_id': 'openai-math'}], 'new': []},
                {'assign': [{'id': 'a', 'story_id': 'invented'}], 'new': []},
                {'assign': [], 'new': [{'key': 'n1', 'label': 'x', 'member_ids': ['a', 'zz']}]},
                {'assign': [], 'new': [], 'items': []}]:
        Broker(monkeypatch, bad)
        with pytest.raises(news_kimi.NewsSummaryError):
            news_kimi.group_stories(payload)


def test_changelog_summary_must_reproduce_the_source_numbers(monkeypatch):
    item = {'title': 'openai/math repository update · October 7, 2026',
            'excerpt': 'We have withdrawn three manuscripts. We have revised 14 other manuscripts. 300 / 719 = ~42%.'}
    Broker(monkeypatch, {'summary': 'The October 7, 2026 update withdrew three manuscripts and revised 14 others; 300 of 719 results are formalized.'})
    assert news_kimi.summarize_changelog(item).startswith('The October 7, 2026 update')
    for wrong in ['The update withdrew three manuscripts and revised 15 others in total.', 'Too short.',
                  'The update revised 14 manuscripts; see https://github.com/openai/math for more.']:
        Broker(monkeypatch, {'summary': wrong})
        with pytest.raises(news_kimi.NewsSummaryError):
            news_kimi.summarize_changelog(item)


def test_budget_caps_calls_per_run_and_per_day_and_persists(monkeypatch):
    state = {}
    budget = news_kimi.NewsBudget(state, 3, 2, today='2026-10-11')
    broker = Broker(monkeypatch, *[{'items': []}] * 4)
    news_kimi.triage([{'id': 'a', 'title': 't'}], budget=budget)
    news_kimi.triage([{'id': 'a', 'title': 't'}], budget=budget)
    with pytest.raises(news_kimi.NewsSummaryError) as error:
        news_kimi.triage([{'id': 'a', 'title': 't'}], budget=budget)
    assert error.value.kind == 'budget' and len(broker.calls) == 2 and state == {'day': '2026-10-11', 'calls': 2}
    next_run = news_kimi.NewsBudget(state, 3, 4, today='2026-10-11')
    assert next_run.take() and not next_run.take() and state['calls'] == 3
    assert news_kimi.NewsBudget(state, 3, 4, today='2026-10-12').take() and state == {'day': '2026-10-12', 'calls': 1}
    assert isinstance(next_run.daily, DailyBudget)


@pytest.mark.parametrize(('value', 'cap'), [(None, 60), ('', 60), ('0', 0), ('12', 12), ('9999', 500), ('-4', 0), ('many', 60)])
def test_daily_cap_comes_from_the_environment(value, cap):
    env = {} if value is None else {'ANSATZE_NEWS_MODEL_CALLS_PER_DAY': value}
    assert news_kimi.daily_cap(env) == cap


def test_bound_tasks_share_one_budget(monkeypatch):
    budget = news_kimi.NewsBudget({}, 60, 1, today='2026-10-11')
    tasks = news_kimi.bound_tasks(budget)
    assert set(tasks) == {'summarizer', 'triager', 'linker', 'grouper', 'changelog_summarizer'}
    Broker(monkeypatch, {'items': []})
    assert tasks['triager']([{'id': 'a', 'title': 't'}]) == []
    with pytest.raises(news_kimi.NewsSummaryError) as error:
        tasks['summarizer']([{'id': 'a', 'title': 't'}])
    assert error.value.kind == 'budget'


def test_a_summary_about_the_supplied_text_is_dropped_for_that_row_only():
    rows = [{'id': 'meta', 'summary': 'Google DeepMind announced Gemini 3.7 Flash. The supplied excerpt contains no details.'},
            {'id': 'good', 'summary': 'Google DeepMind announced Gemini 3.7 Flash, a new model in its Gemini line.'}]
    texts = {row['id']: 'Introducing Gemini 3.7 Flash' for row in rows}
    assert news_kimi.parse_summaries(json.dumps({'items': rows}), set(texts), texts) == [rows[1]]
    assert 'never mention the excerpt' in news_kimi.SUMMARY_SYSTEM


class FakeTime:
    """Stands in for ``kimi_k3.time`` only: a clock that only its sleeps advance."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_a_refusal_kimi_never_processed_is_sent_again_within_the_run_budget(monkeypatch):
    clock, sent = FakeTime(), []
    monkeypatch.setattr(news_kimi.kimi_tasks.kimi_k3, 'time', clock)
    monkeypatch.setattr(news_kimi.client, 'issue_credentials', lambda **kwargs: {'KIMI_API_KEY': 'ephemeral-fixture-token'})
    refusal = news_kimi.client.SponsoredKimiError(
        'Sponsored Kimi request failed: Kimi request was rejected', status=503,
        error={'message': 'Kimi request was rejected', 'type': 'upstream_rejected', 'forwarded': True,
               'upstream_status': 503, 'upstream_elapsed_ms': 126, 'retry_after': None})

    def request(path, **kwargs):
        sent.append((kwargs['timeout'], kwargs['extra_headers']['X-Ansatze-Timeout']))
        if len(sent) % 2:
            raise refusal
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'items': []})}}]}, ''

    monkeypatch.setattr(news_kimi.client, '_request', request)
    budget = news_kimi.NewsBudget({}, 60, 2, today='2026-11-11')
    assert news_kimi.triage([{'id': 'a', 'title': 't'}], budget=budget) == []
    assert clock.sleeps == [5.0] and sent == [(90, '90'), (85, '85')] and budget.run_calls == 2
    # With no call left in the run, the refusal is reported and the stage backs off instead.
    sent.clear()
    with pytest.raises(news_kimi.NewsSummaryError) as error:
        news_kimi.triage([{'id': 'a', 'title': 't'}], budget=news_kimi.NewsBudget({}, 60, 1, today='2026-11-11'))
    assert error.value.kind == 'unavailable' and len(sent) == 1 and clock.sleeps == [5.0]
