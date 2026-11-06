"""OneJourney hackathon prototype. All customer data/registries fictional. No production telecom/payment integrations."""
from __future__ import annotations
import csv, io, json, logging, os, re, sqlite3, uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from PIL import Image, ImageDraw, ImageFont
import pytesseract
from .genai import api_key_configured, configured_model, create_client, reasoning_effort, run_agent

try:
 from dotenv import load_dotenv
 load_dotenv(Path(__file__).resolve().parent.parent / '.env')
except ImportError:
 pass

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DB = Path(os.environ.get('ONEJOURNEY_DB', str(ROOT.parent/'onejourney.sqlite3')))
PRODUCTS = {r['product_id']:r for r in csv.DictReader((DATA/'product_catalogue.csv').open())}
GST = {r['gstin'].upper():r for r in csv.DictReader((DATA/'mock_gstin_registry.csv').open())}
UDYAM = {r['udyam_number'].upper():r for r in csv.DictReader((DATA/'mock_udyam_registry.csv').open())}
LEADS = list(csv.DictReader((DATA/'smb_leads.csv').open()))
DEMO_GST = next(x for x in GST.values() if x['status']=='Active')
DEMO_UDY = next((x for x in UDYAM.values() if x['enterprise_name'].strip().lower()==DEMO_GST['legal_name'].strip().lower()), next(iter(UDYAM.values())))
app=FastAPI(title='OneJourney', version='1.0.0', description='Competition prototype. Synthetic registries and simulated telecom activation.')
app.mount('/static', StaticFiles(directory=str(ROOT/'static')), name='static')

