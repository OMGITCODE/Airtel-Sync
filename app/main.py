"""OneJourney hackathon prototype. All customer data/registries fictional. No production telecom/payment integrations."""
from __future__ import annotations
import asyncio, csv, io, json, logging, os, re, sqlite3, uuid
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from starlette.concurrency import run_in_threadpool
from . import access, storage, languages
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFont
try:
 from dotenv import load_dotenv
 load_dotenv(Path(__file__).resolve().parent.parent / '.env')
except ImportError:
 pass

from .kyc_pipeline import FIXTURE_TODAY as TODAY
from .genai import configured_model, create_client, local_runtime_status, reasoning_effort, run_agent
from .kyc_pipeline import analyse as analyse_kyc_document, gstin_valid
from .needs import ALLOWED_NEEDS, NEED_ALIASES, NEED_LABELS, NEED_SKUS
from .recommender import build_candidates
from .propensity import handoff_priority

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DB = Path(os.environ.get('ONEJOURNEY_DB', str(ROOT.parent/'onejourney.sqlite3')))
PRODUCTS = {r['product_id']:r for r in csv.DictReader((DATA/'product_catalogue.csv').open())}
GST = {r['gstin'].upper():r for r in csv.DictReader((DATA/'mock_gstin_registry.csv').open())}
UDYAM = {r['udyam_number'].upper():r for r in csv.DictReader((DATA/'mock_udyam_registry.csv').open())}
LEADS = list(csv.DictReader((DATA/'smb_leads.csv').open()))
DEMO_GST = next(x for x in GST.values() if x['status']=='Active')
DEMO_UDY = next((x for x in UDYAM.values() if x['enterprise_name'].strip().lower()==DEMO_GST['legal_name'].strip().lower()), next(iter(UDYAM.values())))
@contextmanager
def con(write=False):
 db=sqlite3.connect(DB, timeout=8)
 db.row_factory=sqlite3.Row
 try:
  db.execute('PRAGMA busy_timeout=8000')
  db.execute('PRAGMA secure_delete=ON')
  if write: db.execute('BEGIN IMMEDIATE')
  with db: yield db
 finally: db.close()

@asynccontextmanager
async def lifespan(application):
 prune_expired_journeys()
 worker=asyncio.create_task(retention_sweeper())
 try:
  yield
 finally:
  worker.cancel()
  try: await worker
  except asyncio.CancelledError: pass

app=FastAPI(title='OneJourney',version='2.0.0',lifespan=lifespan,dependencies=[Depends(access.authorizer(con))])
app.add_middleware(access.BodyLimit)
app.mount('/static', StaticFiles(directory=str(ROOT/'static')), name='static')
RETENTION_DAYS = 7

def init():
 DB.parent.mkdir(parents=True, exist_ok=True)
 with con() as c:
  c.execute('PRAGMA journal_mode=WAL')
  c.executescript('''CREATE TABLE IF NOT EXISTS journeys (id TEXT PRIMARY KEY, language TEXT, profile TEXT, stage TEXT, created_at TEXT, source TEXT DEFAULT 'Digital self-service');
  CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, journey_id TEXT, version INTEGER, items TEXT, recurring_paise INTEGER, setup_paise INTEGER, valid INTEGER, issues TEXT, accepted INTEGER DEFAULT 0, created_at TEXT);
  CREATE TABLE IF NOT EXISTS consents (id TEXT PRIMARY KEY, journey_id TEXT, purpose TEXT, version TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, journey_id TEXT, filename TEXT, extracted TEXT, confirmed INTEGER DEFAULT 0, created_at TEXT);
  CREATE TABLE IF NOT EXISTS kyc (journey_id TEXT PRIMARY KEY, status TEXT, reasons TEXT, gstin TEXT, udyam TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, journey_id TEXT, quote_id TEXT, status TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS order_events (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, status TEXT, details TEXT, simulated INTEGER, created_at TEXT);
  CREATE TABLE IF NOT EXISTS handoffs (id TEXT PRIMARY KEY, journey_id TEXT, reason TEXT, permission INTEGER, source TEXT, status TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS advisor_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, journey_id TEXT, role TEXT, content TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS receipts (quote_id TEXT PRIMARY KEY, receipt TEXT, created_at TEXT);
  ''')
  storage.migrate(c)
init()

def now(): return datetime.now(timezone.utc).isoformat()

def erase_journey(c, jid):
 """Remove a journey and every row linked to it, including event/receipt children."""
 order_ids=[r['id'] for r in c.execute('SELECT id FROM orders WHERE journey_id=?',(jid,)).fetchall()]
 quote_ids=[r['id'] for r in c.execute('SELECT id FROM quotes WHERE journey_id=?',(jid,)).fetchall()]
 if order_ids:
  marks=','.join('?' for _ in order_ids)
  c.execute(f'DELETE FROM order_events WHERE order_id IN ({marks})',order_ids)
 if quote_ids:
  marks=','.join('?' for _ in quote_ids)
  c.execute(f'DELETE FROM receipts WHERE quote_id IN ({marks})',quote_ids)
 for table in ('quotes','consents','documents','kyc','orders','handoffs','advisor_messages', *storage.DEPENDENTS):
  c.execute(f'DELETE FROM {table} WHERE journey_id=?',(jid,))
 c.execute('DELETE FROM journeys WHERE id=?',(jid,))

def prune_expired_journeys(reference_time=None):
 """Delete all journey data seven days after journey creation; return the count."""
 cutoff=((reference_time or datetime.now(timezone.utc))-timedelta(days=RETENTION_DAYS)).isoformat()
 with con() as c:
  expired=[r['id'] for r in c.execute('SELECT id FROM journeys WHERE created_at < ?',(cutoff,)).fetchall()]
  for jid in expired: erase_journey(c,jid)
 return len(expired)

async def retention_sweeper():
 while True:
  await asyncio.sleep(3600)
  prune_expired_journeys()

def uid(prefix): return prefix+'_'+uuid.uuid4().hex[:12]
def j(x): return json.dumps(x,ensure_ascii=False)
def parse(x): return json.loads(x) if isinstance(x,str) else x
def row(c,sql,params=()):
 r=c.execute(sql,params).fetchone();return dict(r) if r else None

def product_view(p):
 r=dict(p)
 for f in ['price_min_inr_per_month','price_max_inr_per_month','one_time_setup_inr','min_contract_months','min_employees','min_sites','typical_install_days']:r[f]=int(r[f])
 r['requires_gstin']=r['requires_gstin']=='Y'
 r['requires_one_of']=r['requires_one_of'].split('|') if r['requires_one_of'] else []
 r['available_city_tiers']=[int(x) for x in r['available_city_tiers'].split(';')]
 return r

CAT={k:product_view(v) for k,v in PRODUCTS.items()}

def gst_active(gstin):return bool(gstin and GST.get(str(gstin).upper(),{}).get('status')=='Active')

def money(n):return int((Decimal(str(n))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))

def price_paise(p):return money((Decimal(p['price_min_inr_per_month'])+Decimal(p['price_max_inr_per_month']))/2)

def check_bundle(profile,items):
 errors=[]; quantities={}
 for it in items:
  pid=str(it.get('product_id','')).upper()
  try: q=int(it.get('quantity',1))
  except (ValueError,TypeError):q=0
  if pid not in CAT: errors.append({'product_id':pid,'code':'UNKNOWN_SKU','message':'Unknown product'});continue
  if q<1 or q>100: errors.append({'product_id':pid,'code':'INVALID_QUANTITY','message':'Quantity must be 1–100'});continue
  quantities[pid]=quantities.get(pid,0)+q
 selected=set(quantities)
 for pid,q in quantities.items():
  p=CAT[pid]
  if int(profile.get('city_tier') or 2) not in p['available_city_tiers']:errors.append({'product_id':pid,'code':'CITY_TIER','message':'Not sold in this city tier'})
  if int(profile.get('employees') or 1)<p['min_employees']:errors.append({'product_id':pid,'code':'EMPLOYEES','message':f"Requires at least {p['min_employees']} employees"})
  if int(profile.get('sites') or 1)<p['min_sites']:errors.append({'product_id':pid,'code':'SITES','message':f"Requires at least {p['min_sites']} sites"})
  if p['requires_gstin'] and not gst_active(profile.get('gstin')):errors.append({'product_id':pid,'code':'ACTIVE_GSTIN','message':'Requires a valid active GSTIN in mock registry'})
  if p['requires_one_of'] and not any(req in selected for req in p['requires_one_of']):errors.append({'product_id':pid,'code':'DEPENDENCY','message':'Requires at least one of: '+', '.join(p['requires_one_of'])})
  if p['unit'] in ('per site',) and q>int(profile.get('sites') or 1):errors.append({'product_id':pid,'code':'SITE_QUANTITY','message':'Site quantity exceeds business site count'})
  if p['unit'] in ('per user',) and q>int(profile.get('employees') or 1):errors.append({'product_id':pid,'code':'USER_QUANTITY','message':'User licence quantity exceeds employee count'})
 return errors

