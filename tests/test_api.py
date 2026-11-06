import os
from pathlib import Path
from tempfile import gettempdir
os.environ['ONEJOURNEY_DB']=str(Path(gettempdir())/'onejourney_pytest.sqlite3')
try: Path(os.environ['ONEJOURNEY_DB']).unlink()
except FileNotFoundError: pass
from fastapi.testclient import TestClient
from app.main import app, CAT, GST, UDYAM, check_bundle, calculate
client=TestClient(app)

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
    assert client.get('/api/metrics').json()['activated']==368

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

def test_order_gates_and_idempotency():
    jid=new();profile(jid)
    q=quote(jid,[{'product_id':'P01','quantity':1}]);client.post('/api/quotes/'+q['quote_id']+'/accept')
    body={'journey_id':jid,'quote_id':q['quote_id'],'payment_confirmed':True}
    assert client.post('/api/orders',json=body).status_code==409
    assert client.post('/api/handoffs',json={'journey_id':jid,'reason':'Need help','permission':False}).status_code==403

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
    assert gst['gstin'] in text
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
