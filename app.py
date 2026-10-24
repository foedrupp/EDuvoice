import base64, csv, hashlib, io, json, os, re, secrets, sqlite3, time
from typing import Any
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote
import qrcode
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT=Path(__file__).parent
DB=Path(os.getenv('EDUVOICE_DB',str(ROOT/'eduvoice.sqlite3')))
AUDIO=Path(os.getenv('EDUVOICE_AUDIO',str(ROOT/'data'/'audio')))
MAX_PARTICIPANTS=500
MAX_QUESTIONS=50
MAX_OPTIONS=20
MAX_VOICE_BYTES=1_200_000
VOICE_MIME={'audio/webm','audio/mp4','audio/ogg','audio/mpeg','audio/wav'}
QUESTION_TYPES=('single','multi','dropdown','yesno','text','short','paragraph','scale','likert','voice')
FORM_KINDS=('survey','self','test')
KIND_TYPES={'survey':set(QUESTION_TYPES),'self':{'likert','scale','short','paragraph'},'test':{'single','multi','dropdown','yesno','short','paragraph'}}
app=FastAPI(title='EduVoice MVP')
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')

@contextmanager
def conn():
 c=sqlite3.connect(DB,timeout=15);c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=15000')
 try:
  yield c;c.commit()
 finally:c.close()

