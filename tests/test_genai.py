from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.genai import TOOLS, build_instructions, is_local_model, local_runtime_status, ollama_base_url, reasoning_effort, run_agent


def test_tool_schemas_are_strict_and_privacy_context_is_limited():
    assert TOOLS
    assert all(tool['strict'] is True for tool in TOOLS)
    assert all(tool['parameters']['additionalProperties'] is False for tool in TOOLS)
    instructions = build_instructions(
        {'business_type': 'Clinic', 'gstin': 'sensitive', 'current_tools': 'Mobile hotspot'}, 'en'
    )
    assert 'Clinic' in instructions
    assert 'Mobile hotspot' in instructions
    assert 'sensitive' not in instructions


def test_ollama_url_is_local_only(monkeypatch):
    monkeypatch.setenv('OLLAMA_BASE_URL', 'http://localhost:11434/v1')
    assert ollama_base_url() == 'http://localhost:11434'
    monkeypatch.setenv('OLLAMA_BASE_URL', 'https://example.com/v1')
    assert ollama_base_url() is None
    assert not is_local_model('qwen3:8b-cloud')
    assert is_local_model('qwen3:8b')


def test_local_runtime_status_detects_model(monkeypatch):
    monkeypatch.setenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
    monkeypatch.setenv('OLLAMA_MODEL', 'qwen3:8b')
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'models': [{'name': 'qwen3:8b'}]}
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['trust_env'] is False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr('app.genai.httpx.Client', Client)
    assert local_runtime_status() == {'status': 'ready', 'model': 'qwen3:8b'}


def test_reasoning_effort_is_only_sent_to_models_that_support_it(monkeypatch):
    monkeypatch.setenv('OLLAMA_MODEL', 'llama3.1:8b')
    assert reasoning_effort() is None
    monkeypatch.setenv('OLLAMA_MODEL', 'qwen3:8b')
    monkeypatch.delenv('OLLAMA_REASONING_EFFORT', raising=False)
    assert reasoning_effort() == 'none'
    monkeypatch.setenv('OLLAMA_REASONING_EFFORT', 'high')
    assert reasoning_effort() == 'high'


def test_responses_function_call_cycle_returns_evidence(monkeypatch):
    monkeypatch.setenv('OLLAMA_MODEL', 'llama3.1:8b')
    call = SimpleNamespace(
        id='call_1',
        function=SimpleNamespace(
            name='search_catalogue',
            arguments='{"query":"business fibre","category":"Connectivity","segment":null,"max_results":3}',
        ),
    )
    first = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))], usage=None
    )
    second = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='The catalogue has three matching options.', tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=8, completion_tokens=12),
    )

    class Completions:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return [first, second][len(self.calls) - 1]

    completions = Completions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    def run_immediate(coroutine):
        """Drive this fake client's no-I/O coroutine without opening an event loop."""
        try:
            coroutine.send(None)
        except StopIteration as completed:
            return completed.value
        raise AssertionError('Fake Responses client unexpectedly awaited external I/O')

    result = run_immediate(run_agent(
        client=client, profile={'business_type': 'Clinic'}, language='en', history=[],
        user_text='Find business fibre',
        tool_executor=lambda name, args: {'summary': 'Found one source-backed match', 'products': [{'product_id': 'P01'}]},
    ))

    assert result['message'].startswith('The catalogue')
    assert result['tool_trace'][0]['tool'] == 'search_catalogue'
    assert result['artifacts'][0]['data']['products'][0]['product_id'] == 'P01'
    assert any(item.get('role') == 'tool' for item in completions.calls[1]['messages'] if isinstance(item, dict))
    assert completions.calls[0]['tool_choice'] == 'auto'
    assert 'reasoning_effort' not in completions.calls[0]
    assert completions.calls[0]['tools'][0]['function']['name'] == 'propose_profile_update'
    assert result['usage'] == {'input_tokens': 8, 'output_tokens': 12}


def test_chat_redacts_direct_identifiers_and_profile_proposal_is_constrained():
    raw = 'Call +91 9876543210, email owner@example.in, GSTIN 32TTNXB7384T1ZW and UDYAM-KL-00-3078801'
    cleaned = main.redact_chat(raw)
    assert '+91 9876543210' not in cleaned
    assert 'owner@example.in' not in cleaned
    assert '32TTNXB7384T1ZW' not in cleaned
    assert 'UDYAM-KL-00-3078801' not in cleaned

    proposed = main.propose_profile(
        {'needs': ['internet'], 'gstin': 'must-not-change', 'confirmed': True},
        {'business_type': 'Clinic', 'employees': 6, 'sites': 2, 'budget': 4200,
         'city_tier': 2, 'needs': ['email'], 'language': 'en',
         'current_tools': 'owner@example.in'},
    )
    assert proposed['updates']['employees'] == 6
    assert proposed['profile']['needs'] == ['email', 'internet']
    assert proposed['profile']['current_tools'] == '[email removed]'
    assert 'gstin' not in proposed['profile']
    assert proposed['profile']['confirmed'] is False
    assert 'owner@example.in' not in proposed['updates']['current_tools']
    assert proposed['updates']['current_tools'] in proposed['profile']['current_tools']


