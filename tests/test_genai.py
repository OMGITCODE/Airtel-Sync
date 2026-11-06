from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.genai import TOOLS, api_key_configured, build_instructions, run_agent


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


def test_example_placeholder_is_not_treated_as_a_live_api_key(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'replace_with_your_own_key')
    assert not api_key_configured()


def test_responses_function_call_cycle_returns_evidence():
    call = SimpleNamespace(
        type='function_call', name='search_catalogue',
        arguments='{"query":"business fibre","category":"Connectivity","segment":null,"max_results":3}',
        call_id='call_1',
    )
    first = SimpleNamespace(output=[call], output_text='')
    second = SimpleNamespace(output=[], output_text='The catalogue has three matching options.')

    class Responses:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return [first, second][len(self.calls) - 1]

    responses = Responses()
    client = SimpleNamespace(responses=responses)
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
    assert any(item.get('type') == 'function_call_output' for item in responses.calls[1]['input'] if isinstance(item, dict))
    assert responses.calls[0]['store'] is False
    assert responses.calls[0]['tool_choice'] == 'auto'


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


def test_advisor_clearly_requires_live_key(monkeypatch):
    monkeypatch.setattr(main, 'create_client', lambda: None)
    journey_id = main.newjourney(main.CreateJourney(language='en'))['id']
    coroutine = main.advisor(journey_id, main.TextInput(text='I run a clinic'))
    with pytest.raises(HTTPException) as caught:
        coroutine.send(None)
    assert caught.value.status_code == 503
    assert 'OPENAI_API_KEY' in caught.value.detail
