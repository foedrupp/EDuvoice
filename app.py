import csv, io, json, os, secrets, sqlite3, time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote
import qrcode
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT=Path(__file__).parent
DB=Path(os.getenv('EDUVOICE_DB',str(ROOT/'eduvoice.sqlite3')))
MAX_PARTICIPANTS=500
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
init()
with conn() as c:c.execute('PRAGMA journal_mode=WAL')

class Create(BaseModel):
 question:str=Field(min_length=3,max_length=500)
 options:list[str]=Field(min_length=2,max_length=8)
 mode:str='anonymous'
class Vote(BaseModel):
 participant:str=Field(min_length=8,max_length=100)
 choice:int
 name:str|None=None

clients:dict[str,set[WebSocket]]={}

def get_session(code):
 with conn() as c: r=c.execute('SELECT * FROM sessions WHERE code=?',(code.upper(),)).fetchone()
 if not r:raise HTTPException(404,'Session not found')
 return dict(r)

def state(code,include_private=False):
 s=get_session(code)
 with conn() as c: rows=c.execute('SELECT choice,COUNT(*) AS n FROM responses WHERE code=? GROUP BY choice',(code,)).fetchall()
 counts=[0]*len(json.loads(s['options']))
 for r in rows:
  if 0<=r['choice']<len(counts):counts[r['choice']]=r['n']
 return {'code':code,'question':s['question'],'options':json.loads(s['options']),'mode':s['mode'],'active':bool(s['active']),'counts':counts,'total':sum(counts),'capacity':MAX_PARTICIPANTS,'remaining':MAX_PARTICIPANTS-sum(counts)}

async def notify(code):
 for ws in list(clients.get(code,set())):
  try:await ws.send_json(state(code))
  except Exception:clients.get(code,set()).discard(ws)

@app.get('/')
def home():return HTMLResponse((ROOT/'static'/'index.html').read_text(encoding='utf-8'))

@app.post('/api/sessions')
async def create(data:Create):
 if data.mode not in ('anonymous','named'):raise HTTPException(400,'Invalid mode')
 options=[x.strip() for x in data.options]
 if any(not x or len(x)>160 for x in options):raise HTTPException(400,'Invalid options')
 code=secrets.token_hex(3).upper();token=secrets.token_urlsafe(24)
 with conn() as c:c.execute('INSERT INTO sessions VALUES (?,?,?,?,?,?,?)',(code,token,data.question.strip(),json.dumps(options,ensure_ascii=False),data.mode,1,int(time.time())))
 return {'code':code,'token':token}

@app.get('/api/sessions/{code}')
def read(code:str):return state(code.upper())

@app.post('/api/sessions/{code}/vote')
async def vote(code:str,data:Vote):
 code=code.upper();s=get_session(code)
 if not s['active']:raise HTTPException(400,'Poll is closed')
 if not 0<=data.choice<len(json.loads(s['options'])):raise HTTPException(400,'Invalid choice')
 name=(data.name or '').strip()
 if s['mode']=='named' and not name:raise HTTPException(400,'Name is required')
 # In anonymous mode no student name is persisted; random participant key enforces one vote per browser.
 # Serialize capacity checks and inserts across concurrent requests.
 with conn() as c:
  try:
   c.execute('BEGIN IMMEDIATE')
   n=c.execute('SELECT COUNT(*) FROM responses WHERE code=?',(code,)).fetchone()[0]
   if n>=MAX_PARTICIPANTS:raise HTTPException(409,'This session has reached its 500-participant limit')
   c.execute('INSERT INTO responses(code,participant,name,choice,created) VALUES(?,?,?,?,?)',(code,data.participant,name if s['mode']=='named' else None,data.choice,int(time.time())))
  except sqlite3.IntegrityError:raise HTTPException(409,'This browser has already submitted a response')
 await notify(code)
 return {'ok':True}

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

@app.get('/api/sessions/{code}/export')
def export(code:str,token:str):
 code=code.upper();s=check_token(code,token);opts=json.loads(s['options'])
 with conn() as c:rows=c.execute('SELECT choice,name,created FROM responses WHERE code=? ORDER BY id',(code,)).fetchall()
 f=io.StringIO();w=csv.writer(f);w.writerow(['session','question','option','name','timestamp_unix'])
 for r in rows:w.writerow([code,s['question'],opts[r['choice']],r['name'] if s['mode']=='named' else '',r['created']])
 return Response(content='\ufeff'+f.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="eduvoice_{code}.csv"'})

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
  await ws.send_json(state(code))
  while True:await ws.receive_text()
 except WebSocketDisconnect:pass
 finally:clients.get(code,set()).discard(ws)

@app.get('/health')
def health():return {'status':'ok','max_participants_per_session':MAX_PARTICIPANTS}
