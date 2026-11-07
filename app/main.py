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
from .genai import configured_model, create_client, local_runtime_status, reasoning_effort, run_agent
from .kyc_pipeline import analyse as analyse_kyc_document, gstin_valid
from .needs import ALLOWED_NEEDS, NEED_ALIASES, NEED_LABELS, NEED_SKUS
from .recommender import build_candidates

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
  CREATE TABLE IF NOT EXISTS receipts (quote_id TEXT PRIMARY KEY, receipt TEXT, created_at TEXT);
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
 language:str=Field(default='hi',pattern='^(en|hi|mr)$');source:str='Digital self-service'
class ProfileUpdate(BaseModel):
 profile:dict[str,Any]
class TextInput(BaseModel):
 text:str=Field(min_length=1,max_length=5000)
class QuoteInput(BaseModel):
 journey_id:str;items:list[dict[str,Any]]
class QuoteAccept(BaseModel):
 quote_id:str
class VerifyKyc(BaseModel):
 journey_id:str;gstin:str=Field(default='',max_length=32);udyam_number:str=Field(default='',max_length=40);legal_name:str=Field(default='',max_length=120);require_all_documents:bool=False
class OrderInput(BaseModel):
 journey_id:str;quote_id:str;payment_confirmed:bool=False
class OpsInput(BaseModel):
 status:str;details:str=''
class HandoffInput(BaseModel):
 journey_id:str;reason:str;permission:bool=False

@app.get('/')
def index():return FileResponse(ROOT/'static'/'index.html')
@app.get('/privacy')
def privacy():return FileResponse(ROOT/'static'/'privacy.html')
@app.get('/api/health')
def health():
 local=local_runtime_status()
 return {'ok':True,'products':len(CAT),'leads':len(LEADS),'mock_gstin':len(GST),'mock_udyam':len(UDYAM),'advisor':{'provider':'Ollama local','status':local['status'],'model':configured_model(),'reasoning_effort':reasoning_effort()}}
@app.get('/api/catalogue')
def catalogue():return list(CAT.values())
@app.get('/api/needs')
def need_catalogue():
 return [{'id':need,'label':NEED_LABELS[need],'aliases':NEED_ALIASES[need]} for need in ALLOWED_NEEDS]
@app.get('/api/demo')
def demo():return {'gstin':DEMO_GST['gstin'],'legal_name':DEMO_GST['legal_name'],'udyam':DEMO_UDY['udyam_number'],'city_tier':2,'employees':6,'sites':1,'segment':'prof','budget':4500,'business_type':'Clinic','name':DEMO_GST['trade_name']}
@app.get('/api/demo/document')
def demo_document():
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
def metrics():
 stages=Counter(x['stage_reached'] for x in LEADS);activated=[x for x in LEADS if x.get('outcome')=='Activated']
 rank={'Lead':0,'Contacted':1,'Needs assessed':2,'Quote accepted':3,'KYC complete':4,'Activated':5}
 needs_assessed=sum(rank.get(x['stage_reached'],0)>=2 for x in LEADS)
 quote_accepted=sum(rank.get(x['stage_reached'],0)>=3 for x in LEADS)
 return {'leads':len(LEADS),'activated':len(activated),'activation_pct':round(len(activated)/len(LEADS)*100,2),'quote_accepted':quote_accepted,'needs_assessed':needs_assessed,'quote_accept_pct':round(quote_accepted/needs_assessed*100,2) if needs_assessed else 0,'source':'Computed from supplied synthetic challenge records. Not real Airtel performance.'}

