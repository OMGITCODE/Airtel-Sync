import os
from pathlib import Path
import shutil
from tempfile import gettempdir
from datetime import datetime, timedelta, timezone
import pytest
os.environ['ONEJOURNEY_DB']=str(Path(gettempdir())/'onejourney_pytest.sqlite3')
try: Path(os.environ['ONEJOURNEY_DB']).unlink()
except FileNotFoundError: pass
from fastapi.testclient import TestClient
from app.main import app, CAT, GST, UDYAM, check_bundle, calculate, safe_profile, mask_kyc_value, con, prune_expired_journeys
client=TestClient(app)
KYC_FIXTURES=Path(__file__).parents[1]/'app'/'data'/'kyc_documents'

def new():
    d=client.post('/api/journeys',json={'language':'hi'});assert d.status_code==200;return d.json()['id']

def profile(jid, **kw):
    v={'business_type':'Clinic','employees':6,'sites':1,'city_tier':2,'segment':'prof','budget':4500,'gstin':'','confirmed':True,'needs':['internet']}
    v.update(kw)
    r=client.patch(f'/api/journeys/{jid}/profile',json={'profile':v})
    assert r.status_code==200, r.text
    return r.json()

def quote(jid,items):
    r=client.post('/api/quotes',json={'journey_id':jid,'items':items})
    assert r.status_code==200, r.text
    return r.json()

def test_health():
    assert client.get('/api/health').json()['products']==25
    metrics=client.get('/api/metrics').json()
    assert metrics['activated']==368
    assert metrics['avg_acquisition_cost_inr']==15053
    assert metrics['churn_pct']==15.2
    assert client.get('/api/health').json()['retention_days']==7

def test_partner_handoff_propensity_reports_heldout_auc_without_contact_details():
    result=client.get('/api/partners/propensity').json()
    assert result['synthetic'] is True
    assert result['rows']==5000 and result['training_rows']+result['heldout_rows']==5000
    assert result['heldout_rows']==1000 and 0<=result['heldout_auc']<=1
    assert result['positive_label']=='Activated'
    assert result['prospects'] and len(result['prospects'])<=8
    assert all({'lead_id','lead_source','business_type','city','stage_reached','propensity_score'}<=set(row) for row in result['prospects'])
    assert all('contact_name' not in row and 'gstin' not in row for row in result['prospects'])

def test_delete_my_data_removes_journey_and_linked_records():
    jid=new()
    with con() as db:
        db.execute('INSERT INTO advisor_messages(journey_id,role,content,created_at) VALUES(?,?,?,?)',(jid,'user','synthetic test',datetime.now(timezone.utc).isoformat()))
        db.execute('INSERT INTO consents(id,journey_id,purpose,version,created_at) VALUES(?,?,?,?,?)',('consent_test',jid,'test','1',datetime.now(timezone.utc).isoformat()))
    deleted=client.delete(f'/api/journeys/{jid}/data')
    assert deleted.status_code==200 and deleted.json()['deleted'] is True
    assert client.get(f'/api/journeys/{jid}').status_code==404
    with con() as db:
        assert db.execute('SELECT 1 FROM advisor_messages WHERE journey_id=?',(jid,)).fetchone() is None
        assert db.execute('SELECT 1 FROM consents WHERE journey_id=?',(jid,)).fetchone() is None
    assert client.delete(f'/api/journeys/{jid}/data').status_code==404

def test_journeys_expire_after_seven_days():
    jid=new()
    expired_at=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
    with con() as db:db.execute('UPDATE journeys SET created_at=? WHERE id=?',(expired_at,jid))
    assert prune_expired_journeys()==1
    assert client.get(f'/api/journeys/{jid}').status_code==404

def test_profile_rejects_invalid_values_instead_of_coercing_them():
    jid=new(); profile(jid)
    invalid_profiles=[
        {'employees':0},
        {'employees':True},
        {'sites':101},
        {'city_tier':3},
        {'budget':-1},
        {'budget':1.5},
        {'needs':['internet','made-up-service']},
        {'gstin':'not-a-gstin'},
    ]
    valid_gstin=next(iter(GST))
    invalid_profiles.append({'gstin':valid_gstin[:-1]+('0' if valid_gstin[-1]!='0' else '1')})
    for update in invalid_profiles:
        response=client.patch(f'/api/journeys/{jid}/profile',json={'profile':update})
        assert response.status_code==422, (update,response.text)