def init():
 with conn() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS sessions(code TEXT PRIMARY KEY, token TEXT NOT NULL, question TEXT NOT NULL, options TEXT NOT NULL, mode TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS responses(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, participant TEXT NOT NULL, name TEXT, choice INTEGER NOT NULL, created INTEGER NOT NULL, UNIQUE(code,participant));''')
  scols={r['name'] for r in c.execute('PRAGMA table_info(sessions)')}
  if 'questions' not in scols:c.execute('ALTER TABLE sessions ADD COLUMN questions TEXT')
  if 'title' not in scols:c.execute('ALTER TABLE sessions ADD COLUMN title TEXT')
  if 'description' not in scols:c.execute('ALTER TABLE sessions ADD COLUMN description TEXT')
  if 'quiz' not in scols:c.execute('ALTER TABLE sessions ADD COLUMN quiz INTEGER NOT NULL DEFAULT 0')
  if 'passcode_hash' not in scols:c.execute('ALTER TABLE sessions ADD COLUMN passcode_hash TEXT')
  if 'passcode_salt' not in scols:c.execute('ALTER TABLE sessions ADD COLUMN passcode_salt TEXT')
  if 'kind' not in scols:c.execute("ALTER TABLE sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'survey'")
  rcols={r['name'] for r in c.execute('PRAGMA table_info(responses)')}
  if 'answers' not in rcols:c.execute('ALTER TABLE responses ADD COLUMN answers TEXT')
  c.execute('CREATE TABLE IF NOT EXISTS voice_clips(id TEXT PRIMARY KEY, code TEXT NOT NULL, mime TEXT NOT NULL, bytes INTEGER NOT NULL, created INTEGER NOT NULL)')
 AUDIO.mkdir(parents=True, exist_ok=True)
init()
with conn() as c:c.execute('PRAGMA journal_mode=WAL')

class QuestionIn(BaseModel):
 type:str
 text:str=Field(min_length=1,max_length=500)
 options:list[str]=[]
 required:bool=True
 other:bool=False
 high:int=5
 lowLabel:str=''
 highLabel:str=''
 rows:list[str]=[]
 points:int=1
 correct:Any=None
 feedback:str=''
class Create(BaseModel):
 title:str=''
 description:str=''
 questions:list[QuestionIn]=Field(min_length=1,max_length=MAX_QUESTIONS)
 mode:str='anonymous'
 quiz:bool=False
 kind:str='survey'
 passcode:str=''
class PasscodeIn(BaseModel):
 passcode:str=''
class Vote(BaseModel):
 participant:str=Field(min_length=8,max_length=100)
 name:str|None=None
 answers:list|None=None
 choice:int|None=None

clients:dict[str,set[WebSocket]]={}
opened:dict[str,set[WebSocket]]={}
attempts:dict[tuple,list]={}

def is_int(n):return isinstance(n,int) and not isinstance(n,bool)

def get_session(code):
 with conn() as c: r=c.execute('SELECT * FROM sessions WHERE code=?',(code.upper(),)).fetchone()
 if not r:raise HTTPException(404,'Session not found')
 return dict(r)

def questions_of(s):
 keys=s.keys();raw=s['questions'] if 'questions' in keys else None
 if raw:
  try:
   data=json.loads(raw)
   if isinstance(data,list) and data:return data
  except Exception:pass
 try: opts=json.loads(s['options']) if s['options'] else []
 except Exception: opts=[]
 if not isinstance(opts,list):opts=[]
 return [{'type':'single','text':s['question'] or '','options':opts}]

def norm_text(value):
 return ' '.join(value.strip().casefold().split())

def clean_questions(items,quiz=False,kind='survey'):
 if kind not in FORM_KINDS:raise HTTPException(400,'Choose a survey, a self-evaluation, or a test')
 if not 1<=len(items)<=MAX_QUESTIONS:raise HTTPException(400,'Add between 1 and 50 questions')
 out=[]
 allowed=KIND_TYPES[kind]
 for q in items:
  if q.type not in QUESTION_TYPES:raise HTTPException(400,'Invalid question type')
  if q.type not in allowed:raise HTTPException(400,'This question type is not used in this form')
  text=q.text.strip()
  if not 3<=len(text)<=500:raise HTTPException(400,'Each question needs between 3 and 500 characters')
  options=[];rows=[]
  other=bool(q.other) and q.type in ('single','multi')
  if q.type in ('single','multi','dropdown'):
   options=[x.strip() for x in q.options if x.strip()]
   if not 2<=len(options)<=MAX_OPTIONS:raise HTTPException(400,'Choice questions need between 2 and 20 choices')
   if any(len(x)>160 for x in options):raise HTTPException(400,'A choice is too long')
   if len({x.lower() for x in options})!=len(options):raise HTTPException(400,'Choices in one question must differ')
  elif q.type=='likert':
   options=[x.strip() for x in q.options if x.strip()]
   rows=[x.strip() for x in q.rows if x.strip()]
   if not 3<=len(options)<=7:raise HTTPException(400,'A Likert scale needs between 3 and 7 labels')
   if any(len(x)>40 for x in options):raise HTTPException(400,'A scale label is too long')
   if len({norm_text(x) for x in options})!=len(options):raise HTTPException(400,'Scale labels must differ')
   if not 1<=len(rows)<=10:raise HTTPException(400,'Add between 1 and 10 statements')
   if any(len(x)>200 for x in rows):raise HTTPException(400,'A statement is too long')
   if len({norm_text(x) for x in rows})!=len(rows):raise HTTPException(400,'Statements must differ')
  points=q.points if is_int(q.points) and 0<=q.points<=10 else 1
  feedback=q.feedback.strip()[:300] if isinstance(q.feedback,str) else ''
  correct=None
  if not quiz:pass
  elif q.type in ('single','dropdown') and is_int(q.correct):
   if not 0<=q.correct<len(options):raise HTTPException(400,'Mark a correct answer that is one of the choices')
   correct=q.correct
  elif q.type=='yesno' and q.correct in (0,1):correct=q.correct
  elif q.type=='multi' and isinstance(q.correct,list) and q.correct and all(is_int(i) for i in q.correct):
   if any(not 0<=i<len(options) for i in q.correct):raise HTTPException(400,'Mark a correct answer that is one of the choices')
   correct=sorted(set(q.correct))
  elif q.type in ('short','paragraph','text') and isinstance(q.correct,str) and q.correct.strip():
   correct=q.correct.strip()[:written_limit(q.type)]
  high=q.high if q.high in (5,10) else 5
  out.append({'type':q.type,'text':text,'options':options,'rows':rows,'required':bool(q.required),'other':other,'high':high,'lowLabel':q.lowLabel.strip()[:40],'highLabel':q.highLabel.strip()[:40],'points':points,'correct':correct,'feedback':feedback})
 if quiz and not any(has_key(q) for q in out):raise HTTPException(400,'Mark the correct answer on at least one question')
 return out

def has_key(q):
 points=q.get('points',1)
 if not is_int(points) or points<=0:return False
 key=q.get('correct');kind=q.get('type')
 if kind in ('single','dropdown','yesno'):return is_int(key)
 if kind=='multi':return isinstance(key,list) and len(key)>0
 if kind in ('short','paragraph','text'):return isinstance(key,str) and bool(key.strip())
 return False

def written_limit(kind):
 if kind=='paragraph':return 2000
 if kind=='short':return 300
 return 1000

def clean_answers(questions,answers):
 if not isinstance(answers,list) or len(answers)!=len(questions):raise HTTPException(400,'Answer every question')
 out=[]
 for q,a in zip(questions,answers):
  kind=q['type'];required=q.get('required',True);opts=q.get('options') or []
  if kind in ('single','dropdown'):
   if isinstance(a,dict) and q.get('other'):
    text=a.get('other')
    text=text.strip() if isinstance(text,str) else ''
    if not text:
     if required:raise HTTPException(400,'Answer every required question')
     out.append(None);continue
    if len(text)>160:raise HTTPException(400,'The other answer is too long')
    out.append({'other':text});continue
   if a is None or a=='':
    if required:raise HTTPException(400,'Answer every required question')
    out.append(None);continue
   if not is_int(a) or not 0<=a<len(opts):raise HTTPException(400,'Choose one answer for every required question')
   out.append(a)
  elif kind=='multi':
   choices=a.get('choices') if isinstance(a,dict) else a
   other=a.get('other') if isinstance(a,dict) else ''
   other=other.strip() if isinstance(other,str) else ''
   if choices is None:choices=[]
   if not isinstance(choices,list) or any(not is_int(i) or not 0<=i<len(opts) for i in choices):raise HTTPException(400,'Choose at least one answer for every required question')
   if other and not q.get('other'):raise HTTPException(400,'This question does not allow another answer')
   if other and len(other)>160:raise HTTPException(400,'The other answer is too long')
   choices=sorted(set(choices))
   if required and not choices and not other:raise HTTPException(400,'Answer every required question')
   out.append({'choices':choices,'other':other} if other else choices)
  elif kind=='yesno':
   if a is None or a=='':
    if required:raise HTTPException(400,'Answer every required question')
    out.append(None);continue
   if a not in (0,1):raise HTTPException(400,'Choose yes or no for every required question')
   out.append(a)
  elif kind in ('text','short','paragraph'):
   if a is None:a=''
   if not isinstance(a,str):raise HTTPException(400,'Write an answer')
   text=a.strip();limit=written_limit(kind)
   if not text:
    if required:raise HTTPException(400,'Answer every required question')
    out.append(None);continue
   if len(text)>limit:raise HTTPException(400,'A written answer is too long')
   out.append(text)
  elif kind=='scale':
   high=q.get('high') if q.get('high') in (5,10) else 5
   if a is None or a=='':
    if required:raise HTTPException(400,'Answer every required question')
    out.append(None);continue
   if not is_int(a) or not 1<=a<=high:raise HTTPException(400,'Choose a point on the scale')
   out.append(a)
  elif kind=='voice':
   out.append(decode_voice(a,required))
  elif kind=='likert':
   rows=q.get('rows') or []
   if a is None:
    if required:raise HTTPException(400,'Answer every required question')
    out.append(None);continue
   if not isinstance(a,list) or len(a)!=len(rows):raise HTTPException(400,'Rate every statement')
   cleaned=[]
   for n in a:
    if n is None or n=='':
     if required:raise HTTPException(400,'Rate every statement')
     cleaned.append(None)
    elif not is_int(n) or not 0<=n<len(opts):raise HTTPException(400,'Choose a point on the scale')
    else:cleaned.append(n)
   out.append(cleaned)
  else:raise HTTPException(400,'Invalid question type')
 return out

def decoded_answers(row,n):
 raw=row['answers'] if 'answers' in row.keys() else None
 if raw:
  try:
   data=json.loads(raw)
   if isinstance(data,list):return data
  except Exception:pass
 out=[None]*n
 if n:out[0]=row['choice']
 return out

def question_meta(q):
 return {'required':q.get('required',True),'other':bool(q.get('other')),'high':q.get('high') if q.get('high') in (5,10) else 5,'lowLabel':q.get('lowLabel') or '','highLabel':q.get('highLabel') or ''}

def answer_matches(q,a):
 if a is None:return False
 kind=q.get('type');key=q.get('correct');opts=q.get('options') or []
 if kind in ('single','dropdown','yesno'):return a==key
 if kind=='multi':
  choices=a.get('choices') if isinstance(a,dict) else a
  if isinstance(a,dict) and a.get('other'):return False
  return isinstance(choices,list) and sorted(set(choices))==list(key or [])
 if kind in ('short','paragraph','text'):return isinstance(a,str) and norm_text(a)==norm_text(key or '')
 return False

def grade_one(q,a):
 if not has_key(q):return None
 points=q.get('points') or 0
 ok=answer_matches(q,a)
 return {'correct':ok,'points':points,'earned':points if ok else 0}

def grade_response(questions,answers):
 parts=[grade_one(q,answers[i] if i<len(answers) else None) for i,q in enumerate(questions)]
 graded=[p for p in parts if p]
 return {'score':sum(p['earned'] for p in graded),'max':sum(p['points'] for p in graded),'parts':parts}

def expected_text(q):
 key=q.get('correct');opts=q.get('options') or []
 if q.get('type') in ('single','dropdown') and is_int(key) and 0<=key<len(opts):return opts[key]
 if q.get('type')=='yesno':return 'Yes' if key==0 else 'No' if key==1 else ''
 if q.get('type')=='multi' and isinstance(key,list):return '; '.join(opts[i] for i in key if is_int(i) and 0<=i<len(opts))
 if q.get('type') in ('short','paragraph','text') and isinstance(key,str):return key.strip()
 return ''

def review_for(questions,answers):
 graded=grade_response(questions,answers);out=[]
 for i,part in enumerate(graded['parts']):
  if not part:out.append({'index':i,'graded':False});continue
  q=questions[i]
  out.append({'index':i,'graded':True,'correct':part['correct'],'earned':part['earned'],'points':part['points'],'feedback':q.get('feedback') or '','expected':expected_text(q)})
 return {'score':graded['score'],'max':graded['max'],'review':out}

def enrich(questions,rows,quiz=False):
 stats=[]
 for q in questions:
  meta=question_meta(q)
  item={'type':q['type'],'text':q['text'],'options':list(q.get('options') or []),**meta}
  if q['type'] in ('single','multi','dropdown'):
   item['counts']=[0]*(len(item['options'])+(1 if item['other'] else 0));item['otherTexts']=[]
  elif q['type']=='yesno':
   item['options']=['Yes','No'];item['counts']=[0,0]
  elif q['type']=='scale':
   item['counts']=[0]*item['high'];item['average']=None
  elif q['type']=='likert':
   item['rows']=list(q.get('rows') or [])
   item['counts']=[[0]*len(item['options']) for _ in item['rows']]
   item['averages']=[None]*len(item['rows'])
  elif q['type']=='voice':item['count']=0
  else:item['texts']=[]
  if quiz and has_key(q):
   item['graded']=True;item['points']=q.get('points') or 0;item['correctCount']=0
  stats.append(item)
 for row in rows:
  answers=decoded_answers(row,len(questions))
  for i,q in enumerate(questions):
   if i>=len(answers) or answers[i] is None:continue
   a=answers[i];item=stats[i]
   if q['type'] in ('single','dropdown'):
    if is_int(a) and 0<=a<len(q.get('options') or []):item['counts'][a]+=1
    elif isinstance(a,dict) and a.get('other') and item['other']:
     item['counts'][-1]+=1;item['otherTexts'].append(a['other'])
   elif q['type']=='multi':
    choices=a.get('choices') if isinstance(a,dict) else a
    other=a.get('other') if isinstance(a,dict) else ''
    if isinstance(choices,list):
     for n in choices:
      if is_int(n) and 0<=n<len(q.get('options') or []):item['counts'][n]+=1
    if other and item['other']:
     item['counts'][-1]+=1;item['otherTexts'].append(other)
   elif q['type']=='yesno' and a in (0,1):item['counts'][a]+=1
   elif q['type']=='scale' and is_int(a) and 1<=a<=item['high']:item['counts'][a-1]+=1
   elif q['type']=='likert' and isinstance(a,list):
    opts=q.get('options') or []
    for r,n in enumerate(a):
     if r<len(item['counts']) and is_int(n) and 0<=n<len(opts):item['counts'][r][n]+=1
   elif q['type']=='voice' and isinstance(a,str) and a:item['count']+=1
   elif q['type'] in ('text','short','paragraph') and isinstance(a,str) and a.strip():item['texts'].append(a.strip())
   if item.get('graded'):
    part=grade_one(q,a)
    if part and part['correct']:item['correctCount']+=1
 for item in stats:
  if item['type']=='scale':
   total=sum(item['counts'])
   item['average']=round(sum((i+1)*n for i,n in enumerate(item['counts']))/total,1) if total else None
  elif item['type']=='likert':
   avs=[]
   for counts in item['counts']:
    total=sum(counts)
    avs.append(round(sum((i+1)*n for i,n in enumerate(counts))/total,1) if total else None)
   item['averages']=avs
 return stats

def answer_text(q,a):
 if a is None:return ''
 opts=q.get('options') or []
 if q['type'] in ('single','dropdown'):
  if isinstance(a,dict) and a.get('other'):return 'Other: '+a['other']
  return opts[a] if is_int(a) and 0<=a<len(opts) else ''
 if q['type']=='multi':
  choices=a.get('choices') if isinstance(a,dict) else a
  other=a.get('other') if isinstance(a,dict) else ''
  parts=[opts[i] for i in choices if is_int(i) and 0<=i<len(opts)] if isinstance(choices,list) else []
  if other:parts.append('Other: '+other)
  return '; '.join(parts)
 if q['type']=='yesno':return 'Yes' if a==0 else 'No' if a==1 else ''
 if q['type']=='scale' and is_int(a):return f"{a}/{q.get('high') if q.get('high') in (5,10) else 5}"
 if q['type']=='likert' and isinstance(a,list):
  rows=q.get('rows') or []
  parts=[]
  for i,n in enumerate(a):
   if not is_int(n) or not 0<=n<len(opts):continue
   parts.append(f"{rows[i]}: {opts[n]}" if len(rows)>1 and i<len(rows) else opts[n])
  return '; '.join(parts)
 if q['type'] in ('text','short','paragraph') and isinstance(a,str):return a
 if q['type']=='voice' and isinstance(a,str) and a:return 'Voice recording'
 return ''

def session_kind(session):
 keys=session.keys();value=session['kind'] if 'kind' in keys and session['kind'] else ''
 return value if value in FORM_KINDS else 'survey'

def is_quiz(session):
 return bool(session.get('quiz')) if isinstance(session,dict) else bool(session['quiz']) if 'quiz' in session.keys() and session['quiz'] else False

def response_rows(session,rows):
 qs=questions_of(session);named=session['mode']=='named';quiz=is_quiz(session);out=[]
 for i,r in enumerate(rows,1):
  decoded=decoded_answers(r,len(qs))
  item={'number':i,'name':r['name'] if named else None,'answers':decoded,'answer':answer_text(qs[0],decoded[0] if decoded else None),'created':r['created']}
  if quiz:
   graded=grade_response(qs,decoded);item['score']=graded['score'];item['max']=graded['max']
  out.append(item)
 return out

def decode_voice(a,required):
 if a is None or a=='':
  if required:raise HTTPException(400,'Answer every required question')
  return None
 if not isinstance(a,dict):raise HTTPException(400,'Record a voice answer')
 mime=str(a.get('mime') or 'audio/webm').split(';')[0].strip().lower()
 if mime not in VOICE_MIME:raise HTTPException(400,'Use a voice recording')
 raw=a.get('audio')
 if not isinstance(raw,str) or len(raw)>1_700_000:raise HTTPException(400,'The voice recording is too long')
 try:blob=base64.b64decode(raw,validate=True)
 except Exception:raise HTTPException(400,'The voice recording could not be read')
 if len(blob)<200:raise HTTPException(400,'The voice recording is too short')
 if len(blob)>MAX_VOICE_BYTES:raise HTTPException(400,'The voice recording is too long')
 return {'blob':blob,'mime':mime}

def write_voice(c,code,blob,mime):
 clip=secrets.token_hex(16)
 AUDIO.mkdir(parents=True, exist_ok=True)
 (AUDIO/f'{clip}.bin').write_bytes(blob)
 c.execute('INSERT INTO voice_clips(id,code,mime,bytes,created) VALUES(?,?,?,?,?)',(clip,code,mime,len(blob),int(time.time())))
 return clip

def remove_voice_files(clips):
 for clip in clips:
  try:(AUDIO/f'{clip}.bin').unlink()
  except OSError:pass

def load_rows(code):
 with conn() as c:return c.execute('SELECT id,name,choice,answers,created FROM responses WHERE code=? ORDER BY id',(code,)).fetchall()

def is_locked(session):
 if isinstance(session,dict):return bool(session.get('passcode_hash'))
 return bool(session['passcode_hash']) if 'passcode_hash' in session.keys() and session['passcode_hash'] else False

def store_passcode(pin):
 pin=(pin or '').strip()
 if not pin:return None,None
 if len(pin)!=4 or not pin.isdigit():raise HTTPException(400,'Use a 4-digit passcode')
 salt=secrets.token_hex(16)
 digest=hashlib.pbkdf2_hmac('sha256',pin.encode(),bytes.fromhex(salt),120000).hex()
 return salt,digest

def passcode_matches(session,pin):
 if not is_locked(session):return True
 pin=(pin or '').strip()
 if len(pin)!=4 or not pin.isdigit():return False
 salt=session.get('passcode_salt') or ''
 try:salt_b=bytes.fromhex(salt)
 except ValueError:return False
 digest=hashlib.pbkdf2_hmac('sha256',pin.encode(),salt_b,120000).hex()
 return secrets.compare_digest(digest,session.get('passcode_hash') or '')

def client_ip(obj):
 client=getattr(obj,'client',None)
 return getattr(client,'host',None) or 'local'

def guard_passcode(session,pin,ip):
 if not is_locked(session):return
 pin=(pin or '').strip()
 if not pin:raise HTTPException(401,'Enter the response passcode')
 if len(pin)!=4 or not pin.isdigit():raise HTTPException(401,'Incorrect passcode')
 now=time.time();key=(ip,session['code'])
 recent=[t for t in attempts.get(key,[]) if now-t<600]
 if len(recent)>=10:raise HTTPException(429,'Too many attempts. Wait a few minutes.')
 if passcode_matches(session,pin):
  attempts.pop(key,None);return
 recent.append(now);attempts[key]=recent
 raise HTTPException(401,'Incorrect passcode')

def redact(payload):
 data=json.loads(json.dumps(payload))
 data.pop('responses',None);data.pop('averageScore',None)
 for q in data.get('questions') or []:
  q.pop('texts',None);q.pop('otherTexts',None)
 data['locked']=True;data['revealed']=False
 return data

def state(code):
 s=get_session(code);qs=questions_of(s);rows=load_rows(code);quiz=is_quiz(s)
 enriched=enrich(qs,rows,quiz);first=enriched[0];total=len(rows)
 title=(s.get('title') or '').strip();description=(s.get('description') or '').strip()
 payload={'code':code,'title':title,'description':description,'question':title or first['text'],'options':first.get('options') or [],'questions':enriched,'mode':s['mode'],'kind':session_kind(s),'active':bool(s['active']),'created':s['created'],'counts':first.get('counts') or [],'total':total,'capacity':MAX_PARTICIPANTS,'remaining':MAX_PARTICIPANTS-total,'question_count':len(enriched),'quiz':quiz,'locked':is_locked(s)}
 if quiz:
  grades=[grade_response(qs,decoded_answers(r,len(qs))) for r in rows]
  payload['maxScore']=grade_response(qs,[None]*len(qs))['max']
  payload['averageScore']=round(sum(g['score'] for g in grades)/len(grades),1) if grades else None
 return payload

def live(code):
 code=code.upper();payload=state(code);s=get_session(code)
 payload['responses']=response_rows(s,load_rows(code));payload['revealed']=True
 return payload

def public_state(code):
 payload=state(code)
 return redact(payload) if payload.get('locked') else payload

async def notify(code):
 code=code.upper();full=live(code);safe=redact(full) if full.get('locked') else full
 allowed=opened.get(code,set())
 for ws in list(clients.get(code,set())):
  try:await ws.send_json(full if not full.get('locked') or ws in allowed else safe)
  except Exception:
   clients.get(code,set()).discard(ws);opened.get(code,set()).discard(ws)

@app.get('/')
def home():return HTMLResponse((ROOT/'static'/'index.html').read_text(encoding='utf-8'))

@app.post('/api/sessions')
async def create(data:Create):
 if data.mode not in ('anonymous','named'):raise HTTPException(400,'Invalid mode')
 kind=data.kind if data.kind in FORM_KINDS else ('test' if data.quiz else 'survey')
 quiz=kind=='test'
 qs=clean_questions(data.questions,quiz,kind);first=qs[0]
 title=data.title.strip()[:200];description=data.description.strip()[:800]
 code=secrets.token_hex(3).upper();token=secrets.token_urlsafe(24)
 salt,digest=store_passcode(data.passcode)
 with conn() as c:c.execute('INSERT INTO sessions(code,token,question,options,mode,active,created,questions,title,description,quiz,passcode_hash,passcode_salt,kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(code,token,title or first['text'],json.dumps(first.get('options') or [],ensure_ascii=False),data.mode,1,int(time.time()),json.dumps(qs,ensure_ascii=False),title,description,1 if quiz else 0,digest,salt,kind))
 return {'code':code,'token':token}

def session_public(row,total):
 qs=questions_of(row);first=qs[0]
 title=(row['title'] or '').strip() if 'title' in row.keys() and row['title'] else ''
 description=(row['description'] or '').strip() if 'description' in row.keys() and row['description'] else ''
 quiz=bool(row['quiz']) if 'quiz' in row.keys() and row['quiz'] else False
 locked=is_locked(row)
 return {'code':row['code'],'title':title,'description':description,'question':title or first['text'],'options':first.get('options') or [],'question_count':len(qs),'mode':row['mode'],'kind':session_kind(row),'active':bool(row['active']),'created':row['created'],'total':total,'capacity':MAX_PARTICIPANTS,'quiz':quiz,'locked':locked}

@app.get('/api/sessions')
def list_sessions():
 with conn() as c:
  sessions=c.execute('SELECT * FROM sessions ORDER BY created DESC, code').fetchall()
  counts={r['code']:r['n'] for r in c.execute('SELECT code, COUNT(*) AS n FROM responses GROUP BY code')}
 return [session_public(s,counts.get(s['code'],0)) for s in sessions]

@app.get('/api/sessions/{code}')
def read(code:str):return public_state(code.upper())

@app.get('/api/sessions/{code}/responses')
def list_responses(code:str,request:Request,passcode:str=''):
 code=code.upper();s=get_session(code)
 guard_passcode(s,passcode,client_ip(request))
 rows=load_rows(code)
 out=session_public(s,len(rows));quiz=is_quiz(s)
 out['questions']=enrich(questions_of(s),rows,quiz);out['responses']=response_rows(s,rows)
 out['remaining']=MAX_PARTICIPANTS-len(rows);out['locked']=is_locked(s);out['revealed']=True
 if quiz:
  snap=state(code);out['maxScore']=snap['maxScore'];out['averageScore']=snap['averageScore']
 return out

@app.post('/api/sessions/{code}/passcode')
async def set_passcode(code:str,token:str,data:PasscodeIn):
 code=code.upper();check_token(code,token)
 salt,digest=store_passcode(data.passcode)
 with conn() as c:c.execute('UPDATE sessions SET passcode_hash=?,passcode_salt=? WHERE code=?',(digest,salt,code))
 opened.pop(code,None)
 await notify(code)
 return {'ok':True,'locked':bool(digest)}

@app.post('/api/sessions/{code}/vote')
async def vote(code:str,data:Vote):
 code=code.upper();s=get_session(code)
 if not s['active']:raise HTTPException(400,'Poll is closed')
 qs=questions_of(s)
 if data.answers is None:
  if data.choice is None or len(qs)!=1 or qs[0]['type']!='single':raise HTTPException(400,'Answer every question')
  answers=clean_answers(qs,[data.choice])
 else:answers=clean_answers(qs,data.answers)
 name=(data.name or '').strip()
 if s['mode']=='named' and not name:raise HTTPException(400,'Name is required')
 choice=answers[0] if qs[0]['type'] in ('single','dropdown') and is_int(answers[0]) else 0
 saved=[]
 try:
  with conn() as c:
   try:
    c.execute('BEGIN IMMEDIATE')
    n=c.execute('SELECT COUNT(*) FROM responses WHERE code=?',(code,)).fetchone()[0]
    if n>=MAX_PARTICIPANTS:raise HTTPException(409,'This session has reached its 500-participant limit')
    for i,a in enumerate(answers):
     if isinstance(a,dict) and 'blob' in a:
      answers[i]=write_voice(c,code,a['blob'],a['mime']);saved.append(answers[i])
    c.execute('INSERT INTO responses(code,participant,name,choice,created,answers) VALUES(?,?,?,?,?,?)',(code,data.participant,name if s['mode']=='named' else None,choice,int(time.time()),json.dumps(answers,ensure_ascii=False)))
   except sqlite3.IntegrityError:raise HTTPException(409,'This browser has already submitted a response')
 except Exception:
  remove_voice_files(saved);raise
 await notify(code)
 if is_quiz(s):
  graded=review_for(qs,answers);graded.update(ok=True,quiz=True)
  return graded
 return {'ok':True,'quiz':False}

@app.get('/api/sessions/{code}/result')
def own_result(code:str,participant:str):
 code=code.upper();s=get_session(code)
 with conn() as c:row=c.execute('SELECT id,name,choice,answers,created FROM responses WHERE code=? AND participant=?',(code,participant)).fetchone()
 if not row:raise HTTPException(404,'Response not found')
 if not is_quiz(s):return {'ok':True,'quiz':False}
 qs=questions_of(s)
 graded=review_for(qs,decoded_answers(row,len(qs)));graded.update(ok=True,quiz=True)
 return graded

def check_token(code,token):
 s=get_session(code)
 if not secrets.compare_digest(s['token'],token):raise HTTPException(403,'Invalid lecturer token')
 return s

@app.post('/api/sessions/{code}/close')
async def close(code:str,token:str):
 code=code.upper();check_token(code,token)
 with conn() as c:c.execute('UPDATE sessions SET active=0 WHERE code=?',(code,))
 await notify(code)
 return {'ok':True}

@app.post('/api/sessions/{code}/open')
async def reopen(code:str,token:str):
 code=code.upper();check_token(code,token)
 with conn() as c:c.execute('UPDATE sessions SET active=1 WHERE code=?',(code,))
 await notify(code)
 return {'ok':True}

def sheet_csv(code):
 code=code.upper();s=get_session(code);qs=questions_of(s);rows=response_rows(s,load_rows(code))
 used={};heads=[]
 for q in qs:
  base=q['text'];n=used.get(base,0)+1;used[base]=n
  heads.append(base if n==1 else f'{base} ({n})')
 quiz=is_quiz(s)
 if quiz:heads.extend(['score','points'])
 f=io.StringIO();w=csv.writer(f);w.writerow(['number','timestamp_unix','timestamp','name',*heads])
 for r in rows:
  stamp=time.strftime('%Y-%m-%d %H:%M:%S',time.localtime(r['created']))
  vals=[r['number'],r['created'],stamp,r['name'] or '']
  for i,q in enumerate(qs):vals.append(answer_text(q,r['answers'][i] if i<len(r['answers']) else None))
  if quiz:vals.extend([r.get('score',0),r.get('max',0)])
  w.writerow(vals)
 return Response(content='\ufeff'+f.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="eduvoice_{code}.csv"'})

@app.get('/api/sessions/{code}/sheet')
def sheet(code:str,request:Request,passcode:str=''):
 code=code.upper();s=get_session(code)
 guard_passcode(s,passcode,client_ip(request))
 return sheet_csv(code)

@app.get('/api/sessions/{code}/export')
def export(code:str,token:str,request:Request,passcode:str=''):
 code=code.upper();s=check_token(code,token)
 guard_passcode(s,passcode,client_ip(request))
 return sheet_csv(code)

@app.get('/api/sessions/{code}/voice/{clip}')
def play_voice(code:str,clip:str,request:Request,passcode:str=''):
 code=code.upper()
 if not re.fullmatch(r'[0-9a-f]{32}',clip):raise HTTPException(404,'Recording not found')
 s=get_session(code)
 guard_passcode(s,passcode,client_ip(request))
 with conn() as c:row=c.execute('SELECT mime FROM voice_clips WHERE id=? AND code=?',(clip,code)).fetchone()
 path=AUDIO/f'{clip}.bin'
 if not row or not path.is_file():raise HTTPException(404,'Recording not found')
 return Response(path.read_bytes(),media_type=row['mime'],headers={'Cache-Control':'private, max-age=3600'})

@app.get('/api/sessions/{code}/qr')
def qr(code:str,request:Request):
 get_session(code.upper())
 url=str(request.base_url).rstrip('/')+'/?join='+quote(code.upper())
 im=qrcode.make(url);b=io.BytesIO();im.save(b,format='PNG')
 return Response(b.getvalue(),media_type='image/png')

@app.websocket('/ws/{code}')
async def socket(ws:WebSocket,code:str):
 code=code.upper()
 try:get_session(code)
 except HTTPException:
  await ws.close(code=1008);return
 await ws.accept();clients.setdefault(code,set()).add(ws)
 try:
  full=live(code)
  await ws.send_json(full if not full.get('locked') else redact(full))
  while True:
   raw=await ws.receive_text()
   try:msg=json.loads(raw)
   except Exception:continue
   pin=msg.get('passcode') if isinstance(msg,dict) else ''
   try:guard_passcode(get_session(code),str(pin or ''),client_ip(ws))
   except HTTPException as exc:
    await ws.send_json({'code':code,'locked':True,'revealed':False,'error':exc.detail})
    continue
   opened.setdefault(code,set()).add(ws)
   await ws.send_json(live(code))
 except WebSocketDisconnect:pass
 finally:
  clients.get(code,set()).discard(ws)
  opened.get(code,set()).discard(ws)

@app.get('/health')
def health():return {'status':'ok','max_participants_per_session':MAX_PARTICIPANTS}

import battle
battle.install(app, conn)