def calculate(profile,items,term_months=None):
 if term_months is None:
  term_months=max([12]+[CAT[it['product_id']]['min_contract_months'] for it in items if it.get('product_id') in CAT])
 issues=check_bundle(profile,items);lines=[];rec=setup=0
 grouped={}
 for it in items:
  pid=str(it.get('product_id','')).upper()
  if pid in CAT:
   try:q=int(it.get('quantity',1))
   except:q=0
   if q>0:grouped[pid]=grouped.get(pid,0)+q
 for pid,q in grouped.items():
  p=CAT[pid]
  if term_months<p['min_contract_months']:issues.append({'product_id':pid,'code':'TERM','message':f"Minimum term is {p['min_contract_months']} months"})
  unit=price_paise(p) if term_months==12 else money(p['price_min_inr_per_month'])
  s=money(p['one_time_setup_inr'])*q
  lines.append({'product_id':pid,'name':p['product_name'],'quantity':q,'unit':p['unit'],'unit_price_paise':unit,'monthly_paise':q*unit,'setup_paise':s,'term_months':term_months,'install_days':p['typical_install_days']})
  rec+=q*unit;setup+=s
 return {'items':lines,'recurring_paise':rec,'setup_paise':setup,'issues':issues,'valid':not issues,'currency':'INR','gst_paise':money(Decimal(rec)/100*Decimal('.18')),'monthly_with_gst_paise':rec+money(Decimal(rec)/100*Decimal('.18')),'setup_gst_paise':money(Decimal(setup)/100*Decimal('.18')),'term_months':term_months,'pricing_note':'Illustrative 12-month midpoint or 24-month catalogue minimum, plus 18% simulated GST.'}

IDENTITY_FIELDS=('gstin','udyam_number','name','legal_name')
BOUND_FIELDS=(*IDENTITY_FIELDS,'employees','sites','city_tier')

def public_profile(p):
 return {**p,**{key:mask_kyc_value(p.get(key)) or '' for key in ('gstin','udyam_number','udyam') if key in p}}

def facts(p):
 return j({k:p.get(k) for k in BOUND_FIELDS})

def invalidate(c,jid,reason):
 c.execute('DELETE FROM bindings WHERE journey_id=?',(jid,))
 c.execute("UPDATE kyc SET status='ACTION_REQUIRED',reasons=? WHERE journey_id=?",(j([reason]),jid))

def getjourney(jid):
 with con() as c:
  r=row(c,'SELECT * FROM journeys WHERE id=?',(jid,))
 if not r:raise HTTPException(404,'Journey not found')
 r['profile']=parse(r['profile']);return r

class CreateJourney(BaseModel):
 language:str=Field(default='hi',pattern='^(en|hi|mr|ta|te|kn|gu)$');source:Literal['Digital self-service','Referral','Channel partner','Field sales','Website form','WhatsApp campaign','Inbound call']='Digital self-service'
class ProfileUpdate(BaseModel):
 profile:dict[str,Any]
class TextInput(BaseModel):
 text:str=Field(min_length=1,max_length=5000)
class QuoteInput(BaseModel):
 journey_id:str;items:list[dict[str,Any]]=Field(min_length=1,max_length=30);term_months:Literal[12,24]=12;site_code:Literal['MARKET','WIRELESS','SURVEY']='MARKET'
class QuoteAccept(BaseModel):
 quote_id:str
class VerifyKyc(BaseModel):
 journey_id:str;gstin:str=Field(default='',max_length=32);udyam_number:str=Field(default='',max_length=40);legal_name:str=Field(default='',max_length=120);require_all_documents:bool=True
class OrderInput(BaseModel):
 journey_id:str;quote_id:str;payment_confirmed:bool=False;payment_result:Literal['success','failed']='success';install_date:date|None=None;channel:Literal['SMS','WhatsApp']='SMS'
class OpsInput(BaseModel):
 status:str;details:str=''
class HandoffInput(BaseModel):
 journey_id:str;reason:Literal['SITE_SURVEY','PRICE','INELIGIBLE','KYC_REVIEW','NO_GSTIN','INSTALLATION','OTHER']='OTHER';permission:bool=False

@app.get('/')
def index():return FileResponse(ROOT/'static'/'index.html')
@app.get('/privacy')
def privacy():return FileResponse(ROOT/'static'/'privacy.html')
@app.get('/api/health')
def health():
 local=local_runtime_status()
 return {'ok':True,'demo_mode':os.getenv('ONEJOURNEY_DEMO_MODE')=='1','fixture_date':str(TODAY),'products':len(CAT),'leads':len(LEADS),'mock_gstin':len(GST),'mock_udyam':len(UDYAM),'retention_days':RETENTION_DAYS,'advisor':{'provider':'Ollama local','status':local['status'],'model':configured_model(),'reasoning_effort':reasoning_effort()}}
@app.get('/api/catalogue')
def catalogue():return list(CAT.values())
@app.get('/api/needs')
def need_catalogue():
 return [{'id':need,'label':NEED_LABELS[need],'aliases':NEED_ALIASES[need]} for need in ALLOWED_NEEDS]
@app.get('/api/demo')
def demo():
 if os.getenv('ONEJOURNEY_DEMO_MODE')!='1':raise HTTPException(404,'Demo fixtures disabled')
 return {'name':'Clinic demo','business_type':'Clinic','employees':6,'sites':1,'city_tier':2,'segment':'prof','budget':4500}
@app.get('/api/demo/document')
def demo_document(request:Request):
 if os.getenv("ONEJOURNEY_DEMO_MODE")!="1":raise HTTPException(404,"Demo disabled")
 with con() as db:
  if not db.execute("SELECT 1 FROM agreements WHERE journey_id=? AND purpose='kyc'",(request.state.journey_id,)).fetchone():raise HTTPException(403,"Consent required")
 im=Image.new('RGB',(1800,760),'white');draw=ImageDraw.Draw(im);font=ImageFont.load_default(size=48)
 text='\n'.join([
  'FICTIONAL SAMPLE - NOT A REAL CERTIFICATE',
  'GST REGISTRATION CERTIFICATE - FICTIONAL DEMONSTRATION',
  f"Legal business name: {DEMO_GST['legal_name']}",
  f"Trade name: {DEMO_GST['trade_name']}",
  f"GSTIN: {DEMO_GST['gstin']}",
  f"Udyam number: {DEMO_UDY['udyam_number']}",
  'Created in memory for the OneJourney local demonstration.'
 ])
 draw.multiline_text((80,70),text,fill='#18243a',font=font,spacing=30)
 output=io.BytesIO();im.save(output,format='PNG')
 return Response(content=output.getvalue(),media_type='image/png',headers={'Content-Disposition':'attachment; filename=fictional-demo-business-certificate.png'})

