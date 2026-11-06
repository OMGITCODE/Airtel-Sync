from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.genai import TOOLS, build_instructions, is_local_model, local_runtime_status, ollama_base_url, run_agent


def test_tool_schemas_are_strict_and_privacy_context_is_limited():
    assert TOOLS
    assert all(tool['strict'] is True for tool in TOOLS)
    assert all(tool['parameters']['additionalProperties'] is False for tool in TOOLS)
    instructions = build_instructions(
        {'business_type': 'Clinic', 'gstin': 'sensitive', 'current_tools': 'private email'}, 'en'
    )
    assert 'Clinic' in instructions
    assert 'sensitive' not in instructions
    assert 'private email' not in instructions


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


def test_responses_function_call_cycle_returns_evidence():
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
    assert 'gstin' not in proposed['profile']
    assert proposed['profile']['confirmed'] is False
    assert 'owner@example.in' not in proposed['updates']['current_tools']


def test_local_genai_tools_use_catalogue_and_aggregate_only():
    matches = main.catalogue_search('business fibre', 'Connectivity', None, 3)
    assert matches['products']
    assert matches['products'][0]['product_id'].startswith('P')
    assert matches['source'] == 'app/data/product_catalogue.csv'

    report = main.analyze_leads('Professional offices')
    assert report['lead_count'] > 0
    assert 'activation_pct' in report
    assert 'stage_counts' in report
    assert 'drop_reason' not in report
    assert 'business_name' not in report
    assert 'synthetic' in report['source']


def test_advisor_explains_when_local_ollama_is_unavailable(monkeypatch):
    monkeypatch.setattr(main, 'local_runtime_status', lambda: {'status': 'runtime_unavailable', 'model': 'qwen3:8b'})
    journey_id = main.newjourney(main.CreateJourney(language='en'))['id']
    coroutine = main.advisor(journey_id, main.TextInput(text='I run a clinic'))
    with pytest.raises(HTTPException) as caught:
        coroutine.send(None)
    assert caught.value.status_code == 503
    assert 'Start the Ollama app' in caught.value.detail
