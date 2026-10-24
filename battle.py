import csv, io, json, secrets, time
from urllib.parse import quote
import qrcode
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel, Field

router=APIRouter()
sockets:dict[str,set[WebSocket]]={}
db=None
MAX_PLAYERS=500
MAX_QUESTIONS=30

class BattleQuestion(BaseModel):
 text:str=Field(min_length=1,max_length=300)
 options:list[str]=[]
 correct:int=0
 feedback:str=''
class BattleIn(BaseModel):
 title:str=''
 mode:str='classic'
 seconds:int=20
 rankings:bool=True
 questions:list[BattleQuestion]=Field(min_length=1,max_length=MAX_QUESTIONS)
class JoinIn(BaseModel):
 player:str=Field(min_length=8,max_length=80)
 name:str=Field(min_length=1,max_length=24)
class AnswerIn(BaseModel):
 player:str=Field(min_length=8,max_length=80)
 choice:int
class ControlIn(BaseModel):
 action:str

def install(app, conn):
 global db
 db=conn
 with db() as c:
  c.execute('''CREATE TABLE IF NOT EXISTS battles(code TEXT PRIMARY KEY, token TEXT NOT NULL, title TEXT NOT NULL, mode TEXT NOT NULL, seconds INTEGER NOT NULL, rankings INTEGER NOT NULL, questions TEXT NOT NULL, phase TEXT NOT NULL, qindex INTEGER NOT NULL, started REAL, paused INTEGER NOT NULL DEFAULT 0, remain REAL, active INTEGER NOT NULL DEFAULT 1, created INTEGER NOT NULL)''')
  c.execute('''CREATE TABLE IF NOT EXISTS battle_players(code TEXT NOT NULL, player TEXT NOT NULL, name TEXT NOT NULL, name_key TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0, joined INTEGER NOT NULL, PRIMARY KEY(code, player))''')
  c.execute('CREATE UNIQUE INDEX IF NOT EXISTS battle_name ON battle_players(code, name_key)')
  c.execute('''CREATE TABLE IF NOT EXISTS battle_answers(code TEXT NOT NULL, player TEXT NOT NULL, qindex INTEGER NOT NULL, choice INTEGER NOT NULL, ms INTEGER NOT NULL, correct INTEGER NOT NULL, points INTEGER NOT NULL, PRIMARY KEY(code, player, qindex))''')
 app.include_router(router)

def questions_of(row):
 try:
  data=json.loads(row['questions'])
  if isinstance(data,list):return data
 except Exception:pass
 return []

def clean_questions(items):
 if not 1<=len(items)<=MAX_QUESTIONS:raise HTTPException(400,'Add between 1 and 30 questions')
 out=[]
 for q in items:
  text=q.text.strip()
  if not 3<=len(text)<=300:raise HTTPException(400,'Each question needs between 3 and 300 characters')
  options=[x.strip() for x in q.options if x.strip()]
  if not 2<=len(options)<=4:raise HTTPException(400,'Each question needs 2 to 4 choices')
  if any(len(x)>80 for x in options):raise HTTPException(400,'A choice is too long')
  if len({x.casefold() for x in options})!=len(options):raise HTTPException(400,'Choices in one question must differ')
  if not isinstance(q.correct,int) or isinstance(q.correct,bool) or not 0<=q.correct<len(options):raise HTTPException(400,'Mark the correct answer')
  feedback=q.feedback.strip()[:300] if isinstance(q.feedback,str) else ''
  out.append({'text':text,'options':options,'correct':q.correct,'feedback':feedback})
 return out

def score_points(mode, correct, elapsed, limit):
 if not correct:return 0
 if mode=='accuracy':return 1000
 t=max(0.0,min(float(limit),float(elapsed)))
 return int(round(1000*(1-t/(2*float(limit)))))

def name_key(name):
 return ' '.join(name.strip().casefold().split())

def row_of(c, code):
 row=c.execute('SELECT * FROM battles WHERE code=?',(code,)).fetchone()
 if not row:raise HTTPException(404,'Battle not found')
 return row

def expire(c, row):
 if row['phase']=='question' and not row['paused'] and row['started'] and time.time()>=row['started']+row['seconds']:
  c.execute("UPDATE battles SET phase='reveal', paused=0 WHERE code=? AND phase='question'",(row['code'],))
  return True
 return False

def players(c, code):
 return c.execute('SELECT player, name, score, joined FROM battle_players WHERE code=? ORDER BY score DESC, joined, name',(code,)).fetchall()

def answer_row(c, code, player, index):
 return c.execute('SELECT choice, correct, points, ms FROM battle_answers WHERE code=? AND player=? AND qindex=?',(code,player,index)).fetchone()