@app.get('/api/metrics')
def metrics(segment:str='all',period:str='all'):
 segment_options=sorted({x['segment'] for x in LEADS if x.get('segment')})
 period_options=sorted({x.get('created_date','')[:7] for x in LEADS if x.get('created_date')})
 if segment!='all' and segment not in segment_options:raise HTTPException(422,'Unknown lead segment')
 if period!='all' and period not in period_options:raise HTTPException(422,'Unknown cohort period')
 segment_cohort=LEADS if segment=='all' else [x for x in LEADS if x.get('segment')==segment]
 cohort=segment_cohort if period=='all' else [x for x in segment_cohort if x.get('created_date','').startswith(period)]
 rank={'Lead':0,'Contacted':1,'Needs assessed':2,'Quote accepted':3,'KYC complete':4,'Activated':5}
 stage_order=[('Lead',0),('Contacted',1),('Needs assessed',2),('Quote accepted',3),('KYC complete',4),('Activated',5)]
 funnel=[]
 previous=None
 for label,level in stage_order:
  count=sum(rank.get(x.get('stage_reached','Lead'),0)>=level for x in cohort)
  funnel.append({'label':label,'count':count,'conversion_pct':round(count*100/previous,1) if previous else None})
  previous=count
 drops=[{'from':funnel[i-1]['label'],'to':funnel[i]['label'],'count':funnel[i-1]['count']-funnel[i]['count'],'drop_pct':round((funnel[i-1]['count']-funnel[i]['count'])*100/funnel[i-1]['count'],1) if funnel[i-1]['count'] else 0} for i in range(1,len(funnel))]
 bottleneck=max(drops,key=lambda x:x['count']) if drops else None
 activated=[x for x in cohort if x.get('outcome')=='Activated']
 def numeric(records,key):return [float(x[key]) for x in records if x.get(key,'').replace('.','',1).isdigit()]
 durations=numeric(activated,'days_lead_to_activation')
 calls=numeric(activated,'customer_follow_up_calls')
 costs=numeric(cohort,'acquisition_cost_inr')
 customer_scores=numeric(activated,'nps_score')
 churn_values = {'Y': True, 'N': False, '1': True, '0': False}
 churn = [churn_values[value] for row in activated
          if (value := row.get('churned_within_6_months', '').strip().upper()) in churn_values]
 months=[]
 for month in period_options:
  monthly=[x for x in segment_cohort if x.get('created_date','').startswith(month)]
  monthly_activated=sum(x.get('outcome')=='Activated' for x in monthly)
  months.append({'period':month,'leads':len(monthly),'activated':monthly_activated,'activation_pct':round(monthly_activated*100/len(monthly),2) if monthly else 0})
 needs_assessed=funnel[2]['count'];quote_accepted=funnel[3]['count']
 return {'leads':len(cohort),'activated':len(activated),'activation_pct':round(len(activated)/len(cohort)*100,2) if cohort else 0,'quote_accepted':quote_accepted,'needs_assessed':needs_assessed,'quote_accept_pct':round(quote_accepted*100/needs_assessed,2) if needs_assessed else 0,'funnel':funnel,'bottleneck':bottleneck,'trend':months,'avg_activation_days':round(sum(durations)/len(durations),1) if durations else None,'avg_follow_up_calls':round(sum(calls)/len(calls),1) if calls else None,'avg_acquisition_cost_inr':round(sum(costs)/len(activated)) if costs and activated else None,'avg_customer_score':round(sum(customer_scores)/len(customer_scores),1) if customer_scores else None,'churn_pct':round(sum(churn)*100/len(churn),1) if churn else None,'segments':['all']+segment_options,'periods':['all']+period_options,'segment':segment,'period':period,'source':'Computed from supplied synthetic challenge records. Not real Airtel performance.'}

@app.post('/api/journeys')
def newjourney(b:CreateJourney):
 i=uid('journey');p={'business_type':'','employees':None,'sites':None,'city_tier':None,'segment':'prof','budget':None,'gstin':'','udyam_number':'','needs':[],'must':[],'confirmed':False,'language':b.language,'current_tools':''}
 with con(write=True) as c:
  token=access.issue(c,i)
  c.execute('INSERT INTO milestones(journey_id,kind,created_at) VALUES(?,?,?)',(i,'started',now()))
  c.execute('INSERT INTO journeys(id,language,profile,stage,created_at,source) VALUES (?,?,?,?,?,?)',(i,b.language,j(p),'DISCOVERY',now(),b.source))
 return {'id':i,'token':token,'profile':p,'stage':'DISCOVERY'}
@app.get('/api/journeys/{jid}')
def journey(jid:str):
 r=getjourney(jid)
 with con() as c:
  quotes=[dict(x) for x in c.execute('SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC',(jid,)).fetchall()]
  k=row(c,'SELECT * FROM kyc WHERE journey_id=?',(jid,));orders=[dict(x) for x in c.execute('SELECT * FROM orders WHERE journey_id=? ORDER BY created_at DESC',(jid,))]
 for q in quotes:
  q['issues']=parse(q['issues']);q['items']=parse(q['items'])
  with con() as db2:receipt=row(db2,'SELECT receipt FROM receipts WHERE quote_id=?',(q['id'],))
  q['receipt']=parse(receipt['receipt']) if receipt else None
 if k:
  k['notes']=['The matched applicant is an office admin; designation alone does not block verification.'] if any(l['gstin']==k['gstin'] and l['contact_role'].casefold()=='office admin' for l in LEADS) else []
  k['reasons']=parse(k['reasons']);k['gstin']=mask_kyc_value(k['gstin']);k['udyam']=mask_kyc_value(k['udyam'])
 r['profile']=public_profile(r['profile'])
 from .onboarding import has,public_doc
 with con() as c:
  agreements={key:has(c,jid,key) for key in ('kyc','partner')}
  docs=[{'document_id':d['id'],'confirmed':bool(d['confirmed']),'filename':'Reviewed upload','extracted':public_doc(parse(d['extracted']))} for d in c.execute("SELECT * FROM documents WHERE journey_id=? AND extracted!='' ORDER BY rowid",(jid,))]
  for q in quotes:
   term=row(c,'SELECT payload FROM quote_terms WHERE quote_id=?',(q['id'],))
   if term:q['terms']=parse(term['payload'])
 return {**r,'quotes':quotes,'kyc':k,'orders':orders,'consents':agreements,'documents':docs}

@app.delete('/api/journeys/{jid}/data')
def delete_journey_data(jid:str):
 with con() as c:
  if not row(c,'SELECT id FROM journeys WHERE id=?',(jid,)):raise HTTPException(404,'Journey not found or already deleted')
  erase_journey(c,jid)
 return {'deleted':True,'journey_id':jid,'message':'Journey profile, documents, consent, quotes, messages, handoffs, and simulated order data were deleted.'}