def con():
 c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init():
 DB.parent.mkdir(parents=True, exist_ok=True)
 with con() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS journeys (id TEXT PRIMARY KEY, language TEXT, profile TEXT, stage TEXT, created_at TEXT, source TEXT DEFAULT 'Digital self-service');
  CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, journey_id TEXT, version INTEGER, items TEXT, recurring_paise INTEGER, setup_paise INTEGER, valid INTEGER, issues TEXT, accepted INTEGER DEFAULT 0, created_at TEXT);
  CREATE TABLE IF NOT EXISTS consents (id TEXT PRIMARY KEY, journey_id TEXT, purpose TEXT, version TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, journey_id TEXT, filename TEXT, extracted TEXT, confirmed INTEGER DEFAULT 0, created_at TEXT);
  CREATE TABLE IF NOT EXISTS kyc (journey_id TEXT PRIMARY KEY, status TEXT, reasons TEXT, gstin TEXT, udyam TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, journey_id TEXT, quote_id TEXT, status TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS order_events (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, status TEXT, details TEXT, simulated INTEGER, created_at TEXT);
  CREATE TABLE IF NOT EXISTS handoffs (id TEXT PRIMARY KEY, journey_id TEXT, reason TEXT, permission INTEGER, source TEXT, status TEXT, created_at TEXT);
  CREATE TABLE IF NOT EXISTS advisor_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, journey_id TEXT, role TEXT, content TEXT, created_at TEXT);
  ''')
init()

def now(): return datetime.now(timezone.utc).isoformat()
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

def calculate(profile,items):
 issues=check_bundle(profile,items);lines=[];rec=setup=0
 grouped={}
 for it in items:
  pid=str(it.get('product_id','')).upper()
  if pid in CAT:
   try:q=int(it.get('quantity',1))
   except:q=0
   if q>0:grouped[pid]=grouped.get(pid,0)+q
 for pid,q in grouped.items():
  p=CAT[pid];unit=price_paise(p);s=money(p['one_time_setup_inr'])*q
  lines.append({'product_id':pid,'name':p['product_name'],'quantity':q,'unit':p['unit'],'unit_price_paise':unit,'monthly_paise':q*unit,'setup_paise':s,'term_months':p['min_contract_months'],'install_days':p['typical_install_days']})
  rec+=q*unit;setup+=s
 return {'items':lines,'recurring_paise':rec,'setup_paise':setup,'issues':issues,'valid':not issues,'currency':'INR','pricing_note':'Illustrative midpoint of synthetic catalogue ranges; setup charged per unit; not an operator tariff'}

def getjourney(jid):
 with con() as c:
  r=row(c,'SELECT * FROM journeys WHERE id=?',(jid,))
 if not r:raise HTTPException(404,'Journey not found')
 r['profile']=parse(r['profile']);return r

class CreateJourney(BaseModel):
 language:str='hi';source:str='Digital self-service'
class ProfileUpdate(BaseModel):
 profile:dict[str,Any]
class TextInput(BaseModel):
 text:str=Field(min_length=1,max_length=5000)
class QuoteInput(BaseModel):
 journey_id:str;items:list[dict[str,Any]]
class QuoteAccept(BaseModel):
 quote_id:str
class VerifyKyc(BaseModel):
 journey_id:str;gstin:str='';udyam_number:str='';legal_name:str=''
class OrderInput(BaseModel):
 journey_id:str;quote_id:str;payment_confirmed:bool=False
class OpsInput(BaseModel):
 status:str;details:str=''
class HandoffInput(BaseModel):
 journey_id:str;reason:str;permission:bool=False

@app.get('/')
def index():return FileResponse(ROOT/'static'/'index.html')
@app.get('/api/health')
def health():return {'ok':True,'products':len(CAT),'leads':len(LEADS),'mock_gstin':len(GST),'mock_udyam':len(UDYAM),'advisor':{'provider':'OpenAI Responses API','status':'live' if api_key_configured() else 'needs_api_key','model':configured_model(),'reasoning_effort':reasoning_effort()}}
@app.get('/api/catalogue')
def catalogue():return list(CAT.values())
@app.get('/api/demo')
def demo():return {'gstin':DEMO_GST['gstin'],'legal_name':DEMO_GST['legal_name'],'udyam':DEMO_UDY['udyam_number'],'city_tier':2,'employees':6,'sites':1,'segment':'prof','budget':4500,'business_type':'Clinic','name':DEMO_GST['trade_name']}
@app.get('/api/demo/document')
def demo_document():
 im=Image.new('RGB',(1800,760),'white');draw=ImageDraw.Draw(im);font=ImageFont.load_default(size=48)
 text='\n'.join([
  'FICTIONAL SAMPLE - NOT A REAL CERTIFICATE',
  'BUSINESS REGISTRATION DEMONSTRATION',
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
def metrics():
 stages=Counter(x['stage_reached'] for x in LEADS);activated=[x for x in LEADS if x.get('outcome')=='Activated']
 return {'leads':len(LEADS),'activated':len(activated),'activation_pct':round(len(activated)/len(LEADS)*100,2),'quote_accepted':861,'needs_assessed':1974,'quote_accept_pct':round(861/1974*100,2),'source':'Official synthetic challenge records. Not real Airtel performance.'}

@app.post('/api/journeys')
def newjourney(b:CreateJourney):
 i=uid('journey');p={'business_type':'Clinic','employees':6,'sites':1,'city_tier':2,'segment':'prof','budget':4500,'gstin':'','udyam_number':'','needs':['internet','email'],'confirmed':False,'language':b.language,'current_tools':'Mobile hotspot'}
 with con() as c:c.execute('INSERT INTO journeys(id,language,profile,stage,created_at,source) VALUES (?,?,?,?,?,?)',(i,b.language,j(p),'DISCOVERY',now(),b.source))
 return {'id':i,'profile':p,'stage':'DISCOVERY'}
@app.get('/api/journeys/{jid}')
def journey(jid:str):
 r=getjourney(jid)
 with con() as c:
  quotes=[dict(x) for x in c.execute('SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC',(jid,)).fetchall()]
  k=row(c,'SELECT * FROM kyc WHERE journey_id=?',(jid,));orders=[dict(x) for x in c.execute('SELECT * FROM orders WHERE journey_id=? ORDER BY created_at DESC',(jid,))]
 for q in quotes:q['issues']=parse(q['issues']);q['items']=parse(q['items'])
 if k:k['reasons']=parse(k['reasons'])
 return {**r,'quotes':quotes,'kyc':k,'orders':orders}
@app.patch('/api/journeys/{jid}/profile')
def update_profile(jid:str,b:ProfileUpdate):
 x=getjourney(jid);p={**x['profile'],**b.profile}
 for n in ['employees','sites','city_tier']:
  try:p[n]=max(1,int(p.get(n,1)))
  except:p[n]=1
 with con() as c:
  if row(c,'SELECT id FROM orders WHERE journey_id=?',(jid,)):
   raise HTTPException(409,'Order already submitted. Changes require an assisted order amendment, not quote rewriting.')
  c.execute('UPDATE journeys SET profile=?,stage=? WHERE id=?',(j(p),'PROFILE_CONFIRMED' if p.get('confirmed') else 'DISCOVERY',jid))
  last=row(c,'SELECT * FROM quotes WHERE journey_id=? AND accepted=1 ORDER BY version DESC',(jid,))
  invalidated=[]
  if last:
   issues=check_bundle(p,parse(last['items']))
   if issues:
    c.execute('UPDATE quotes SET accepted=0 WHERE id=?',(last['id'],))
    invalidated=issues
 return {'profile':p,'invalidated':invalidated,'notice':'Changed profile rechecked against accepted quote. Any invalid accepted quote was revoked.'}

NEED_SKUS={'internet':['P02','P01','P04'],'backup':['P09'],'email':['P16'],'security':['P20'],'calling':['P12'],'mobile':['P07'],'website':['P18'],'cctv':['P23']}
def candidates(profile):
 wants=list(profile.get('needs') or ['internet']);main=[];alts=[]
 for need in wants:
  for pid in NEED_SKUS.get(need,[]):
   if pid not in main:
    p=CAT[pid]; qty=(int(profile.get('employees') or 1) if pid=='P16' else min(3,int(profile.get('employees') or 1)) if pid=='P20' else 1)
    main.append({'product_id':pid,'quantity':qty});break
 # dependency closure, not to permit disallowed products
 ids={x['product_id'] for x in main}
 for itm in list(main):
  p=CAT[itm['product_id']]
  if p['requires_one_of'] and not any(x in ids for x in p['requires_one_of']):
   for option in ['P01','P02','P04']:
    if option in p['requires_one_of']:
     main.insert(0,{'product_id':option,'quantity':1});ids.add(option);break
 # prune invalid one SKU at a time, preserve mandatory dependencies when possible
 def clean(items):
  items=list(items)
  for _ in range(15):
   errs=check_bundle(profile,items)
   if not errs:break
   rem=errs[0]['product_id'];items=[i for i in items if i['product_id']!=rem]
  return items
 main=clean(main)
 if not main:
  for pid in ['P01','P04','P07','P18']:
   t=[{'product_id':pid,'quantity':1}]
   if not check_bundle(profile,t):main=t;break
 # options: always only fully validated, avoid duplicate copies
 choices=[]
 def add(items,label):
  if not items or check_bundle(profile,items):return
  key=tuple(sorted((i['product_id'],i['quantity']) for i in items))
  if key not in [c['_key'] for c in choices]:choices.append({'label':label,'items':items,'_key':key})
 add(main,'Recommended fit')
 cheaper=sorted(main,key=lambda x:price_paise(CAT[x['product_id']])*x['quantity'],reverse=True)
 if len(cheaper)>1:
  for cut in cheaper:
   reduced=clean([x for x in main if x['product_id']!=cut['product_id']]);add(reduced,'Lower-cost alternative')
   if len(choices)>=3:break
 if len(choices)<3 and any(x['product_id'] in ('P02','P01') for x in main):
  alt=[{'product_id':'P04','quantity':1} if i['product_id'] in ('P01','P02') else i for i in main];add(clean(alt),'Simpler connectivity')
 return [{k:v for k,v in c.items() if k!='_key'} for c in choices]

def redact_chat(text:str)->str:
 """Remove common direct identifiers before chat is sent to or saved for the model."""
 text=re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b','[email removed]',text,flags=re.I)
 text=re.sub(r'\bUDYAM-[A-Z]{2}-\d{2}-\d{7}\b','[Udyam ID removed]',text,flags=re.I)
 text=re.sub(r'\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b','[GSTIN removed]',text,flags=re.I)
 text=re.sub(r'(?<!\w)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\w)','[phone removed]',text)
 return text[:5000]

def safe_profile(profile:dict[str,Any])->dict[str,Any]:
 return {k:profile.get(k) for k in ('business_type','employees','sites','city_tier','budget','needs','language','confirmed') if k in profile}

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

def analyze_leads(segment:str|None)->dict[str,Any]:
 cohort=LEADS
 if segment:
  needle=segment.casefold()
  cohort=[r for r in LEADS if needle in r.get('segment','').casefold() or needle in r.get('business_type','').casefold()]
 if not cohort:return {'summary':'No records matched that segment.','lead_count':0,'source':'supplied synthetic challenge dataset'}
 activated=sum(r.get('outcome')=='Activated' for r in cohort)
 drops=Counter(r.get('drop_reason') for r in cohort if r.get('outcome')!='Activated' and r.get('drop_reason'))
 stages=Counter(r.get('stage_reached') for r in cohort)
 durations=[int(r['days_lead_to_activation']) for r in cohort if r.get('days_lead_to_activation','').isdigit()]
 calls=[int(r['customer_follow_up_calls']) for r in cohort if r.get('customer_follow_up_calls','').isdigit()]
 sources=Counter(r.get('lead_source','') for r in cohort if r.get('lead_source'))
 products=Counter(pid for r in cohort for pid in re.findall(r'P\d{2}',r.get('products_of_interest','')))
 pct=round(activated*100/len(cohort),1)
 return {'summary':f"Analyzed {len(cohort)} synthetic lead records; {activated} activated ({pct}%).",'lead_count':len(cohort),'activated':activated,'activation_pct':pct,'average_days_to_activation':round(sum(durations)/len(durations),1) if durations else None,'average_follow_up_calls':round(sum(calls)/len(calls),1) if calls else None,'stage_counts':dict(stages),'top_drop_reasons':[{'reason':k,'count':v} for k,v in drops.most_common(5)],'lead_sources':dict(sources),'top_products_of_interest':[{'product_id':k,'count':v} for k,v in products.most_common(5)],'source':'supplied synthetic challenge dataset; not real Airtel performance'}

def propose_profile(profile:dict[str,Any],args:dict[str,Any])->dict[str,Any]:
 updated={**profile};changes={}
 for key,limit in (('employees',500),('sites',100),('budget',10000000)):
  value=args.get(key)
  if isinstance(value,int) and not isinstance(value,bool) and (1<=value<=limit if key!='budget' else 0<=value<=limit):changes[key]=value
 for key in ('business_type','current_tools'):
  value=args.get(key)
  if isinstance(value,str) and value.strip():changes[key]=redact_chat(value.strip())[:100]
 tier=args.get('city_tier')
 if tier in (1,2):changes['city_tier']=tier
 lang=args.get('language')
 if lang in ('en','hi','mr'):changes['language']=lang
 needs=args.get('needs')
 if isinstance(needs,list):changes['needs']=sorted(set(updated.get('needs',[]))|{x for x in needs if x in {'internet','backup','email','security','calling','mobile','website','cctv'}})
 updated.update(changes)
 updated['confirmed']=False
 return {'summary':'Captured explicit business details as unconfirmed suggestions.' if changes else 'No new profile facts were stated clearly enough to capture.','updates':changes,'profile':safe_profile(updated)}

@app.post('/api/journeys/{jid}/advisor')
async def advisor(jid:str,b:TextInput):
 x=getjourney(jid)
 if x['stage']=='ORDER_SUBMITTED':
  raise HTTPException(409,'This journey has a submitted order. Start a new journey for a separate advisor session.')
 try:client=create_client()
 except Exception as exc:
  logging.exception('OneJourney GenAI client could not be initialized')
  raise HTTPException(503,'The GenAI advisor dependencies are unavailable. Install the project requirements and restart the app.') from exc
 if client is None:
  raise HTTPException(503,'Live OneJourney GenAI is not configured. Set OPENAI_API_KEY on the server and restart the app.')
 cleaned=redact_chat(b.text)
 with con() as db:
  history=[dict(r) for r in db.execute('SELECT role,content FROM advisor_messages WHERE journey_id=? ORDER BY id DESC LIMIT 10',(jid,)).fetchall()][::-1]
 profile=x['profile'];profile_proposal:dict[str,Any]={}
 def execute_tool(name:str,args:dict[str,Any])->dict[str,Any]:
  nonlocal profile,profile_proposal
  if name=='propose_profile_update':
   result=propose_profile(profile,args);profile_proposal=result.get('updates',{});profile={**profile,**profile_proposal,'confirmed':False};return result
  if name=='search_catalogue':return catalogue_search(args.get('query',''),args.get('category'),args.get('segment'),args.get('max_results',5))
  if name=='preview_quote':
   items=args.get('items',[])
   if not isinstance(items,list) or not items or len(items)>12:raise ValueError('Select between 1 and 12 catalogue items.')
   quote=calculate(profile,items)
   quote['preview_only']=True;quote['summary']='Illustrative quote preview calculated by the deterministic eligibility and pricing engine.'
   return quote
  if name=='analyze_funnel':return analyze_leads(args.get('segment'))
  if name=='assess_journey_recovery':
   with con() as db:q=row(db,'SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC LIMIT 1',(jid,))
   if not q:return {'summary':'No quote exists in this journey yet. Build a bundle to preview options.','quote_found':False}
   items=parse(q['items']);issues=check_bundle(profile,items);alts=candidates(profile)
   return {'summary':'Rechecked the latest quote against the current profile and listed currently valid alternatives.','quote_found':True,'quote_version':q['version'],'current_valid':not issues,'current_issues':issues,'valid_alternatives':alts[:3],'note':'Submitted orders are never rewritten. Create a new quote to proceed.'}
  raise ValueError('Unknown advisor tool.')
 try:
  result=await run_agent(client=client,profile=profile,language=x['language'],history=history,user_text=cleaned,tool_executor=execute_tool)
 except Exception as exc:
  logging.exception('OneJourney Responses API request failed')
  raise HTTPException(502,'The GenAI advisor could not complete this turn. Check server configuration and try again.') from exc
 if profile_proposal:
  profile_proposal['confirmed']=False
  changed=update_profile(jid,ProfileUpdate(profile=profile_proposal))
  profile=changed['profile']
  if changed.get('invalidated'):result['journeyproof_invalidated']=changed['invalidated']
 result['updates']=profile_proposal;result['profile']=safe_profile(profile);result['mode']='live GenAI tool agent';result['disclosure']='GenAI conversation and tools are live. Catalogue pricing, eligibility, saved quotes and orders are controlled by deterministic backend logic.'
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
  options.append({**c,**calc,'budget_fit':not budget or calc['recurring_paise']<=budget*100,'tradeoff':'Compare coverage, terms and price. Serviceability not confirmed.'})
 return {'options':options,'profile':p,'pricing_disclosure':'Midpoint demo policy; prices are illustrative only.'}

@app.post('/api/quotes')
def quote(b:QuoteInput):
 p=getjourney(b.journey_id)['profile']
 if not p.get('confirmed'):raise HTTPException(409,'Profile must be confirmed before quotation')
 with con() as db:
  if row(db,'SELECT id FROM orders WHERE journey_id=?',(b.journey_id,)):
   raise HTTPException(409,'Submitted orders cannot be revised by creating a new quote')
 c=calculate(p,b.items);qid=uid('quote')
 with con() as db:
  ver=db.execute('SELECT COALESCE(MAX(version),0)+1 FROM quotes WHERE journey_id=?',(b.journey_id,)).fetchone()[0]
  db.execute('UPDATE quotes SET accepted=0 WHERE journey_id=?',(b.journey_id,))
  db.execute('INSERT INTO quotes VALUES(?,?,?,?,?,?,?,?,?,?)',(qid,b.journey_id,ver,j(b.items),c['recurring_paise'],c['setup_paise'],int(c['valid']),j(c['issues']),0,now()))
  db.execute('UPDATE journeys SET stage=? WHERE id=?',('QUOTE_DRAFT',b.journey_id))
 return {'quote_id':qid,'version':ver,**c,'receipt':{'quote_version':ver,'eligibility':'valid' if c['valid'] else 'blocked','reasons':c['issues'],'customer_requirements':p.get('needs',[]),'confirmed_profile':p.get('confirmed',False),'assumption':'All pricing uses fixed synthetic catalogue midpoint; site qualification pending'}}
@app.post('/api/quotes/{qid}/accept')
def accept(qid:str):
 with con() as c:
  q=row(c,'SELECT * FROM quotes WHERE id=?',(qid,))
  if not q:raise HTTPException(404,'Quote not found')
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

@app.post('/api/kyc/consent')
def consent(journey_id:str=Form(...),accepted:bool=Form(...)):
 getjourney(journey_id)
 if not accepted:raise HTTPException(400,'Explicit consent required')
 with con() as c:c.execute('INSERT INTO consents VALUES(?,?,?,?,?)',(uid('consent'),journey_id,'Business onboarding and eligibility verification','v1',now()))
 return {'consented':True,'notice':'Synthetic mock registry checks; document held locally for prototype; no live government verification.'}
@app.post('/api/kyc/documents')
async def document(journey_id:str=Form(...),file:UploadFile=File(...)):
 getjourney(journey_id)
 with con() as c:
  if not row(c,'SELECT * FROM consents WHERE journey_id=? ORDER BY created_at DESC',(journey_id,)):raise HTTPException(403,'Consent required before document upload')
 raw=await file.read(4*1024*1024+1)
 if len(raw)>4*1024*1024:raise HTTPException(413,'4 MB max image')
 try:
  im=Image.open(io.BytesIO(raw));im.verify();im=Image.open(io.BytesIO(raw)).convert('RGB');
  if im.width*im.height>20_000_000:raise ValueError('Image too large')
  im.thumbnail((2200,2200))
 except:raise HTTPException(415,'Only valid image files accepted')
 try:text=pytesseract.image_to_string(im,config='--psm 6')
 except Exception as e:raise HTTPException(503,'OCR engine unavailable: '+str(e))
 extracted={'text_preview':text[:1200],'gstin_candidates':re.findall(r'\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z0-9]Z[A-Z0-9]\b',text.upper()),'udyam_candidates':re.findall(r'UDYAM[- ]?[A-Z]{2}[- ]?\d{2}[- ]?\d{7}',text.upper()),'ocr_engine':'Tesseract','requires_user_review':True}
 id=uid('doc')
 with con() as c:c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?)',(id,journey_id,re.sub(r'[^a-zA-Z0-9._-]','_',file.filename or 'image'),j(extracted),0,now()))
 return {'document_id':id,'extracted':extracted,'notice':'Actual local OCR, not proof of identity. Verify against the mock registry.'}
@app.post('/api/kyc/documents/{did}/confirm')
def confirm_doc(did:str):
 with con() as c:
  if not row(c,'SELECT * FROM documents WHERE id=?',(did,)):raise HTTPException(404,'Document missing')
  c.execute('UPDATE documents SET confirmed=1 WHERE id=?',(did,))
 return {'confirmed':True}
@app.post('/api/kyc/verify')
def verify(b:VerifyKyc):
 x=getjourney(b.journey_id);p=x['profile'];reasons=[];g=GST.get(b.gstin.upper().strip());u=UDYAM.get(b.udyam_number.upper().strip()) if b.udyam_number.strip() else None
 with con() as c:
  if not row(c,'SELECT * FROM consents WHERE journey_id=?',(b.journey_id,)):reasons.append('Consent missing')
  ds=[dict(r) for r in c.execute('SELECT * FROM documents WHERE journey_id=?',(b.journey_id,)).fetchall()]
  if not ds:reasons.append('Document upload required')
  elif not any(d['confirmed'] for d in ds):reasons.append('OCR result requires user confirmation')
  if b.gstin.strip():
   if not g:reasons.append('GSTIN not found in synthetic registry')
   elif g['status']!='Active':reasons.append('GSTIN status: '+g['status'])
   if g and b.legal_name.strip() and b.legal_name.strip().casefold() not in (g['legal_name'].casefold(),g['trade_name'].casefold()):reasons.append('Business name differs from GST registry')
  if b.udyam_number.strip() and not u:reasons.append('Udyam number not found in mock registry')
  if u and g and u['enterprise_name'].strip().casefold()!=g['legal_name'].strip().casefold():
   reasons.append('GSTIN and Udyam registry business names do not match')
  if ds and any(d['confirmed'] for d in ds):
   confirmed_docs=[parse(d['extracted']) for d in ds if d['confirmed']]
   recognized={v.upper() for d in confirmed_docs for v in d.get('gstin_candidates',[])+d.get('udyam_candidates',[])}
   if not recognized.intersection({b.gstin.upper().strip(),b.udyam_number.upper().strip()}-{''}):
    reasons.append('Uploaded document does not contain a matching extracted registration identifier; human review required')
  quote=row(c,'SELECT * FROM quotes WHERE journey_id=? AND accepted=1 ORDER BY version DESC',(b.journey_id,))
  if quote:
   selected=[x['product_id'] for x in parse(quote['items'])]
   if any(CAT[pid]['requires_gstin'] for pid in selected if pid in CAT) and (not g or g['status']!='Active'):reasons.append('Selected product requires active GSTIN')
  status=('REVIEW_REQUIRED' if any('human review' in reason for reason in reasons) else 'ACTION_REQUIRED') if reasons else 'VERIFIED';newp={**p,'gstin':b.gstin.upper().strip(),'udyam_number':b.udyam_number.upper().strip()}
  # Only save registry-backed GSTIN as an active eligibility fact.
  c.execute('UPDATE journeys SET profile=? WHERE id=?',(j(newp),b.journey_id))
  c.execute('INSERT OR REPLACE INTO kyc VALUES(?,?,?,?,?,?)',(b.journey_id,status,j(list(dict.fromkeys(reasons))),b.gstin.upper().strip(),b.udyam_number.upper().strip(),now()))
 return {'status':status,'reasons':list(dict.fromkeys(reasons)),'registry':{'gstin_status':g['status'] if g else 'not_found','legal_name':g['legal_name'] if g else None,'udyam_found':bool(u)},'fixture_date':'2026-07-15'}

@app.post('/api/orders')
def createorder(b:OrderInput):
 x=getjourney(b.journey_id)
 if not b.payment_confirmed:raise HTTPException(400,'Simulated checkout authorization required')
 with con() as c:
  q=row(c,'SELECT * FROM quotes WHERE id=? AND journey_id=?',(b.quote_id,b.journey_id))
  if not q or not q['accepted']:raise HTTPException(409,'Current accepted quote required')
  if not x['profile'].get('confirmed'):raise HTTPException(409,'Business profile must be confirmed')
  latest=row(c,'SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC',(b.journey_id,))
  if latest['id']!=q['id'] or not q['valid']:raise HTTPException(409,'Quote superseded or invalid')
  issues=check_bundle(x['profile'],parse(q['items']))
  if issues:raise HTTPException(409,{'message':'Quote no longer eligible','issues':issues})
  k=row(c,'SELECT * FROM kyc WHERE journey_id=?',(b.journey_id,))
  if not k or k['status']!='VERIFIED':raise HTTPException(409,'KYC not verified')
  existing=row(c,'SELECT * FROM orders WHERE quote_id=?',(b.quote_id,))
  if existing:return {'order_id':existing['id'],'status':existing['status'],'idempotent':True}
  oid=uid('order');c.execute('INSERT INTO orders VALUES(?,?,?,?,?)',(oid,b.journey_id,b.quote_id,'ORDER_PLACED',now()))
  c.execute('INSERT INTO order_events(order_id,status,details,simulated,created_at) VALUES(?,?,?,?,?)',(oid,'ORDER_PLACED','Order placed through simulated checkout',1,now()))
  c.execute('UPDATE journeys SET stage=? WHERE id=?',('ORDER_SUBMITTED',b.journey_id))
 return {'order_id':oid,'status':'ORDER_PLACED','simulated':True}
@app.get('/api/orders/{oid}')
def getorder(oid:str):
 with con() as c:
  order=row(c,'SELECT * FROM orders WHERE id=?',(oid,))
  if not order:raise HTTPException(404,'Order not found')
  events=[dict(r) for r in c.execute('SELECT * FROM order_events WHERE order_id=? ORDER BY id',(oid,))]
 return {**order,'events':events,'notice':'Telecom provisioning events are simulated. No Airtel network was changed.'}
TRANS={'ORDER_PLACED':{'SERVICEABILITY_PENDING','ON_HOLD','CANCELLED'},'SERVICEABILITY_PENDING':{'INSTALLATION_SCHEDULED','ON_HOLD','FAILED'},'INSTALLATION_SCHEDULED':{'PROVISIONING','ON_HOLD','CANCELLED'},'PROVISIONING':{'ACTIVATED','ON_HOLD','FAILED'},'ON_HOLD':{'SERVICEABILITY_PENDING','INSTALLATION_SCHEDULED','CANCELLED'},'ACTIVATED':set(),'FAILED':set(),'CANCELLED':set()}
@app.post('/api/mock/ops/orders/{oid}/events')
def event(oid:str,b:OpsInput):
 with con() as c:
  o=row(c,'SELECT * FROM orders WHERE id=?',(oid,))
  if not o:raise HTTPException(404,'Order missing')
  if b.status not in TRANS.get(o['status'],set()):raise HTTPException(409,f"Invalid transition {o['status']} to {b.status}")
  c.execute('UPDATE orders SET status=? WHERE id=?',(b.status,oid))
  c.execute('INSERT INTO order_events(order_id,status,details,simulated,created_at) VALUES(?,?,?,?,?)',(oid,b.status,b.details or 'Synthetic operations update',1,now()))
 return {'status':b.status,'simulated':True}
@app.post('/api/handoffs')
def handoff(b:HandoffInput):
 x=getjourney(b.journey_id)
 if not b.permission:raise HTTPException(403,'Customer authorization is required for partner sharing')
 with con() as c:c.execute('INSERT INTO handoffs VALUES(?,?,?,?,?,?,?)',(uid('handoff'),b.journey_id,b.reason,1,x['source'],'NEW',now()))
 return {'queued':True,'source_attribution':x['source']}
@app.get('/api/partners/handoffs')
def handoffs():
 with con() as c: return [dict(r) for r in c.execute('SELECT id,reason,source,status,created_at FROM handoffs ORDER BY created_at DESC LIMIT 30')]