def counts_for(c, code, index, n):
 counts=[0]*n
 for choice, in c.execute('SELECT choice FROM battle_answers WHERE code=? AND qindex=?',(code,index)):
  if isinstance(choice,int) and 0<=choice<n:counts[choice]+=1
 return counts

def view(c, row, player='', host=False):
 qs=questions_of(row);phase=row['phase'];index=row['qindex'];people=players(c,row['code'])
 now=time.time();limit=row['seconds']
 if phase=='question' and row['paused']:remaining=max(0,int(round(row['remain'] or 0)))
 elif phase=='question' and row['started']:remaining=max(0,int(round(row['started']+limit-now)))
 else:remaining=limit if phase=='lobby' else 0
 show_key=phase in ('reveal','podium')
 q=qs[index] if 0<=index<len(qs) else None
 options=list(q['options']) if q else []
 board=[{'name':p['name'],'score':p['score']} for p in people]
 mine=None
 if player:
  who=next((p for p in people if p['player']==player),None)
  if who:
   mine={'name':who['name'],'score':who['score']}
   if q and phase in ('question','reveal'):
    ans=answer_row(c,row['code'],player,index)
    if ans:
     shown=ans['points'] if show_key else 0
     mine['score']=who['score']-ans['points']+shown
     mine.update(choice=ans['choice'],locked=True)
     if show_key:mine.update(points=ans['points'],correct=bool(ans['correct']))
    else:mine.update(locked=False)
 show_ranks=host or (bool(row['rankings']) and phase in ('reveal','podium'))
 payload={'code':row['code'],'title':row['title'],'mode':row['mode'],'rankings':bool(row['rankings']),'phase':phase,'index':index,'total':len(qs),'seconds':limit,'remaining':remaining,'paused':bool(row['paused']),'players':len(people),'answered':0,'question':None,'counts':[],'leaderboard':board if show_ranks else [],'you':mine,'active':bool(row['active'])}
 if phase=='lobby':payload['names']=[p['name'] for p in people]
 if q and phase in ('question','reveal'):
  payload['question']={'text':q['text'],'options':options}
  payload['answered']=c.execute('SELECT COUNT(*) FROM battle_answers WHERE code=? AND qindex=?',(row['code'],index)).fetchone()[0]
  if show_key:
   payload['question']['correct']=q['correct']
   payload['question']['feedback']=q.get('feedback') or ''
   payload['counts']=counts_for(c,row['code'],index,len(options))
 if phase=='podium':
  if show_ranks:payload['podium']=board[:3]
  hard=[]
  for i,item in enumerate(qs):
   n=c.execute('SELECT COUNT(*) FROM battle_answers WHERE code=? AND qindex=?',(row['code'],i)).fetchone()[0]
   ok=c.execute('SELECT COUNT(*) FROM battle_answers WHERE code=? AND qindex=? AND correct=1',(row['code'],i)).fetchone()[0]
   answer=item['options'][item['correct']] if 0<=item['correct']<len(item['options']) else ''
   hard.append({'text':item['text'],'answer':answer,'feedback':item.get('feedback') or '','correct':ok,'answered':n})
  payload['report']=hard
 return payload

def load(code, player='', host=False):
 code=code.upper();changed=False
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  row=row_of(c,code)
  changed=expire(c,row)
  if changed:row=row_of(c,code)
  payload=view(c,row,player,host)
 return changed,payload

async def notify(code):
 code=code.upper()
 try:_,payload=load(code)
 except HTTPException:return
 for ws in list(sockets.get(code,set())):
  try:await ws.send_json(payload)
  except Exception:sockets.get(code,set()).discard(ws)

def check_token(code, token):
 with db() as c:
  row=row_of(c,code)
 if not secrets.compare_digest(row['token'],token or ''):raise HTTPException(403,'Invalid host token')
 return row