@app.patch('/api/journeys/{jid}/profile')
def update_profile(jid:str,b:ProfileUpdate):
 with con(write=True) as c:
  old=parse(row(c,'SELECT profile FROM journeys WHERE id=?',(jid,))['profile'])
  supplied=dict(b.profile)
  if 'udyam' in supplied:
   alias=supplied.pop('udyam')
   if 'udyam_number' in supplied and supplied['udyam_number']!=alias:raise HTTPException(422,'Conflicting Udyam input fields')
   supplied['udyam_number']=alias
  p={**old,**supplied}
  for field in ('gstin','udyam_number'):
   if p.get(field) and p[field]==mask_kyc_value(old.get(field)):p[field]=old.get(field)
  p.setdefault('must',[])
  validate_profile(p)
  binding=row(c,'SELECT status FROM kyc WHERE journey_id=?',(jid,))
  changed=any(p.get(k)!=old.get(k) for k in BOUND_FIELDS)
  if changed and binding and binding['status']=='VERIFIED' and any(p.get(k)!=old.get(k) for k in IDENTITY_FIELDS):
   raise HTTPException(409,'Verified identity is locked. Withdraw KYC consent before changing business identity.')
  if p.get('confirmed') and p.get('gstin') and gst_active(p['gstin']):
   owner=row(c,'SELECT journey_id FROM claims WHERE gstin=?',(p['gstin'],))
   if owner and owner['journey_id']!=jid:raise HTTPException(409,'GST registration already has an active journey. Request assisted review.')
   c.execute('DELETE FROM claims WHERE journey_id=? AND gstin<>?',(jid,p['gstin']))
   c.execute('INSERT OR IGNORE INTO claims VALUES(?,?)',(p['gstin'],jid))
  elif changed and not p.get('gstin'):c.execute('DELETE FROM claims WHERE journey_id=?',(jid,))
  if changed:invalidate(c,jid,'Profile changed; verify the updated facts again')
  if row(c,'SELECT id FROM orders WHERE journey_id=?',(jid,)):
   raise HTTPException(409,'Order already submitted. Changes require an assisted order amendment, not quote rewriting.')
  c.execute('UPDATE journeys SET profile=?,language=?,stage=? WHERE id=?',(j(p),p.get('language','en'),'PROFILE_CONFIRMED' if p.get('confirmed') else 'DISCOVERY',jid))
  last=row(c,'SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC LIMIT 1',(jid,))
  invalidated=[];proposals=[];items=[]
  if last:
   items=parse(last['items']);issues=check_bundle(p,items);budget=int(p.get('budget') or 0)
   if budget and last['recurring_paise']>budget*100:
    issues=issues+[{'product_id':'*','code':'BUDGET','message':f"Latest quote is ₹{last['recurring_paise']/100:,.0f}/month; the confirmed budget is ₹{budget:,}."}]
   if issues:
    if last['accepted']:c.execute('UPDATE quotes SET accepted=0 WHERE id=?',(last['id'],))
    invalidated=issues
  if invalidated:
   from .onboarding import auto_handoff
   auto_handoff(c,jid,'PRICE' if any(i['code']=='BUDGET' for i in invalidated) else 'INELIGIBLE')
 if invalidated:proposals=recovery_options(p,items)
 return {'profile':public_profile(p),'kyc_invalidated':changed,'invalidated':invalidated,'recovery_options':proposals,'notice':'The latest quote was rechecked against the confirmed profile. Choose a recovery option to create a new quote version.'}

class RecoverInput(BaseModel):
 drop:list[str]=Field(default_factory=list)

@app.post('/api/journeys/{jid}/recover')
def recover(jid:str,b:RecoverInput):
 profile=getjourney(jid)['profile']
 with con() as c:
  if row(c,'SELECT id FROM orders WHERE journey_id=?',(jid,)):
   raise HTTPException(409,'Submitted orders require an assisted amendment; their quote cannot be rewritten.')
  last=row(c,'SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC LIMIT 1',(jid,))
 if not last:raise HTTPException(404,'There is no quote to recover.')
 previous_items=parse(last['items']);requested=sorted({item.strip().upper() for item in b.drop})
 option=next((candidate for candidate in recovery_options(profile,previous_items) if candidate['drop']==requested),None)
 if option is None:raise HTTPException(409,'Choose one of the currently validated recovery options.')
 with con() as db:terms=row(db,'SELECT payload FROM quote_terms WHERE quote_id=?',(last['id'],))
 term=parse(terms['payload'])['term_months'] if terms else 12
 old=calculate(profile,previous_items,term);new=quote(QuoteInput(journey_id=jid,items=option['items'],term_months=term,site_code=parse(terms['payload']).get('site_code','MARKET') if terms else 'MARKET'))
 receipt={'previous_version':last['version'],'new_version':new['version'],
  'changed_facts':sorted({issue['code'] for issue in check_bundle(profile,previous_items)}|({'BUDGET'} if int(profile.get('budget') or 0) and old['recurring_paise']>int(profile['budget'])*100 else set())),
  'preserved':[item['product_id'] for item in option['items']],
  'dropped':[{'product_id':pid,'name':CAT[pid]['product_name'],'need':need_of(pid,profile)} for pid in requested if pid in CAT],
  'unmet_needs':option['unmet_needs'],'monthly_before_paise':old['recurring_paise'],
  'monthly_after_paise':new['recurring_paise'],'setup_before_paise':old['setup_paise'],
  'setup_after_paise':new['setup_paise'],'within_budget':option['within_budget'],
  'valid':new['valid'],'requires_new_acceptance':True,'pricing_policy':'CATALOGUE_TERM_V2','term_months':term,
  'note':'Eligibility rules stayed in force. Only the customer-selected items were removed.'}
 with con() as c:c.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',(new['quote_id'],j(receipt),now()))
 return {**new,'receipt':receipt}

def need_of(pid:str,profile:dict[str,Any]|None=None)->str:
 pid=str(pid).upper()
 if pid in NEED_SKUS.get('dedicated_line',[]) and profile and 'dedicated_line' in (profile.get('needs') or []):return 'dedicated_line'
 for need,skus in NEED_SKUS.items():
  if pid in skus:return need
 return 'extra'

def drop_lines(profile:dict[str,Any],items:list[dict[str,Any]],drop:set[str])->tuple[list[dict[str,Any]],list[str]]:
 """Remove selected items and any dependencies they leave orphaned."""
 dropped={pid.upper() for pid in drop};keep=[item for item in items if str(item.get('product_id','')).upper() not in dropped]
 for _ in range(12):
  orphaned={issue['product_id'] for issue in check_bundle(profile,keep) if issue['code']=='DEPENDENCY'}
  if not orphaned:break
  dropped|=orphaned;keep=[item for item in keep if item['product_id'] not in orphaned]
 return keep,sorted(dropped)

def recovery_options(profile:dict[str,Any],items:list[dict[str,Any]])->list[dict[str,Any]]:
 """Offer eligible, priced alternatives; the customer chooses every reduction."""
 budget=int(profile.get('budget') or 0)*100;must=set(profile.get('must') or [])|{'internet'}
 base=calculate(profile,items);forced={issue['product_id'] for issue in base['issues'] if issue['code']!='DEPENDENCY'}
 start,forced_drop=drop_lines(profile,items,forced);options=[]
 def add(keep:list[dict[str,Any]],dropped:list[str])->None:
  if not keep:return
  quote=calculate(profile,keep)
  if not quote['valid']:return
  unmet=sorted({need_of(pid,profile) for pid in dropped if need_of(pid,profile)!='extra'})
  if set(unmet)&must:return
  options.append({'drop':dropped,'dropped_names':[CAT[pid]['product_name'] for pid in dropped if pid in CAT],
   'unmet_needs':unmet,'items':keep,'recurring_paise':quote['recurring_paise'],'setup_paise':quote['setup_paise'],
   'within_budget':not budget or quote['recurring_paise']<=budget,
   'saves_paise':base['recurring_paise']-quote['recurring_paise'],'forced':bool(forced_drop)})
 if forced_drop:add(start,forced_drop)
 for pid in sorted({str(item['product_id']).upper() for item in start}):
  keep,dropped=drop_lines(profile,items,set(forced_drop)|{pid});add(keep,dropped)
 unique={tuple(option['drop']):option for option in options}
 return sorted(unique.values(),key=lambda option:(not option['within_budget'],-option['recurring_paise']))[:3]

UNMET_REASONS={'ACTIVE_GSTIN':'requires an active GSTIN','CITY_TIER':'is not sold in this city tier',
 'EMPLOYEES':'requires a larger team','SITES':'requires more sites','DEPENDENCY':'requires a compatible connectivity product',
 'NOT_SELECTED':'is not included in this option','INVALID_QUANTITY':'exceeds the supported quantity for one quote and needs review',
 'USER_QUANTITY':'requires a quantity within the confirmed employee count','SITE_QUANTITY':'requires a quantity within the confirmed site count'}

def unmet_needs(profile:dict[str,Any],items:list[dict[str,Any]])->list[dict[str,Any]]:
 have={str(item['product_id']).upper() for item in items};out=[]
 for need in profile.get('needs') or []:
  skus=NEED_SKUS.get(need,[])
  if not skus or have&set(skus):continue
  eligible=[];failures=[]
  for pid in skus:
   unit=CAT[pid]['unit'].casefold()
   quantity=int(profile.get('sites') or 1) if 'site' in unit else int(profile.get('employees') or 1) if 'user' in unit else 1
   issues=[issue for issue in check_bundle(profile,items+[{'product_id':pid,'quantity':quantity}]) if issue['product_id']==pid]
   if not issues:eligible.append(pid)
   else:failures.extend(issues)
  if eligible:code='NOT_SELECTED'
  else:code=next((issue['code'] for issue in failures if issue['code']!='DEPENDENCY'),'DEPENDENCY')
  out.append({'need':need,'label':NEED_LABELS.get(need,need.replace('_',' ').title()),'code':code,'reason':UNMET_REASONS.get(code,'requires review'),
   'unlock_with_gstin':code=='ACTIVE_GSTIN'})
 return out