def test_profile_capture_returns_all_discovery_fields_without_registry_ids():
    profile={'name':'Balaji Dental Care','business_type':'Clinic','employees':6,'sites':2,
             'city_tier':1,'budget':4200,'needs':['internet','backup'],'language':'en',
             'current_tools':'Mobile hotspot','gstin':'','udyam_number':'','confirmed':False}
    safe=safe_profile(profile)
    assert safe['business_type']=='Clinic'
    assert safe['employees']==6 and safe['sites']==2 and safe['city_tier']==1
    assert safe['budget']==4200 and safe['needs']==['internet','backup']
    assert safe['current_tools']=='Mobile hotspot'
    assert safe['name']=='Balaji Dental Care'
    assert 'gstin' not in safe and 'udyam_number' not in safe

def test_new_journey_does_not_prefill_unstated_business_facts():
    response=client.post('/api/journeys',json={'language':'en'})
    assert response.status_code==200
    jid=response.json()['id'];initial=response.json()['profile']
    assert initial['business_type']==''
    assert initial['employees'] is None and initial['sites'] is None
    assert initial['city_tier'] is None and initial['budget'] is None
    assert initial['needs']==[] and initial['current_tools']==''

    captured=client.patch(f'/api/journeys/{jid}/profile',json={'profile':{'business_type':'Clinic','employees':12}})
    assert captured.status_code==200,captured.text
    assert captured.json()['profile']['sites'] is None
    assert captured.json()['profile']['city_tier'] is None
    assert client.patch(f'/api/journeys/{jid}/profile',json={'profile':{'confirmed':True}}).status_code==422

def test_dependency_rejection():
    p={'employees':6,'sites':1,'city_tier':2,'gstin':''}
    assert any(e['code']=='DEPENDENCY' for e in check_bundle(p,[{'product_id':'P09','quantity':1},{'product_id':'P04','quantity':1}]))
    assert not check_bundle(p,[{'product_id':'P09','quantity':1},{'product_id':'P01','quantity':1}])

def test_gstin_and_tier():
    p={'employees':6,'sites':1,'city_tier':2,'gstin':''}
    issues=check_bundle(p,[{'product_id':'P03','quantity':1}])
    assert {e['code'] for e in issues}>={'ACTIVE_GSTIN','CITY_TIER'}

def test_quote_arithmetic_and_version():
    jid=new();profile(jid)
    x=quote(jid,[{'product_id':'P01','quantity':1}])
    assert x['valid'] and x['recurring_paise']==109900
    assert client.post('/api/quotes/'+x['quote_id']+'/accept').status_code==200
    y=quote(jid,[{'product_id':'P04','quantity':1}]);assert y['valid'] and y['version']==2
    assert client.post('/api/quotes/'+x['quote_id']+'/accept').status_code==409

def test_profile_change_invalidates_accepted_quote():
    gst=next(g for g in GST.values() if g['status']=='Active')
    jid=new();profile(jid,gstin=gst['gstin'],city_tier=1)
    x=quote(jid,[{'product_id':'P03','quantity':1}]);assert x['valid']
    client.post('/api/quotes/'+x['quote_id']+'/accept')
    r=profile(jid,gstin='',city_tier=2)
    assert len(r['invalidated'])>0
    assert client.get('/api/journeys/'+jid).json()['quotes'][0]['accepted']==0

def test_budget_recovery_creates_a_valid_customer_chosen_quote_and_receipt():
    gst=next(g for g in GST.values() if g['status']=='Active')
    needs=['internet','backup','calling','email']
    jid=new();profile(jid,gstin=gst['gstin'],needs=needs,must=['email'])
    options=client.get(f'/api/journeys/{jid}/recommendations').json()['options']
    assert options and all(option['valid'] for option in options)
    original=quote(jid,options[0]['items'])
    assert original['recurring_paise']>360000
    assert client.post('/api/quotes/'+original['quote_id']+'/accept').status_code==200

    changed=profile(jid,gstin=gst['gstin'],budget=3600,needs=needs,must=['email'])
    assert any(issue['code']=='BUDGET' for issue in changed['invalidated'])
    assert changed['recovery_options']
    option=changed['recovery_options'][0]
    assert option['within_budget'] and option['drop']==['P12']
    assert option['unmet_needs']==['calling']
    assert client.get(f'/api/journeys/{jid}').json()['quotes'][0]['accepted']==0

    recovered=client.post(f'/api/journeys/{jid}/recover',json={'drop':option['drop']})
    assert recovered.status_code==200,recovered.text
    result=recovered.json()
    assert result['version']==2 and result['valid'] and result['recurring_paise']<=360000
    assert result['receipt']['requires_new_acceptance'] is True
    assert result['receipt']['unmet_needs']==['calling']
    assert 'P16' in result['receipt']['preserved']
    saved=client.get(f'/api/journeys/{jid}').json()['quotes'][0]
    assert saved['receipt']['new_version']==2
    assert client.post('/api/quotes/'+original['quote_id']+'/accept').status_code==409
    assert client.post('/api/quotes/'+result['quote_id']+'/accept').status_code==200