@app.post('/api/journeys')
def newjourney(b:CreateJourney):
 i=uid('journey');p={'business_type':'','employees':None,'sites':None,'city_tier':None,'segment':'prof','budget':None,'gstin':'','udyam_number':'','needs':[],'must':[],'confirmed':False,'language':b.language,'current_tools':''}
 with con() as c:c.execute('INSERT INTO journeys(id,language,profile,stage,created_at,source) VALUES (?,?,?,?,?,?)',(i,b.language,j(p),'DISCOVERY',now(),b.source))
 return {'id':i,'profile':p,'stage':'DISCOVERY'}
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
 if k:k['reasons']=parse(k['reasons'])
 return {**r,'quotes':quotes,'kyc':k,'orders':orders}
@app.patch('/api/journeys/{jid}/profile')
def update_profile(jid:str,b:ProfileUpdate):
 x=getjourney(jid);p={**x['profile'],**b.profile}
 p.setdefault('must',[])
 validate_profile(p)
 with con() as c:
  if row(c,'SELECT id FROM orders WHERE journey_id=?',(jid,)):
   raise HTTPException(409,'Order already submitted. Changes require an assisted order amendment, not quote rewriting.')
  c.execute('UPDATE journeys SET profile=?,stage=? WHERE id=?',(j(p),'PROFILE_CONFIRMED' if p.get('confirmed') else 'DISCOVERY',jid))
  last=row(c,'SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC LIMIT 1',(jid,))
  invalidated=[];proposals=[];items=[]
  if last:
   items=parse(last['items']);issues=check_bundle(p,items);budget=int(p.get('budget') or 0)
   if budget and last['recurring_paise']>budget*100:
    issues=issues+[{'product_id':'*','code':'BUDGET','message':f"Latest quote is ₹{last['recurring_paise']/100:,.0f}/month; the confirmed budget is ₹{budget:,}."}]
   if issues:
    if last['accepted']:c.execute('UPDATE quotes SET accepted=0 WHERE id=?',(last['id'],))
    invalidated=issues
 if invalidated:proposals=recovery_options(p,items)
 return {'profile':p,'invalidated':invalidated,'recovery_options':proposals,'notice':'The latest quote was rechecked against the confirmed profile. Choose a recovery option to create a new quote version.'}

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
 old=calculate(profile,previous_items);new=quote(QuoteInput(journey_id=jid,items=option['items']))
 receipt={'previous_version':last['version'],'new_version':new['version'],
  'changed_facts':sorted({issue['code'] for issue in check_bundle(profile,previous_items)}|({'BUDGET'} if int(profile.get('budget') or 0) and old['recurring_paise']>int(profile['budget'])*100 else set())),
  'preserved':[item['product_id'] for item in option['items']],
  'dropped':[{'product_id':pid,'name':CAT[pid]['product_name'],'need':need_of(pid,profile)} for pid in requested if pid in CAT],
  'unmet_needs':option['unmet_needs'],'monthly_before_paise':old['recurring_paise'],
  'monthly_after_paise':new['recurring_paise'],'setup_before_paise':old['setup_paise'],
  'setup_after_paise':new['setup_paise'],'within_budget':option['within_budget'],
  'valid':new['valid'],'requires_new_acceptance':True,'pricing_policy':'MIDPOINT_DEMO_V1',
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
 text=text.translate(DEVANAGARI_DIGITS)
 text=re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b','[email removed]',text,flags=re.I)
 text=re.sub(r'\bUDYAM-[A-Z]{2}-\d{2}-\d{7}\b','[Udyam ID removed]',text,flags=re.I)
 text=re.sub(r'\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b','[GSTIN removed]',text,flags=re.I)
 text=re.sub(r'(?<!\w)(?:\+?91[\s-]?)?[6-9]\d{9}(?!\w)','[phone removed]',text)
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
  anchor_end=r'(?=\s|[,;:.!?]|$)'
  return re.search(rf"(?:\b{number}\b[\s\S]{{0,35}}(?:{anchors}){anchor_end}|(?:{anchors}){anchor_end}[\s\S]{{0,35}}\b{number}\b)",text) is not None
 if key=='city_tier' and value in (1,2):
  return re.search(rf'\btier\s*(?:{value}|{("one" if value==1 else "two")})\b',text) is not None
 if key=='language' and isinstance(value,str):
  language={'en':'english','hi':'hindi','mr':'marathi'}.get(value)
  return bool(language and re.search(rf'\b(?:{language})\b',text))
 if key=='business_type' and isinstance(value,str):
  aliases={
   'clinic':['clinic','dental practice','dentist','क्लिनिक','दवाखाना'], 'retail':['retail','shop','store','kirana','grocery','किराना','दुकान'],
   'restaurant':['restaurant','cafe','café','eatery','food outlet'], 'pharmacy':['pharmacy','chemist','medical shop'],
   'coaching centre':['coaching centre','coaching center','tuition','tutoring'],
   'manufacturing':['manufacturing','manufacturer','factory'], 'ca firm':['ca firm','chartered accountant'],
   'bakery':['bakery','bake shop'],
  }
  terms=aliases.get(value.casefold(),[value.casefold()])
  return any(re.search(rf'(?<!\w){re.escape(term)}(?!\w)',text) for term in terms)
 if key=='needs' and isinstance(value,str):
  aliases={
   'internet':['internet','broadband','wifi','wi-fi','fibre','fiber','इंटरनेट'], 'backup':['backup','failover','secondary connection','बैकअप'],
   'email':['email','e-mail','ईमेल'], 'security':['security','firewall','cybersecurity','सुरक्षा'],
   'calling':['calling','voice','landline','कॉलिंग','कॉल'], 'mobile':['mobile sim','mobile service','sim card','मोबाइल सिम','सिम कार्ड'],
   'website':['website','web presence','online store','वेबसाइट'], 'cctv':['cctv','surveillance camera','सीसीटीवी'],
  }
  return any(re.search(rf'(?<!\w){re.escape(term)}(?!\w)',text) for term in aliases.get(value, [value]))
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
 if lang in ('en','hi','mr') and explicitly_grounded('language',lang,source_text):changes['language']=lang
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

def mask_kyc_value(value:str)->str:
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
 if profile.get('language') not in ('en','hi','mr'):
  raise HTTPException(422,'Preferred language must be English, Hindi, or Marathi.')
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

@app.post('/api/journeys/{jid}/advisor')
async def advisor(jid:str,b:TextInput):
 x=getjourney(jid)
 if x['stage']=='ORDER_SUBMITTED':
  raise HTTPException(409,'This journey has a submitted order. Start a new journey for a separate advisor session.')
 local=local_runtime_status()
 if local['status']=='model_missing':
  raise HTTPException(503,f"Local model '{configured_model()}' is not installed. Run 'ollama pull {configured_model()}' and try again.")
 if local['status']=='runtime_unavailable':
  raise HTTPException(503,'Ollama is not reachable on this device. Start the Ollama app and try again.')
 if local['status']=='cloud_model_blocked':
  raise HTTPException(400,'Cloud Ollama models are disabled. Set OLLAMA_MODEL to a locally installed model such as qwen3:8b.')
 if local['status']!='ready':
  raise HTTPException(503,'OLLAMA_BASE_URL must point to localhost or the local Docker host.')
 try:client=create_client()
 except Exception as exc:
  logging.exception('OneJourney GenAI client could not be initialized')
  raise HTTPException(503,'The local GenAI advisor dependencies are unavailable. Install the project requirements and restart the app.') from exc
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
  if name=='analyze_funnel':return analyze_leads(args.get('segment'))
  if name=='assess_journey_recovery':
   with con() as db:q=row(db,'SELECT * FROM quotes WHERE journey_id=? ORDER BY version DESC LIMIT 1',(jid,))
   if not q:return {'summary':'No quote exists in this journey yet. Build a bundle to preview options.','quote_found':False}
   items=parse(q['items']);issues=check_bundle(profile,items);budget=int(profile.get('budget') or 0)
   if budget and q['recurring_paise']>budget*100:issues=issues+[{'product_id':'*','code':'BUDGET','message':f"Latest quote exceeds the confirmed budget of ₹{budget:,}."}]
   alts=candidates(profile);recoveries=recovery_options(profile,items) if issues else []
   return {'summary':'Rechecked the latest quote against the current profile and listed validated, customer-selected recovery options.','quote_found':True,'quote_version':q['version'],'current_valid':not issues,'current_issues':issues,'valid_alternatives':alts[:3],'recovery_options':recoveries,'note':'Submitted orders are never rewritten. A recovery creates a new quote that needs fresh acceptance.'}
  raise ValueError('Unknown advisor tool.')
 try:
  result=await run_agent(client=client,profile=profile,language=x['language'],history=history,user_text=cleaned,tool_executor=execute_tool)
 except Exception as exc:
  logging.exception('OneJourney local Ollama request failed')
  raise HTTPException(502,'The local GenAI advisor could not complete this turn. Check Ollama and try again.') from exc
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
 except:raise HTTPException(415,'Only valid image files accepted')
 try:analysis=analyse_kyc_document(raw,GST)
 except Exception as e:raise HTTPException(503,'Local OCR could not process this image. Check OpenCV and Tesseract are installed.') from e
 text=analysis['text'];flags=list(analysis['flags']);display_fields=dict(analysis['fields'])
 if display_fields.get('gstin_raw'):display_fields['gstin_raw']=mask_kyc_value(display_fields['gstin_raw'])
 if not analysis['doc_type']:flags.append('DOCUMENT_TYPE_UNCLEAR: review the OCR or upload a clearer business document')
 extracted={'text_preview':redact_chat(text[:1200]),'gstin_candidates':analysis['gstin_candidates'],
  'gstin_suggestion':analysis['gstin_suggestion'],'udyam_candidates':analysis['udyam_candidates'],
  'doc_type':analysis['doc_type'],'fields':display_fields,'quality':analysis['quality'],'flags':flags,
  'ocr_engine':'Tesseract with quality gate and deskew','requires_user_review':True}
 id=uid('doc')
 with con() as c:c.execute('INSERT INTO documents VALUES(?,?,?,?,?,?)',(id,journey_id,re.sub(r'[^a-zA-Z0-9._-]','_',file.filename or 'image'),j(extracted),0,now()))
 return {'document_id':id,'extracted':extracted,'notice':'Actual local OCR, not proof of identity. Verify against the mock registry.'}
@app.post('/api/kyc/documents/{did}/confirm')
def confirm_doc(did:str):
 with con() as c:
  if not row(c,'SELECT * FROM documents WHERE id=?',(did,)):raise HTTPException(404,'Document missing')
  c.execute('UPDATE documents SET confirmed=1 WHERE id=?',(did,))
 return {'confirmed':True}

def business_names(gstin_record:dict[str,Any])->set[str]:
 """Accept registry trade names as well as the proprietor's GST legal-name form."""
 names={gstin_record['legal_name'].strip().casefold(),gstin_record['trade_name'].strip().casefold()}
 match=re.search(r'\(prop\.?\s*(.+?)\)',gstin_record['legal_name'],re.I)
 if match:names.add(match.group(1).strip().casefold())
 return names

def normalized_name(value:str)->str:
 return re.sub(r'[^a-z0-9]','',(value or '').casefold())

@app.post('/api/kyc/verify')
def verify(b:VerifyKyc):
 x=getjourney(b.journey_id);p=x['profile'];reasons=[]
 gstin=b.gstin.upper().strip();udyam=canonical_udyam(b.udyam_number) if b.udyam_number.strip() else ''
 g=GST.get(gstin);u=UDYAM.get(udyam) if udyam else None
 if gstin and (not GSTIN_PATTERN.fullmatch(gstin) or not gstin_valid(gstin)):
  reasons.append('GSTIN format or checksum is invalid; review the extracted value or type it again')
 elif gstin and not g:reasons.append('GSTIN not found in synthetic registry')
 elif g and g['status']!='Active':reasons.append('GSTIN status: '+g['status'])
 if b.udyam_number.strip() and not udyam:reasons.append('Udyam number format is invalid')
 elif udyam and not u:reasons.append('Udyam number not found in mock registry')
 if g and b.legal_name.strip() and normalized_name(b.legal_name) not in {normalized_name(name) for name in business_names(g)}:
  reasons.append('Business name differs from GST registry')
 if u and g and u['enterprise_name'].strip().casefold() not in business_names(g):
  reasons.append('GSTIN and Udyam registry business names do not match')
 with con() as c:
  if not row(c,'SELECT * FROM consents WHERE journey_id=?',(b.journey_id,)):reasons.append('Consent missing')
  ds=[dict(r) for r in c.execute('SELECT * FROM documents WHERE journey_id=?',(b.journey_id,)).fetchall()]
  if not ds:reasons.append('Document upload required')
  elif not any(d['confirmed'] for d in ds):reasons.append('OCR result requires user confirmation')
  confirmed_docs=[parse(d['extracted']) for d in ds if d['confirmed']]
  if b.require_all_documents:
   types={doc.get('doc_type') for doc in confirmed_docs if doc.get('doc_type')}
   for doc_type in ('gst_certificate','udyam_certificate','signatory_id','address_proof'):
    if doc_type not in types:reasons.append('Missing document: '+doc_type.replace('_',' '))
  if confirmed_docs:
   recognized_gstin={value.upper() for doc in confirmed_docs for value in doc.get('gstin_candidates',[])}
   recognized_udyam={canonical_udyam(value) for doc in confirmed_docs for value in doc.get('udyam_candidates',[])}-{None}
   suggestions={doc.get('gstin_suggestion') for doc in confirmed_docs if doc.get('gstin_suggestion')}
   if gstin and gstin in suggestions:recognized_gstin.add(gstin)
   for doc in confirmed_docs:
    for flag in doc.get('flags',[]):
     if flag.startswith('CONFIRM_GSTIN_SUGGESTION') and gstin and gstin==doc.get('gstin_suggestion'):continue
     if flag.startswith('UDYAM_NUMBER_NOT_READABLE') and udyam and u:continue
     reasons.append(flag)
   if not ((gstin and gstin in recognized_gstin) or (udyam and udyam in recognized_udyam)):
    reasons.append('Uploaded documents do not contain a matching registration identifier; human review required')
   doc_names={normalized_name(value) for doc in confirmed_docs for key,value in (doc.get('fields') or {}).items()
    if key in ('trade_name','enterprise_name','billed_to','business') and value}
   if len(doc_names)>1:reasons.append('Business names differ across uploaded documents; human review required')
  quote=row(c,'SELECT * FROM quotes WHERE journey_id=? AND accepted=1 ORDER BY version DESC',(b.journey_id,))
  if quote:
   selected=[x['product_id'] for x in parse(quote['items'])]
   if any(CAT[pid]['requires_gstin'] for pid in selected if pid in CAT) and (not g or g['status']!='Active'):reasons.append('Selected product requires active GSTIN')
  status=('REVIEW_REQUIRED' if any('human review' in reason.casefold() for reason in reasons) else 'ACTION_REQUIRED') if reasons else 'VERIFIED';newp={**p,'gstin':gstin,'udyam_number':udyam or ''}
  # Only save registry-backed GSTIN as an active eligibility fact.
  c.execute('UPDATE journeys SET profile=? WHERE id=?',(j(newp),b.journey_id))
  c.execute('INSERT OR REPLACE INTO kyc VALUES(?,?,?,?,?,?)',(b.journey_id,status,j(list(dict.fromkeys(reasons))),gstin,udyam or '',now()))
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