def candidates(profile):
 return build_candidates(profile,CAT,check_bundle,price_paise)

DEVANAGARI_DIGITS=str.maketrans('०१२३४५६७८९','0123456789')

def redact_chat(text:str)->str:
 """Remove common direct identifiers before chat is sent to or saved for the model."""
 text=languages.normalize_digits(text)
 text=re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b','[email removed]',text,flags=re.I)
 text=re.sub(r'\bUDYAM[\s-]*[A-Z]{2}[\s-]*\d{2}[\s-]*\d{7}\b','[Udyam ID removed]',text,flags=re.I)
 text=re.sub(r'\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b','[GSTIN removed]',text,flags=re.I)
 text=re.sub(r'(?<!\w)(?:\+?91[ -]?)?[6-9](?:[ -]?\d){9}(?!\w)','[phone removed]',text)
 return text[:5000]

def safe_profile(profile:dict[str,Any])->dict[str,Any]:
 # Return every discovery field needed by the form, while keeping registry identifiers
 # and legal identity out of the advisor's conversation context.
 return {k:profile.get(k) for k in ('name','business_type','employees','sites','city_tier','budget','needs','must','language','current_tools','confirmed') if k in profile}

def catalogue_search(query:str,category:str|None,segment:str|None,max_results:int)->dict[str,Any]:
 terms={x for x in re.findall(r'[a-z0-9]+',query.casefold()) if len(x)>1}
 rows=[]
 for p in CAT.values():
  if category and category.casefold() not in p['category'].casefold():continue
  if segment and segment.casefold() not in p['target_segments'].casefold():continue
  hay=' '.join(str(p.get(k,'')) for k in ('product_id','category','product_name','description','target_segments')).casefold()
  score=sum(1 for term in terms if term in hay)
  if not terms or score:rows.append((score,p))
 rows.sort(key=lambda x:(-x[0],x[1]['product_id']))
 products=[{'product_id':p['product_id'],'category':p['category'],'name':p['product_name'],'description':p['description'],'unit':p['unit'],'price_min_inr_per_month':p['price_min_inr_per_month'],'price_max_inr_per_month':p['price_max_inr_per_month'],'setup_inr_per_unit':p['one_time_setup_inr'],'minimum_contract_months':p['min_contract_months'],'minimum_employees':p['min_employees'],'minimum_sites':p['min_sites'],'requires_gstin':p['requires_gstin'],'requires_one_of':p['requires_one_of'],'available_city_tiers':p['available_city_tiers']} for _,p in rows[:max(1,min(8,max_results))]]
 return {'summary':f"Found {len(products)} catalogue matches from the supplied synthetic catalogue.",'products':products,'source':'app/data/product_catalogue.csv'}