def test_profile_proposal_ignores_invalid_or_ambiguous_values():
    proposed = main.propose_profile(
        {'needs': ['internet'], 'confirmed': True},
        {'employees': True, 'sites': 0, 'budget': -1, 'city_tier': True,
         'needs': [{'unexpected': 'value'}, 'not-a-priority'], 'language': 'xx'},
    )

    assert proposed['updates'] == {}
    assert proposed['profile']['confirmed'] is False
    assert proposed['profile']['needs'] == ['internet']


def test_profile_proposal_only_applies_facts_grounded_in_the_latest_message():
    proposed = main.propose_profile(
        {'needs': [], 'confirmed': False, 'city_tier': None, 'language': 'hi'},
        {'business_type': 'Clinic', 'employees': 12, 'sites': 2, 'budget': 4200,
         'current_tools': 'Mobile hotspot', 'city_tier': 2, 'language': 'en',
         'needs': ['internet', 'email', 'backup']},
        source_text='I run a dental clinic with 12 employees at 2 sites. We use a mobile hotspot. Our monthly budget is ₹4,200 and we need internet, email, and backup.',
    )

    assert proposed['updates']['business_type'] == 'Clinic'
    assert proposed['updates']['employees'] == 12
    assert proposed['updates']['sites'] == 2
    assert proposed['updates']['budget'] == 4200
    assert proposed['updates']['current_tools'] == 'Mobile hotspot'
    assert proposed['updates']['needs'] == ['backup', 'email', 'internet']
    assert 'city_tier' not in proposed['updates']
    assert 'language' not in proposed['updates']


def test_hindi_facts_are_grounded_and_must_keep_requires_explicit_language():
    source = main.redact_chat('मेरे क्लिनिक में ६ कर्मचारी हैं, इंटरनेट और ईमेल चाहिए, बजट 4500')
    args = {'business_type': 'Clinic', 'employees': 6, 'budget': 4500,
            'needs': ['internet', 'email'], 'must': ['email']}
    proposed = main.propose_profile({'needs': [], 'confirmed': False}, args, source_text=source)
    assert proposed['updates']['business_type'] == 'Clinic'
    assert proposed['updates']['employees'] == 6
    assert proposed['updates']['budget'] == 4500
    assert set(proposed['updates']['needs']) == {'internet', 'email'}
    assert 'must' not in proposed['updates']

    explicit = main.redact_chat('इंटरनेट अनिवार्य है और ईमेल चाहिए')
    args['must'] = ['internet']
    proposed = main.propose_profile({'needs': [], 'confirmed': False}, args, source_text=explicit)
    assert proposed['updates']['must'] == ['internet']


def test_common_current_tools_fill_the_profile_only_when_stated_as_in_use():
    profile = {'needs': [], 'confirmed': False}
    proposed = main.propose_profile(profile, {}, source_text='We currently use a mobile hotspot and personal email.')
    assert proposed['updates']['current_tools'] == 'Mobile hotspot, Personal email'

    proposed = main.propose_profile(profile, {}, source_text='We do not use a mobile hotspot; we use broadband instead.')
    assert proposed['updates']['current_tools'] == 'Broadband'


def test_local_genai_tools_use_catalogue_and_aggregate_only():
    matches = main.catalogue_search('business fibre', 'Connectivity', None, 3)
    assert matches['products']
    assert matches['products'][0]['product_id'].startswith('P')
    assert matches['source'] == 'app/data/product_catalogue.csv'

    from app.genai import TOOLS
    assert 'analyze_funnel' not in {tool['name'] for tool in TOOLS}


def test_advisor_explains_when_local_ollama_is_unavailable(monkeypatch):
    monkeypatch.setattr(main, 'local_runtime_status', lambda: {'status': 'runtime_unavailable', 'model': 'qwen3:8b'})
    journey_id = main.newjourney(main.CreateJourney(language='en'))['id']
    import asyncio
    result=asyncio.run(main.advisor(journey_id,main.TextInput(text='I run a clinic')))
    assert result['mode']=='guided'
    assert result['updates']['business_type'].lower()=='clinic'