@router.post('/api/battles')
def create_battle(data:BattleIn):
 if data.mode not in ('classic','accuracy'):raise HTTPException(400,'Choose speed battle or accuracy')
 if data.seconds not in (10,20,30,45,60):raise HTTPException(400,'Choose 10, 20, 30, 45, or 60 seconds')
 qs=clean_questions(data.questions)
 title=(data.title or '').strip()[:120] or 'Quiz battle'
 with db() as c:
  code=None
  for _ in range(8):
   guess=secrets.token_hex(3).upper()
   if c.execute('SELECT 1 FROM sessions WHERE code=?',(guess,)).fetchone():continue
   if c.execute('SELECT 1 FROM battles WHERE code=?',(guess,)).fetchone():continue
   code=guess;break
  if not code:raise HTTPException(500,'Could not create a code')
  token=secrets.token_urlsafe(24)
  c.execute('INSERT INTO battles(code,token,title,mode,seconds,rankings,questions,phase,qindex,started,paused,remain,active,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(code,token,title,data.mode,data.seconds,1 if data.rankings else 0,json.dumps(qs,ensure_ascii=False),'lobby',-1,None,0,None,1,int(time.time())))
 return {'code':code,'token':token}

@router.get('/api/battles')
def list_battles():
 with db() as c:
  rows=c.execute('SELECT code, title, mode, phase, active, created, questions FROM battles ORDER BY created DESC, code').fetchall()
  counts={r['code']:r['n'] for r in c.execute('SELECT code, COUNT(*) AS n FROM battle_players GROUP BY code')}
 out=[]
 for row in rows:
  out.append({'code':row['code'],'title':row['title'],'mode':row['mode'],'phase':row['phase'],'active':bool(row['active']),'created':row['created'],'questions':len(questions_of(row)),'players':counts.get(row['code'],0)})
 return out

@router.get('/api/battles/{code}')
async def read_battle(code:str, player:str=''):
 changed,payload=load(code,player)
 if changed:await notify(code)
 return payload

@router.get('/api/battles/{code}/host')
def read_host(code:str, token:str=''):
 check_token(code.upper(),token)
 _,payload=load(code,host=True)
 payload['host']=True
 return payload

@router.post('/api/battles/{code}/join')
async def join_battle(code:str, data:JoinIn):
 code=code.upper();name=' '.join(data.name.split());key=name_key(name)
 if len(name)<2:raise HTTPException(400,'Enter a nickname')
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  row=row_of(c,code)
  if row['phase']=='podium' or not row['active']:raise HTTPException(400,'This battle has ended')
  existing=c.execute('SELECT player, name FROM battle_players WHERE code=? AND player=?',(code,data.player)).fetchone()
  if existing:pass
  else:
   taken=c.execute('SELECT player FROM battle_players WHERE code=? AND name_key=?',(code,key)).fetchone()
   if taken:raise HTTPException(409,'That name is already in the game')
   n=c.execute('SELECT COUNT(*) FROM battle_players WHERE code=?',(code,)).fetchone()[0]
   if n>=MAX_PLAYERS:raise HTTPException(409,'This battle is full')
   c.execute('INSERT INTO battle_players(code,player,name,name_key,score,joined) VALUES (?,?,?,?,0,?)',(code,data.player,name,key,int(time.time())))
 await notify(code)
 _,payload=load(code,data.player)
 return payload

@router.post('/api/battles/{code}/answer')
async def answer_battle(code:str, data:AnswerIn):
 code=code.upper();late=False
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  row=row_of(c,code)
  if expire(c,row):late=True
  else:
   row=row_of(c,code)
   if row['phase']!='question':raise HTTPException(400,'Wait for the question')
   if row['paused']:raise HTTPException(400,'The question is paused')
   who=c.execute('SELECT name FROM battle_players WHERE code=? AND player=?',(code,data.player)).fetchone()
   if not who:raise HTTPException(404,'Join the battle first')
   qs=questions_of(row);index=row['qindex']
   if not 0<=index<len(qs):raise HTTPException(400,'Wait for the question')
   options=qs[index]['options']
   if not isinstance(data.choice,int) or isinstance(data.choice,bool) or not 0<=data.choice<len(options):raise HTTPException(400,'Choose one answer')
   if c.execute('SELECT 1 FROM battle_answers WHERE code=? AND player=? AND qindex=?',(code,data.player,index)).fetchone():raise HTTPException(409,'You already answered')
   elapsed=max(0.0,time.time()-(row['started'] or time.time()))
   if elapsed>row['seconds']:late=True
   else:
    ok=data.choice==qs[index]['correct']
    pts=score_points(row['mode'],ok,elapsed,row['seconds'])
    c.execute('INSERT INTO battle_answers(code,player,qindex,choice,ms,correct,points) VALUES (?,?,?,?,?,?,?)',(code,data.player,index,data.choice,int(elapsed*1000),1 if ok else 0,pts))
    c.execute('UPDATE battle_players SET score=score+? WHERE code=? AND player=?',(pts,code,data.player))
 if late:
  await notify(code)
  raise HTTPException(400,'Time is up')
 await notify(code)
 _,payload=load(code,data.player)
 return payload

@router.post('/api/battles/{code}/control')
async def control_battle(code:str, data:ControlIn, token:str=''):
 code=code.upper();check_token(code,token);action=data.action
 with db() as c:
  c.execute('BEGIN IMMEDIATE')
  row=row_of(c,code);expire(c,row);row=row_of(c,code)
  qs=questions_of(row);phase=row['phase'];index=row['qindex']
  applied=False
  if action=='start' and phase=='lobby':
   c.execute("UPDATE battles SET phase='question', qindex=0, started=?, paused=0, remain=NULL WHERE code=?",(time.time(),code));applied=True
  elif action=='pause' and phase=='question' and not row['paused']:
   remain=max(0.0,(row['started'] or time.time())+row['seconds']-time.time())
   c.execute('UPDATE battles SET paused=1, remain=? WHERE code=?',(remain,code));applied=True
  elif action=='resume' and phase=='question' and row['paused']:
   remain=row['remain'] if row['remain'] is not None else row['seconds']
   c.execute('UPDATE battles SET paused=0, started=?, remain=NULL WHERE code=?',(time.time()-(row['seconds']-remain),code));applied=True
  elif action=='reveal' and phase=='question':
   c.execute("UPDATE battles SET phase='reveal', paused=0 WHERE code=?",(code,));applied=True
  elif action=='next' and phase=='reveal':
   if index+1>=len(qs):c.execute("UPDATE battles SET phase='podium', paused=0, active=0 WHERE code=?",(code,))
   else:c.execute("UPDATE battles SET phase='question', qindex=?, started=?, paused=0, remain=NULL WHERE code=?",(index+1,time.time(),code))
   applied=True
  elif action=='end':
   c.execute("UPDATE battles SET phase='podium', paused=0, active=0 WHERE code=?",(code,));applied=True
 if not applied:raise HTTPException(400,'That control is not available now')
 await notify(code)
 _,payload=load(code,host=True)
 payload['host']=True
 return payload

@router.get('/api/battles/{code}/qr')
def battle_qr(code:str, request:Request):
 code=code.upper()
 with db() as c:row_of(c,code)
 url=str(request.base_url).rstrip('/')+'/?play='+quote(code)
 im=qrcode.make(url);buf=io.BytesIO();im.save(buf,format='PNG')
 return Response(buf.getvalue(),media_type='image/png')

@router.get('/api/battles/{code}/history')
def battle_history(code:str, token:str=''):
 code=code.upper();check_token(code,token)
 with db() as c:
  row=row_of(c,code);qs=questions_of(row);people=players(c,code)
  answers=c.execute('SELECT player, qindex, choice, points, correct FROM battle_answers WHERE code=?',(code,)).fetchall()
 by={(a['player'],a['qindex']):a for a in answers}
 questions=[]
 for i,q in enumerate(qs):
  got=[a for a in answers if a['qindex']==i]
  answer=q['options'][q['correct']] if 0<=q['correct']<len(q['options']) else ''
  questions.append({'text':q['text'],'answer':answer,'feedback':q.get('feedback') or '','answered':len(got),'correct':sum(1 for a in got if a['correct'])})
 responses=[]
 for n,p in enumerate(people,1):
  cells=[]
  for i,q in enumerate(qs):
   a=by.get((p['player'],i))
   if not a:cells.append(None);continue
   text=q['options'][a['choice']] if 0<=a['choice']<len(q['options']) else ''
   cells.append({'text':text,'points':a['points'],'correct':bool(a['correct'])})
  responses.append({'number':n,'name':p['name'],'score':p['score'],'created':p['joined'],'answers':cells})
 return {'code':code,'title':row['title'],'mode':row['mode'],'phase':row['phase'],'active':bool(row['active']),'created':row['created'],'seconds':row['seconds'],'players':len(people),'questions':questions,'responses':responses}

@router.get('/api/battles/{code}/sheet')
def battle_sheet(code:str, token:str=''):
 code=code.upper();check_token(code,token)
 with db() as c:
  row=row_of(c,code);qs=questions_of(row);people=players(c,code)
  answers=c.execute('SELECT player, qindex, choice, points, correct FROM battle_answers WHERE code=?',(code,)).fetchall()
 by={(a['player'],a['qindex']):a for a in answers}
 heads=['name','score']
 for i,q in enumerate(qs,1):heads.extend([f'q{i}',f'q{i}_points',f'q{i}_correct'])
 f=io.StringIO();w=csv.writer(f);w.writerow(heads)
 for p in people:
  vals=[p['name'],p['score']]
  for i,q in enumerate(qs):
   a=by.get((p['player'],i))
   if not a:vals.extend(['','',''])
   else:
    choice=q['options'][a['choice']] if 0<=a['choice']<len(q['options']) else ''
    vals.extend([choice,a['points'],'yes' if a['correct'] else 'no'])
  w.writerow(vals)
 data=f.getvalue().encode('utf-8-sig')
 return Response(data,media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{code}-battle.csv"'})

@router.websocket('/ws/battle/{code}')
async def battle_socket(ws:WebSocket, code:str):
 code=code.upper()
 try:
  with db() as c:row_of(c,code)
 except HTTPException:
  await ws.close(code=1008);return
 await ws.accept();sockets.setdefault(code,set()).add(ws)
 try:
  _,payload=load(code)
  await ws.send_json(payload)
  while True:await ws.receive_text()
 except WebSocketDisconnect:pass
 finally:sockets.get(code,set()).discard(ws)