def explicitly_grounded(key:str,value:Any,source_text:str|None)->bool:
 if source_text is None:return True
 text=source_text.casefold()
 if key in ('employees','sites','budget') and isinstance(value,int) and not isinstance(value,bool):
  number=f"(?:{re.escape(str(value))}|{re.escape(f'{value:,}')})"
  anchors={
   'employees':r'employees?|staff|workers?|team members?|कर्मचारी|कर्मचारियों|लोग|स्टाफ|कर्मी',
   'sites':r'sites?|locations?|branches|outlets?|offices?|दुकान|शाखा|स्थान|लोकेशन',
   'budget':r'budget|spend|pay|afford|inr|rupees?|monthly|बजट|खर्च|रुपये|रुपए|महीने',
  }[key]
  anchors += '|'+'|'.join(languages.ANCHORS[key])
  anchor_end=r'(?=\s|[,;:.!?]|$)'
  return re.search(rf"(?:\b{number}\b[\s\S]{{0,35}}(?:{anchors}){anchor_end}|(?:{anchors}){anchor_end}[\s\S]{{0,35}}\b{number}\b)",text) is not None
 if key=='city_tier' and value in (1,2):
  return re.search(rf'\btier\s*(?:{value}|{("one" if value==1 else "two")})\b',text) is not None
 if key=='language' and isinstance(value,str):
  language={'en':'english','hi':'hindi','mr':'marathi','ta':'tamil','te':'telugu','kn':'kannada','gu':'gujarati'}.get(value)
  return bool(language and re.search(rf'\b(?:{language})\b',text))
 if key=='business_type' and isinstance(value,str):
  aliases={
   'clinic':['clinic','dental practice','dentist','क्लिनिक','दवाखाना'], 'retail':['retail','shop','store','kirana','grocery','किराना','दुकान'],
   'restaurant':['restaurant','cafe','café','eatery','food outlet'], 'pharmacy':['pharmacy','chemist','medical shop'],
   'coaching centre':['coaching centre','coaching center','tuition','tutoring'],
   'manufacturing':['manufacturing','manufacturer','factory'], 'ca firm':['ca firm','chartered accountant'],
   'bakery':['bakery','bake shop'],
  }
  terms=aliases.get(value.casefold(),[value.casefold()])+list(languages.BUSINESS.get(value.casefold(),()))
  return any(re.search(rf'(?<!\w){re.escape(term)}(?!\w)',text) for term in terms)
 if key=='needs' and isinstance(value,str):
  aliases={
   'internet':['internet','broadband','wifi','wi-fi','fibre','fiber','इंटरनेट'], 'backup':['backup','failover','secondary connection','बैकअप'],
   'email':['email','e-mail','ईमेल'], 'security':['security','firewall','cybersecurity','सुरक्षा'],
   'calling':['calling','voice','landline','कॉलिंग','कॉल'], 'mobile':['mobile sim','mobile service','sim card','मोबाइल सिम','सिम कार्ड'],
   'website':['website','web presence','online store','वेबसाइट'], 'cctv':['cctv','surveillance camera','सीसीटीवी'],
  }
  return any(re.search(rf'(?<!\w){re.escape(term)}(?!\w)',text) for term in aliases.get(value, [value])+list(languages.NEEDS.get(value,())))
 if key=='current_tools' and isinstance(value,str):
  terms=[word for word in re.findall(r'[a-z0-9]+',value.casefold()) if word not in {'the','and','for','with'}]
  if terms and sum(term in text for term in terms)>=max(1,(len(terms)+1)//2):return True
  hindi_tools={'mobile hotspot':['मोबाइल हॉटस्पॉट','हॉटस्पॉट'],'mobile data':['मोबाइल डेटा','मोबाइल इंटरनेट'],'broadband':['ब्रॉडबैंड'],'wifi':['वाईफाई','वाइफाइ']}
  return any(term in text for term in hindi_tools.get(value.casefold(),[]))
 return False

def explicitly_mandatory(need:str,source_text:str|None)->bool:
 """A model cannot turn a mentioned priority into a must-keep without explicit wording."""
 if not source_text:return False
 terms={
  'internet':['internet','broadband','wifi','wi-fi','fibre','fiber','इंटरनेट'],
  'backup':['backup','failover','secondary connection','बैकअप'],
  'email':['email','e-mail','ईमेल'],
  'security':['security','firewall','cybersecurity','सुरक्षा'],
  'calling':['calling','voice','landline','कॉलिंग','कॉल'],
  'mobile':['mobile sim','mobile service','sim card','मोबाइल सिम','सिम कार्ड'],
  'website':['website','web presence','online store','वेबसाइट'],
  'cctv':['cctv','surveillance camera','सीसीटीवी'],
 }
 mandatory=re.compile(r'\b(?:must(?:[- ](?:keep|have))?|essential|non[- ]negotiable|cannot (?:be )?(?:removed|dropped)|need to keep|have to keep)\b|ज़रूरी|जरूरी|अनिवार्य|नहीं छोड़|नही छोड़|नहीं हट|नही हट')
 text=source_text.casefold()
 for term in terms.get(need,[]):
  for match in re.finditer(rf'(?<!\w){re.escape(term)}(?!\w)',text):
   nearby=text[max(0,match.start()-55):min(len(text),match.end()+55)]
   if mandatory.search(nearby):return True
 return False

def current_tools_from_text(source_text:str|None)->str|None:
 """Fill common, explicitly named tools even when the model omits a valid field."""
 if not source_text:return None
 labels={
  'Mobile hotspot':['mobile hotspot','phone hotspot','tethering','मोबाइल हॉटस्पॉट','हॉटस्पॉट'],
  'Mobile data':['mobile data','phone data','मोबाइल डेटा','मोबाइल इंटरनेट'],
  'Broadband':['broadband','broad band','ब्रॉडबैंड'],
  'Wi-Fi':['wi-fi','wifi','वाईफाई','वाइफाई'],
  'Fibre broadband':['fibre broadband','fiber broadband','fibre','fiber','फाइबर'],
  'Personal email':['personal email','gmail','yahoo mail'],
  'Landline':['landline','fixed line','लैंडलाइन'],
 }
 text=source_text.casefold();found=[]
 for label,terms in labels.items():
  for term in terms:
   pattern=rf'(?<!\w){re.escape(term)}(?!\w)' if term.isascii() else re.escape(term)
   match=re.search(pattern,text)
   if not match:continue
   prefix=text[max(0,match.start()-24):match.start()]
   if re.search(r"\b(?:not|never|no|don't|doesn't|didn't|do not|does not)(?:\s+(?:currently|really|ever|use|have|a|an|the))*\s*$|नहीं\s*$|नही\s*$",prefix):continue
   found.append(label);break
 return ', '.join(dict.fromkeys(found)) or None

def propose_profile(profile:dict[str,Any],args:dict[str,Any],source_text:str|None=None)->dict[str,Any]:
 updated={**profile};changes={}
 for key,minimum,limit in (('employees',1,500),('sites',1,100),('budget',0,10000000)):
  value=args.get(key)
  if isinstance(value,int) and not isinstance(value,bool) and minimum<=value<=limit and explicitly_grounded(key,value,source_text):changes[key]=value
 for key in ('business_type','current_tools'):
  value=args.get(key)
  if isinstance(value,str) and value.strip() and explicitly_grounded(key,value,source_text):changes[key]=redact_chat(value.strip())[:100]
 if 'current_tools' not in changes:
  detected_tools=current_tools_from_text(source_text)
  if detected_tools:changes['current_tools']=detected_tools
 tier=args.get('city_tier')
 if isinstance(tier,int) and not isinstance(tier,bool) and tier in (1,2) and explicitly_grounded('city_tier',tier,source_text):changes['city_tier']=tier
 lang=args.get('language')
 if lang in ('en','hi','mr','ta','te','kn','gu') and explicitly_grounded('language',lang,source_text):changes['language']=lang
 needs=args.get('needs')
 if isinstance(needs,list):
  allowed=set(ALLOWED_NEEDS)
  stated={x.strip().casefold() for x in needs if isinstance(x,str) and x.strip().casefold() in allowed and explicitly_grounded('needs',x.strip().casefold(),source_text)}
  existing={x for x in updated.get('needs',[]) if isinstance(x,str) and x in allowed}
  if stated:changes['needs']=sorted(existing|stated)
 must=args.get('must')
 if isinstance(must,list):
  explicit_must={x.strip().casefold() for x in must if isinstance(x,str) and x.strip().casefold() in set(ALLOWED_NEEDS) and explicitly_grounded('needs',x.strip().casefold(),source_text) and explicitly_mandatory(x.strip().casefold(),source_text)}
  if explicit_must:
   existing_must={x for x in updated.get('must',[]) if isinstance(x,str) and x in set(ALLOWED_NEEDS)}
   changes['must']=sorted(existing_must|explicit_must)
   changes['needs']=sorted({x for x in (changes.get('needs') or updated.get('needs') or []) if x in set(ALLOWED_NEEDS)}|explicit_must)
 updated.update(changes)
 updated['confirmed']=False
 return {'summary':'Captured explicit business details as unconfirmed suggestions.' if changes else 'No new profile facts were stated clearly enough to capture.','updates':changes,'profile':safe_profile(updated)}

PROFILE_NEEDS=set(ALLOWED_NEEDS)
PROFILE_FIELDS={'name','business_type','employees','sites','city_tier','budget','needs','must','language','current_tools','confirmed','gstin','udyam_number','udyam','legal_name','segment'}
GSTIN_PATTERN=re.compile(r'^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$')
UDYAM_PATTERN=re.compile(r'^UDYAM-?([A-Z]{2})-?(\d{2})-?(\d{7})$')

def canonical_need_key(value:str)->str:
 return ' '.join(re.sub(r'[_-]+',' ',value.casefold()).split())

def normalize_need_list(value:Any,field_name:str)->list[str]:
 if not isinstance(value,list):
  raise HTTPException(422,f'{field_name} must be a list of supported business needs.')
 lookup={}
 for need in ALLOWED_NEEDS:
  for term in (need,NEED_LABELS[need],*NEED_ALIASES[need]):
   lookup.setdefault(canonical_need_key(term),need)
 normalized=[];unknown=False
 for item in value:
  if not isinstance(item,str):unknown=True;continue
  for token in re.split(r'[,;]',item):
   key=canonical_need_key(token.strip())
   if not key:continue
   need=lookup.get(key)
   if need is None:unknown=True
   elif need not in normalized:normalized.append(need)
 if unknown:
  raise HTTPException(422,f'{field_name} includes an unsupported value. Choose from the catalogue priorities.')
 return normalized

def canonical_udyam(value:str)->str|None:
 compact=re.sub(r'\s+','',value.strip().upper())
 match=UDYAM_PATTERN.fullmatch(compact)
 return f"UDYAM-{match.group(1)}-{match.group(2)}-{match.group(3)}" if match else None

def mask_kyc_value(value:str|None)->str:
 if not value:return ''
 compact=re.sub(r'\s+','',value.strip().upper())
 if GSTIN_PATTERN.fullmatch(compact):return compact[:2]+'*'*9+compact[-4:]
 if canonical_udyam(compact):return 'UDYAM-**-**-*******'
 return '[identifier redacted]'

def validate_profile(profile:dict[str,Any])->None:
 """Reject malformed form values before they can drive quotes or KYC checks."""
 unknown=set(profile)-PROFILE_FIELDS
 if unknown:raise HTTPException(422,f"Unsupported profile field(s): {', '.join(sorted(unknown))}.")
 for key,maximum in (('name',120),('business_type',100),('current_tools',160),('legal_name',120),('segment',50)):
  value=profile.get(key)
  if value is not None and (not isinstance(value,str) or len(value.strip())>maximum):
   raise HTTPException(422,f"{key.replace('_',' ').capitalize()} must be text of at most {maximum} characters.")
  if isinstance(value,str):profile[key]=value.strip()
 confirmed=profile.get('confirmed') is True
 if not profile.get('business_type') and confirmed:
  raise HTTPException(422,'Business type is required before confirming the profile.')
 for key,minimum,maximum in (('employees',1,500),('sites',1,100),('budget',0,10000000)):
  value=profile.get(key)
  if value is None and not confirmed:continue
  if not isinstance(value,int) or isinstance(value,bool) or not minimum<=value<=maximum:
   raise HTTPException(422,f"{key.replace('_',' ').capitalize()} must be a whole number from {minimum} to {maximum}.")
 tier=profile.get('city_tier')
 if tier is None and not confirmed:pass
 elif not isinstance(tier,int) or isinstance(tier,bool) or tier not in (1,2):
  raise HTTPException(422,'City tier must be Tier 1 or Tier 2.')
 needs=normalize_need_list(profile.get('needs'),'Business priorities')
 if len(needs)>len(PROFILE_NEEDS):raise HTTPException(422,'Too many business priorities were selected.')
 profile['needs']=needs
 must=normalize_need_list(profile.get('must',[]),'Must-keep priorities')
 profile['must']=must
 if not set(profile['must']).issubset(profile['needs']):
  raise HTTPException(422,'Every must-keep item must also be selected as a business priority.')
 if profile.get('language') not in ('en','hi','mr','ta','te','kn','gu'):
  raise HTTPException(422,'Select one of the supported advisor languages.')
 if not isinstance(profile.get('confirmed'),bool):
  raise HTTPException(422,'Profile confirmation must be explicit.')
 gstin=profile.get('gstin','')
 if not isinstance(gstin,str) or (gstin and (not GSTIN_PATTERN.fullmatch(gstin.strip().upper()) or not gstin_valid(gstin.strip().upper()))):
  raise HTTPException(422,'Enter a valid 15-character GSTIN or leave it blank.')
 if isinstance(gstin,str):profile['gstin']=gstin.strip().upper()
 for key in ('udyam_number','udyam'):
  value=profile.get(key,'')
  if not isinstance(value,str):raise HTTPException(422,'Enter a valid Udyam number or leave it blank.')
  canonical=canonical_udyam(value) if value else ''
  if value and not canonical:
   raise HTTPException(422,'Enter a valid Udyam number or leave it blank.')
  if value and canonical:profile[key]=canonical

GUIDE_PROMPTS={
 'en':'Confirm the business type, employees, sites, tier, budget and priorities in the questionnaire.',
 'hi':'प्रश्नावली में व्यवसाय, कर्मचारी, स्थान, टियर, बजट और ज़रूरतें भरें।',
 'mr':'प्रश्नावलीत व्यवसाय, कर्मचारी, ठिकाणे, शहराचा स्तर, बजेट आणि गरजा नोंदवा.',
 'ta':'படிவத்தில் தொழில் வகை, ஊழியர்கள், கிளைகள், நகர வகை, செலவுத்திட்டம் மற்றும் தேவைகளை உறுதிப்படுத்தவும்.',
 'te':'ఫారంలో వ్యాపార రకం, ఉద్యోగులు, శాఖలు, నగర స్థాయి, బడ్జెట్ మరియు అవసరాలను నిర్ధారించండి.',
 'kn':'ಪ್ರಶ್ನಾವಳಿಯಲ್ಲಿ ಉದ್ಯಮ, ಉದ್ಯೋಗಿಗಳು, ಶಾಖೆಗಳು, ನಗರ ಶ್ರೇಣಿ, ಬಜೆಟ್ ಮತ್ತು ಅಗತ್ಯಗಳನ್ನು ದೃಢೀಕರಿಸಿ.',
 'gu':'પ્રશ્નાવલીમાં વ્યવસાય, કર્મચારીઓ, સ્થળો, શહેરનો વર્ગ, બજેટ અને જરૂરિયાતોની પુષ્ટિ કરો.'}

def guided(jid,x,text):
 cleaned=redact_chat(text)
 draft={}
 language=x.get('language','en')
 questions=languages.QUESTIONS.get(language,languages.QUESTIONS['en'])
 index=languages.missing_field(x['profile'])
 with con() as db:
  last=row(db,"SELECT content FROM advisor_messages WHERE journey_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(jid,))
 # A short answer is contextual only after that exact question was actually asked.
 if index is not None and last and last['content']==questions[index]:
  field=languages.FIELDS[index]
  if field in ('employees','sites','city_tier','budget') and cleaned.strip().isdigit():
   value=int(cleaned.strip())
   if value <= {'employees':500,'sites':100,'city_tier':2,'budget':10000000}[field] and value >= (0 if field=='budget' else 1):
    draft[field]=value
    cleaned+=' '+{'employees':'employees','sites':'sites','city_tier':'tier','budget':'budget'}[field]
    if field=='city_tier':cleaned+=' tier '+str(value)
  elif field=='business_type' and 1<len(cleaned.strip())<=90:draft[field]=cleaned.strip()
 for field,words in languages.ANCHORS.items():
  anchors='(?:'+'|'.join(words)+')'
  match=re.search(r'(\d+)\s*'+anchors+'|'+anchors+r'\s*(\d+)',cleaned)
  if match:draft[field]=int(match.group(1) or match.group(2))
 for key,pattern in [('employees',r'(\d+)\s*(?:employees?|staff|workers?|कर्मचारी)'),('sites',r'(\d+)\s*(?:sites?|branches|locations?|शाखा)'),('budget',r'(?:budget|INR|₹)\s*([\d,]+)')]:
  match=re.search(pattern,cleaned,re.I)
  if match:draft[key]=int(match.group(1).replace(',',''))
 for kind in ('clinic','pharmacy','retail','restaurant','manufacturing','bakery'):
  if explicitly_grounded('business_type',kind,cleaned):draft['business_type']=kind;break
 tier=re.search(r'tier\s*([12])',cleaned,re.I)
 if tier:draft['city_tier']=int(tier.group(1))
 draft['needs']=[n for n in ALLOWED_NEEDS if explicitly_grounded('needs',n,cleaned)]
 if not draft['needs']:draft.pop('needs')
 proposal=propose_profile(x['profile'],draft,source_text=cleaned).get('updates',{})
 profile=x['profile']
 if proposal:
  proposal['confirmed']=False
  profile=update_profile(jid,ProfileUpdate(profile=proposal))['profile']
 index=languages.missing_field(profile)
 result={'mode':'guided','model':None,'message':questions[index] if index is not None else GUIDE_PROMPTS.get(language,GUIDE_PROMPTS['en']),
 'updates':proposal,'profile':safe_profile(profile),'tools':[],'options':[]}
 if profile.get('confirmed'):
  result['options']=recommend(jid)['options']
  if result['options']:result['message']+=' '+result['options'][0]['label']+' · ₹'+str(result['options'][0]['recurring_paise']//100)+'/month.'
 with con() as db:
  db.executemany('INSERT INTO advisor_messages(journey_id,role,content,created_at) VALUES(?,?,?,?)',[(jid,'user',cleaned,now()),(jid,'assistant',result['message'],now())])
 return result

@app.post('/api/journeys/{jid}/advisor')
async def advisor(jid:str,b:TextInput):
 deadline=asyncio.get_running_loop().time()+17.5
 x=getjourney(jid)
 if re.search(r'\b(too expensive|too costly|price too high|cannot afford|over budget)\b',b.text,re.I):
  from .onboarding import auto_handoff
  with con(write=True) as db:auto_handoff(db,jid,'PRICE')
 if x['stage']=='ORDER_SUBMITTED':
  raise HTTPException(409,'This journey has a submitted order. Start a new journey for a separate advisor session.')
 local=await run_in_threadpool(local_runtime_status)
 if local['status']!='ready':return guided(jid,x,b.text)
 try:client=create_client()
 except Exception:return guided(jid,x,b.text)
 cleaned=redact_chat(b.text)
 with con() as db:
  history=[dict(r) for r in db.execute('SELECT role,content FROM advisor_messages WHERE journey_id=? ORDER BY id DESC LIMIT 10',(jid,)).fetchall()][::-1]
 profile=x['profile'];profile_proposal:dict[str,Any]={}
 def execute_tool(name:str,args:dict[str,Any])->dict[str,Any]:
  nonlocal profile,profile_proposal
  if name=='propose_profile_update':
   result=propose_profile(profile,args,source_text=cleaned);profile_proposal=result.get('updates',{});profile={**profile,**profile_proposal,'confirmed':False};return result
  if name=='search_catalogue':return catalogue_search(args.get('query',''),args.get('category'),args.get('segment'),args.get('max_results',5))
  if name=='preview_quote':
   items=args.get('items',[])
   if not isinstance(items,list) or not items or len(items)>12:raise ValueError('Select between 1 and 12 catalogue items.')
   quote=calculate(profile,items)
   quote['preview_only']=True;quote['summary']='Illustrative quote preview calculated by the deterministic eligibility and pricing engine.'
   return quote
  if name=='assess_journey_recovery':
   with con() as db:q=row(db,'SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC LIMIT 1',(jid,))
   if not q:return {'summary':'No quote exists in this journey yet. Build a bundle to preview options.','quote_found':False}
   items=parse(q['items']);issues=check_bundle(profile,items);budget=int(profile.get('budget') or 0)
   if budget and q['recurring_paise']>budget*100:issues=issues+[{'product_id':'*','code':'BUDGET','message':f"Latest quote exceeds the confirmed budget of ₹{budget:,}."}]
   alts=candidates(profile);recoveries=recovery_options(profile,items) if issues else []
   return {'summary':'Rechecked the latest quote against the current profile and listed validated, customer-selected recovery options.','quote_found':True,'quote_version':q['version'],'current_valid':not issues,'current_issues':issues,'valid_alternatives':alts[:3],'recovery_options':recoveries,'note':'Submitted orders are never rewritten. A recovery creates a new quote that needs fresh acceptance.'}
  raise ValueError('Unknown advisor tool.')
 try:
  result=await asyncio.wait_for(run_agent(client=client,profile={k:(redact_chat(v) if isinstance(v,str) else v) for k,v in safe_profile(profile).items()},language=x['language'],history=history,user_text=cleaned,tool_executor=execute_tool),timeout=max(0.05,deadline-asyncio.get_running_loop().time()))
 except TimeoutError:
  return guided(jid,x,cleaned)
 except Exception as exc:
  logging.exception('OneJourney local Ollama request failed')
  return guided(jid,x,cleaned)
 finally:
  await client.close()
 if profile_proposal:
  profile_proposal['confirmed']=False
  changed=update_profile(jid,ProfileUpdate(profile=profile_proposal))
  profile=changed['profile']
  if changed.get('invalidated'):result['journeyproof_invalidated']=changed['invalidated']
 result['updates']=profile_proposal;result['profile']=safe_profile(profile);result['mode']='local Ollama tool agent';result['disclosure']='Your message was processed by the local Ollama model. Catalogue pricing, eligibility, saved quotes and orders are controlled by deterministic backend logic.'
 with con() as db:
  db.executemany('INSERT INTO advisor_messages(journey_id,role,content,created_at) VALUES(?,?,?,?)',[(jid,'user',cleaned,now()),(jid,'assistant',result['message'][:6000],now())])
  db.execute('DELETE FROM advisor_messages WHERE journey_id=? AND id NOT IN (SELECT id FROM advisor_messages WHERE journey_id=? ORDER BY id DESC LIMIT 24)',(jid,jid))
 return result
@app.get('/api/journeys/{jid}/recommendations')
def recommend(jid:str):
 p=getjourney(jid)['profile'];options=[]
 if not p.get('confirmed'):
  raise HTTPException(409,'Confirm your business profile before recommendations')
 for c in candidates(p):
  calc=calculate(p,c['items']);budget=int(p.get('budget') or 0)
  unmet=unmet_needs(p,c['items']);tradeoff='Compare coverage, terms and price. Serviceability not confirmed.'
  if unmet:tradeoff='Not included: '+'; '.join(f"{item['need']} ({item['reason']})" for item in unmet)+'. '+tradeoff
  options.append({**c,**calc,'budget_fit':not budget or calc['recurring_paise']<=budget*100,'unmet_needs':unmet,'tradeoff':tradeoff})
 if 'internet' in (p.get('needs') or []):
  connected=[option for option in options if not any(item['need']=='internet' for item in option['unmet_needs'])]
  if connected:options=connected
 must=set(p.get('must') or [])
 options.sort(key=lambda option:(sum(item['need'] in must for item in option['unmet_needs']),len(option['unmet_needs']),not option['budget_fit'],option['setup_paise'],option['recurring_paise']))
 from .onboarding import auto_handoff
 if not options or all(o['unmet_needs'] for o in options):
  with con(write=True) as db:auto_handoff(db,jid,'NO_GSTIN' if not p.get('gstin') else 'INELIGIBLE')
 return {'options':options,'profile':public_profile(p),'pricing_disclosure':'Illustrative catalogue prices. The final quote shows the selected term, setup and GST.'}

@app.post('/api/quotes')
def quote(b:QuoteInput):
 with con(write=True) as db:
  p=parse(db.execute('SELECT profile FROM journeys WHERE id=?',(b.journey_id,)).fetchone()[0])
  if not p.get('confirmed'):raise HTTPException(409,'Confirm business profile before quotation')
  if row(db,'SELECT id FROM orders WHERE journey_id=?',(b.journey_id,)):raise HTTPException(409,'Submitted order is locked')
  c=calculate(p,b.items,b.term_months)
  fibre=any(it['product_id'] in ('P01','P02','P03') for it in c['items'])
  feasible=b.site_code!='SURVEY' and (b.site_code!='WIRELESS' or not fibre)
  if not feasible:
   c['valid']=False;c['issues'].append({'code':'SITE','product_id':'*','message':'Mock coverage check failed. Request a site survey or select a supported wireless service.'})
  qid=uid('quote');expiry=(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()
  terms={**c,'expires_at':expiry,'business_date':TODAY.isoformat(),'valid_until':(TODAY+timedelta(days=7)).isoformat(),'site_feasible':feasible,'site_code':b.site_code,'serviceability':'eligible in mock coverage' if feasible else 'site survey required'}
  ver=db.execute('SELECT COALESCE(MAX(version),0)+1 FROM quotes WHERE journey_id=?',(b.journey_id,)).fetchone()[0]
  db.execute('UPDATE quotes SET accepted=0 WHERE journey_id=?',(b.journey_id,))
  db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?,?)',(qid,b.journey_id,ver,j(b.items),c['recurring_paise'],c['setup_paise'],int(c['valid']),j(c['issues']),0,now()))
  db.execute('INSERT INTO quote_terms VALUES(?,?,?)',(qid,b.journey_id,j(terms)))
  db.execute('UPDATE journeys SET stage=? WHERE id=?',('QUOTE_DRAFT',b.journey_id))
  invalidate(db,b.journey_id,'New quote requires fresh verification')
  from .onboarding import auto_handoff
  if not c['valid']:auto_handoff(db,b.journey_id,'SITE_SURVEY' if not feasible else 'INELIGIBLE')
  elif p.get('budget') and c['recurring_paise']>int(p['budget'])*100:auto_handoff(db,b.journey_id,'PRICE')
  elif not p.get('gstin'):auto_handoff(db,b.journey_id,'NO_GSTIN')
 return {'quote_id':qid,'version':ver,**terms,'receipt':{'quote_version':ver,'eligibility':'valid' if c['valid'] else 'blocked','reasons':c['issues'],'customer_requirements':p.get('needs',[]),'confirmed_profile':True,'assumption':c['pricing_note']}}
@app.post('/api/quotes/{qid}/accept')
def accept(qid:str):
 with con(write=True) as c:
  q=row(c,'SELECT * FROM quotes WHERE id=?',(qid,))
  if not q:raise HTTPException(404,'Quote not found')
  terms=row(c,'SELECT payload FROM quote_terms WHERE quote_id=?',(qid,))
  if not terms or parse(terms['payload'])['expires_at']<=now() or parse(terms['payload']).get('valid_until','9999')<TODAY.isoformat():raise HTTPException(409,'Quote expired. Generate a new quote.')
  if not q['valid']:raise HTTPException(409,'Invalid quote cannot be accepted')
  if row(c,'SELECT id FROM orders WHERE journey_id=?',(q['journey_id'],)):
   raise HTTPException(409,'Order already submitted')
  p=getjourney(q['journey_id'])['profile'];errors=check_bundle(p,parse(q['items']))
  if errors:raise HTTPException(409,{'message':'Customer facts changed; create a new quote','issues':errors})
  latest=row(c,'SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC',(q['journey_id'],))
  if latest['id']!=qid:raise HTTPException(409,'Superseded quote cannot be accepted')
  c.execute('UPDATE quotes SET accepted=1 WHERE id=?',(qid,))
  c.execute('UPDATE journeys SET stage=? WHERE id=?',('QUOTE_ACCEPTED',q['journey_id']))
 return {'accepted':True,'quote_id':qid,'version':q['version']}

from .onboarding import router as onboarding_router
from .fulfilment import router as fulfilment_router
app.include_router(onboarding_router)
app.include_router(fulfilment_router)