def test_customer_priorities_and_budget_tradeoffs_are_disclosed():
    jid=new();profile(jid,gstin='',needs=['internet','calling'])
    options=client.get(f'/api/journeys/{jid}/recommendations').json()['options']
    assert options
    first=options[0]
    calling=next((need for need in first['unmet_needs'] if need['need']=='calling'),None)
    assert calling and calling['code']=='ACTIVE_GSTIN' and calling['unlock_with_gstin']
    assert 'Not included' in first['tradeoff']
    assert all(any(item['product_id'] in {'P01','P02','P04'} for item in option['items']) for option in options)

def test_require_all_kyc_mode_names_each_missing_document():
    jid=new()
    response=client.post('/api/kyc/verify',json={'journey_id':jid,'require_all_documents':True})
    assert response.status_code==200,response.text
    assert response.json()['status']!='VERIFIED'
    assert all(any(doc_type.replace('_',' ') in reason for reason in response.json()['reasons'])
               for doc_type in ('gst_certificate','udyam_certificate','signatory_id','address_proof'))

def test_kyc_identifier_display_masks_registration_numbers():
    gst=next(iter(GST))
    record=next(iter(UDYAM))
    assert mask_kyc_value(gst)==gst[:2]+'*'*9+gst[-4:]
    assert mask_kyc_value(record)=='UDYAM-**-**-*******'

def test_order_gates_and_idempotency():
    jid=new();profile(jid)
    q=quote(jid,[{'product_id':'P01','quantity':1}]);client.post('/api/quotes/'+q['quote_id']+'/accept')
    body={'journey_id':jid,'quote_id':q['quote_id'],'payment_confirmed':True}
    assert client.post('/api/orders',json=body).status_code==409
    assert client.post('/api/handoffs',json={'journey_id':jid,'reason':'Need help','permission':False}).status_code==403

@pytest.mark.skipif(shutil.which('tesseract') is None, reason='Tesseract OCR executable is not installed on this machine')
def test_ocr_and_kyc_real_document():
    gst=next(g for g in GST.values() if g['status']=='Active')
    ud=next((u for u in UDYAM.values() if u['enterprise_name'].casefold()==gst['legal_name'].casefold()),None)
    assert ud
    jid=new();profile(jid,gstin=gst['gstin'])
    q=quote(jid,[{'product_id':'P01','quantity':1}]);assert q['valid']
    assert client.post('/api/quotes/'+q['quote_id']+'/accept').status_code==200
    assert client.post('/api/kyc/documents',data={'journey_id':jid},files={'file':('a.png',b'not png','image/png')}).status_code==403
    r=client.post('/api/kyc/consent',data={'journey_id':jid,'accepted':'true'});assert r.status_code==200
    fixture=client.get('/api/demo/document');assert fixture.status_code==200
    x=client.post('/api/kyc/documents',data={'journey_id':jid},files={'file':('demo.png',fixture.content,'image/png')})
    assert x.status_code==200,x.text
    text=x.json()['extracted']['text_preview']
    assert gst['gstin'] not in text
    assert '[GSTIN removed]' in text
    did=x.json()['document_id'];assert client.post('/api/kyc/documents/'+did+'/confirm').status_code==200
    k=client.post('/api/kyc/verify',json={'journey_id':jid,'gstin':gst['gstin'],'udyam_number':ud['udyam_number'],'legal_name':gst['legal_name']})
    assert k.status_code==200,k.text
    assert k.json()['status']=='VERIFIED',k.text
    body={'journey_id':jid,'quote_id':q['quote_id'],'payment_confirmed':True}
    o=client.post('/api/orders',json=body);assert o.status_code==200,o.text
    assert client.post('/api/orders',json=body).json()['order_id']==o.json()['order_id']
    assert client.patch('/api/journeys/'+jid+'/profile',json={'profile':{'budget':1000}}).status_code==409
    oid=o.json()['order_id']
    assert client.post('/api/mock/ops/orders/'+oid+'/events',json={'status':'ACTIVATED'}).status_code==409
    for status in ['SERVICEABILITY_PENDING','INSTALLATION_SCHEDULED','PROVISIONING','ACTIVATED']:
        assert client.post('/api/mock/ops/orders/'+oid+'/events',json={'status':status}).status_code==200
    assert client.get('/api/orders/'+oid).json()['status']=='ACTIVATED'

@pytest.mark.skipif(shutil.which('tesseract') is None, reason='Tesseract OCR executable is not installed on this machine')
def test_four_document_specimen_kyc_verifies_only_after_review():
    jid=new();profile(jid)
    assert client.post('/api/kyc/consent',data={'journey_id':jid,'accepted':'true'}).status_code==200
    gstin='';udyam=''
    for kind in ('gst_certificate','udyam_certificate','signatory_id','address_proof'):
        image=KYC_FIXTURES/f'K02_{kind}.png'
        response=client.post('/api/kyc/documents',data={'journey_id':jid},files={'file':(image.name,image.read_bytes(),'image/png')})
        assert response.status_code==200,response.text
        uploaded=response.json()['extracted']
        assert all(identifier not in uploaded['text_preview']
                   for identifier in uploaded['gstin_candidates']+uploaded['udyam_candidates'])
        if uploaded['gstin_candidates']:gstin=uploaded['gstin_candidates'][0]
        if uploaded['udyam_candidates']:udyam=uploaded['udyam_candidates'][0]
        document_id=response.json()['document_id']
        assert client.post(f'/api/kyc/documents/{document_id}/confirm').status_code==200

    assert gstin in GST and udyam in UDYAM
    verification=client.post('/api/kyc/verify',json={'journey_id':jid,'gstin':gstin,'udyam_number':udyam,
        'legal_name':GST[gstin]['legal_name'],'require_all_documents':True})
    assert verification.status_code==200,verification.text
    assert verification.json()['status']=='VERIFIED',verification.text

@pytest.mark.skipif(shutil.which('tesseract') is None, reason='Tesseract OCR executable is not installed on this machine')
@pytest.mark.parametrize(('applicant','expected'),[
    ('K01','ACTION_REQUIRED'),('K02','VERIFIED'),('K03','VERIFIED'),('K04','ACTION_REQUIRED'),
    ('K05','ACTION_REQUIRED'),('K06','REVIEW_REQUIRED'),('K07','REVIEW_REQUIRED'),('K08','VERIFIED'),
    ('K09','VERIFIED'),('K10','VERIFIED'),('K11','ACTION_REQUIRED'),('K12','VERIFIED'),
])
def test_all_twelve_supplied_kyc_applicants_keep_expected_decision(applicant,expected):
    jid=new()
    assert client.post('/api/kyc/consent',data={'journey_id':jid,'accepted':'true'}).status_code==200
    extracted=[]
    for image in sorted(KYC_FIXTURES.glob(f'{applicant}_*.png')):
        response=client.post('/api/kyc/documents',data={'journey_id':jid},files={'file':(image.name,image.read_bytes(),'image/png')})
        assert response.status_code==200,(applicant,image.name,response.text)
        document=response.json()['extracted'];extracted.append(document)
        assert client.post(f"/api/kyc/documents/{response.json()['document_id']}/confirm").status_code==200
    gst_document=next((doc for doc in extracted if doc.get('doc_type')=='gst_certificate'),{})
    gstin=gst_document.get('gst_suggestion') or gst_document.get('gstin_suggestion') or next(
        (value for doc in extracted for value in doc.get('gstin_candidates',[])), '')
    # K04's blurred GST certificate is intentionally unreadable; its Udyam number remains checkable.
    udyam=next((value for doc in extracted for value in doc.get('udyam_candidates',[])), '')
    record=GST.get(gstin,{})
    verification=client.post('/api/kyc/verify',json={'journey_id':jid,'gstin':gstin,
        'udyam_number':udyam,'legal_name':record.get('legal_name',''),'require_all_documents':True})
    assert verification.status_code==200,(applicant,verification.text)
    result=verification.json()
    assert result['status']==expected,(applicant,result)
    if applicant=='K06':
        assert gst_document.get('fields',{}).get('gstin_checksum_valid_as_read') is False,gst_document
        assert any('checksum-invalid document value' in reason for reason in result['reasons']),result
    if applicant=='K07':
        assert any('legal name differs from the selected GST registry record' in reason for reason in result['reasons']),result
    if applicant=='K10':
        assert not gst_document.get('gstin_suggestion'),gst_document
        assert gst_document.get('gstin_candidates'),gst_document
