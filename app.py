import os,sqlite3,json,secrets,random,re,hashlib,datetime,io,csv,shutil,time
from functools import wraps
from flask import Flask,request,session,redirect,url_for,render_template,flash,send_file,abort
from werkzeug.security import generate_password_hash,check_password_hash
from werkzeug.utils import secure_filename
from importer import parse,TYPES
BASE=os.path.dirname(os.path.abspath(__file__))
DB=os.environ.get('EXAM_DB','/tmp/exam_platform.sqlite')
if not os.path.exists(DB):
 os.makedirs(os.path.dirname(os.path.abspath(DB)),exist_ok=True)
 shutil.copyfile(os.path.join(BASE,'questions_seed.sqlite'),DB)
app=Flask(__name__);app.secret_key=os.environ.get('SECRET_KEY') or secrets.token_hex(32);app.config.update(MAX_CONTENT_LENGTH=25*1024*1024,SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SAMESITE='Lax',SESSION_COOKIE_SECURE=os.environ.get('PUBLIC_HTTPS','1')=='1')
_login_attempts={}
LABELS={'single':'Выбор ответа','matching':'Соответствие','sequence':'Последовательность','fill':'Дополнение','short':'Краткий ответ','essay':'Развёрнутый ответ'}
app.jinja_env.globals['labels']=LABELS

def db():
 c=sqlite3.connect(DB);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');return c

def init():
 with db() as c:
  c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'student',group_name TEXT NOT NULL DEFAULT 'Общая',full_name TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS questions(id INTEGER PRIMARY KEY,subject TEXT NOT NULL,type TEXT NOT NULL,question TEXT NOT NULL,options TEXT NOT NULL DEFAULT '[]',answer TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT '',image TEXT DEFAULT '',reviewed INTEGER DEFAULT 0,active INTEGER DEFAULT 1,hash TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,started TEXT NOT NULL,finished TEXT,subject TEXT,FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS responses(id INTEGER PRIMARY KEY,attempt_id INTEGER NOT NULL,question_id INTEGER NOT NULL,answer TEXT,correct INTEGER,teacher_note TEXT DEFAULT '',FOREIGN KEY(attempt_id) REFERENCES attempts(id),FOREIGN KEY(question_id) REFERENCES questions(id),UNIQUE(attempt_id,question_id));''')
  if not c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
   pw=os.environ.get('ADMIN_PASSWORD')
   if not pw or len(pw)<12:raise RuntimeError('Укажите ADMIN_PASSWORD длиной минимум 12 символов в настройках Render')
   c.execute('INSERT INTO users(username,password,role,full_name) VALUES(?,?,?,?)',('admin',generate_password_hash(pw),'admin','Администратор'))
   print('Создан администратор admin; пароль задан переменной окружения.')

def login_required(fn):
 @wraps(fn)
 def w(*a,**kw):
  if not session.get('uid'):return redirect(url_for('login'))
  return fn(*a,**kw)
 return w

def admin_required(fn):
 @wraps(fn)
 @login_required
 def w(*a,**kw):
  if session.get('role')!='admin':abort(403)
  return fn(*a,**kw)
 return w

def insert_questions(records):
 added=0
 with db() as c:
  for q in records:
   raw=json.dumps([q['subject'].lower().strip(),q['type'],re.sub(r'\s+',' ',q['question']).lower().strip()],ensure_ascii=False)
   h=hashlib.sha256(raw.encode()).hexdigest()
   cur=c.execute('INSERT OR IGNORE INTO questions(subject,type,question,options,answer,source,hash) VALUES(?,?,?,?,?,?,?)',(q['subject'],q['type'],q['question'],json.dumps(q['options'],ensure_ascii=False),q['answer'],q['source'],h))
   added+=cur.rowcount
 return added

@app.before_request
def csrf_protect():
 if request.method in ('POST','PUT','PATCH','DELETE'):
  if not secrets.compare_digest(session.get('_csrf',''),request.form.get('_csrf','')) or not session.get('_csrf'):
   abort(400,'Недействительный токен формы')

@app.context_processor
def csrf_context():
 if '_csrf' not in session:session['_csrf']=secrets.token_urlsafe(32)
 return {'csrf_token':session['_csrf']}

@app.context_processor
def common():return {'me':session.get('username'),'is_admin':session.get('role')=='admin'}
@app.route('/login',methods=['GET','POST'])
def login():
 if request.method=='POST':
  ip=request.remote_addr or 'unknown';now=time.monotonic()
  recent=[x for x in _login_attempts.get(ip,[]) if now-x<600]
  if len(recent)>=12:abort(429,'Слишком много попыток входа, попробуйте позже')
  recent.append(now);_login_attempts[ip]=recent
  with db() as c:u=c.execute('SELECT * FROM users WHERE username=?',(request.form.get('username',''),)).fetchone()
  if u and check_password_hash(u['password'],request.form.get('password','')):
   session.clear();session.update(uid=u['id'],username=u['username'],role=u['role'],_csrf=secrets.token_urlsafe(32));_login_attempts.pop(ip,None);return redirect(url_for('home'))
  flash('Неверный логин или пароль')
 return render_template('login.html')
@app.route('/logout')
def logout():session.clear();return redirect(url_for('login'))
@app.route('/')
@login_required
def home():
 with db() as c:
  subjects=c.execute('SELECT subject,count(*) n FROM questions WHERE active=1 GROUP BY subject ORDER BY subject').fetchall()
  recent=c.execute('SELECT * FROM attempts WHERE user_id=? ORDER BY id DESC LIMIT 5',(session['uid'],)).fetchall()
 return render_template('home.html',subjects=subjects,recent=recent)

def select_questions(c,subject):
 rows=c.execute('SELECT id,type FROM questions WHERE active=1 '+('AND subject=?' if subject else ''),((subject,) if subject else ())).fetchall()
 pools={t:[r['id'] for r in rows if r['type']==t] for t in TYPES}
 for ids in pools.values():random.shuffle(ids)
 chosen=[]
 # Fair round robin. When a type is exhausted, redistribute to other types.
 while len(chosen)<50 and any(pools.values()):
  order=TYPES[:];random.shuffle(order)
  for t in order:
   if pools[t] and len(chosen)<50:chosen.append(pools[t].pop())
 random.shuffle(chosen)
 return chosen
@app.route('/start',methods=['POST'])
@login_required
def start():
 subject=request.form.get('subject','')
 with db() as c:
  ids=select_questions(c,subject)
  if not ids:flash('В базе пока нет вопросов');return redirect(url_for('home'))
  a=c.execute('INSERT INTO attempts(user_id,started,subject) VALUES(?,?,?)',(session['uid'],datetime.datetime.now().isoformat(timespec='seconds'),subject or 'Все предметы')).lastrowid
  c.executemany('INSERT INTO responses(attempt_id,question_id) VALUES(?,?)',[(a,q) for q in ids])
 session['order_'+str(a)]=ids
 return redirect(url_for('test',aid=a))

def owned(c,aid):
 a=c.execute('SELECT * FROM attempts WHERE id=?',(aid,)).fetchone()
 if not a or (a['user_id']!=session['uid'] and session.get('role')!='admin'):abort(403)
 return a

def norm(s):return re.sub(r'\s+','',str(s).strip().casefold().replace('ё','е'))
def score(q,ans):
 if q['type']=='essay':return None
 if not ans.strip():return 0
 key=q['answer'] or ''
 if q['type']=='single':
  opts=json.loads(q['options']); keys=[o['id'] for o in opts]
  def convert(x):
   x=x.strip().upper()
   if x in keys:return x
   mapping={'A':'А','B':'В','C':'С','D':'Д','E':'Е','F':'Ф','G':'Г'}
   return mapping.get(x,x)
  k=[convert(x) for x in re.split(r'[,;\s]+',key) if x.strip()]
  a=[convert(x) for x in re.split(r'[,;\s]+',ans) if x.strip()]
  return int(set(k)==set(a))
 if q['type'] in ('matching','sequence'):
  return int(norm(re.sub(r'[^\wА-Яа-яЁё]+','',ans))==norm(re.sub(r'[^\wА-Яа-яЁё]+','',key)))
 variants=[v.strip() for v in re.split(r'\s+/\s+|\s*;\s*',key) if v.strip()]
 return int(any(norm(ans)==norm(v) for v in variants))
@app.route('/test/<int:aid>',methods=['GET','POST'])
@login_required
def test(aid):
 with db() as c:
  attempt=owned(c,aid)
  if attempt['finished']:return redirect(url_for('result',aid=aid))
  rows=c.execute('SELECT r.*,q.type,q.question,q.options,q.answer AS key_answer,q.image,q.source FROM responses r JOIN questions q ON q.id=r.question_id WHERE r.attempt_id=?',(aid,)).fetchall()
  order=session.get('order_'+str(aid)) or [r['question_id'] for r in rows]
  byid={r['question_id']:r for r in rows};rows=[byid[i] for i in order if i in byid]
  pos=max(0,min(int(request.values.get('n',0)),len(rows)-1));r=rows[pos]
  if request.method=='POST':
   answer=request.form.get('answer','').strip()
   if True:
    q={'type':r['type'],'answer':r['key_answer'],'options':r['options']}
    correct=score(q,answer)
    c.execute('UPDATE responses SET answer=?,correct=? WHERE id=?',(answer,correct,r['id']))
   if request.form.get('finish')=='1':
    c.execute('UPDATE attempts SET finished=? WHERE id=?',(datetime.datetime.now().isoformat(timespec='seconds'),aid));return redirect(url_for('result',aid=aid))
   return redirect(url_for('test',aid=aid,n=pos,feedback=1))
 return render_template('test.html',r=r,aid=aid,n=pos,total=len(rows),options=json.loads(r['options']),answered=sum(x['answer'] is not None for x in rows),feedback=request.args.get('feedback')=='1')
@app.route('/result/<int:aid>')
@login_required
def result(aid):
 with db() as c:
  a=owned(c,aid)
  rs=c.execute('SELECT r.*,q.question,q.type,q.answer AS key_answer FROM responses r JOIN questions q ON q.id=r.question_id WHERE attempt_id=?',(aid,)).fetchall()
  good=sum(r['correct']==1 for r in rs);pending=sum(r['correct'] is None for r in rs);total=len(rs)
 return render_template('result.html',a=a,rs=rs,good=good,pending=pending,total=total)
@app.route('/admin')
@admin_required
def admin():
 with db() as c:
  stats=c.execute('''SELECT u.id,u.username,u.full_name,u.group_name,COUNT(DISTINCT a.id) attempts,COUNT(r.id) questions,SUM(CASE WHEN r.correct=1 THEN 1 ELSE 0 END) correct FROM users u LEFT JOIN attempts a ON a.user_id=u.id AND a.finished IS NOT NULL LEFT JOIN responses r ON r.attempt_id=a.id WHERE u.role='student' GROUP BY u.id ORDER BY u.group_name,u.full_name''').fetchall()
  users=c.execute('SELECT id,username,full_name,group_name,role FROM users ORDER BY group_name,username').fetchall()
  count=c.execute('SELECT count(*) FROM questions').fetchone()[0]
 return render_template('admin.html',stats=stats,users=users,count=count)
@app.route('/admin/users',methods=['POST'])
@admin_required
def users():
 username=request.form.get('username','').strip();password=request.form.get('password','');role=request.form.get('role','student')
 if not username or not password or role not in ('student','admin'):flash('Проверьте данные');return redirect(url_for('admin'))
 try:
  with db() as c:c.execute('INSERT INTO users(username,password,role,group_name,full_name) VALUES(?,?,?,?,?)',(username,generate_password_hash(password),role,request.form.get('group_name','Общая'),request.form.get('full_name','')))
  flash('Пользователь добавлен')
 except sqlite3.IntegrityError:flash('Логин уже занят')
 return redirect(url_for('admin'))
@app.route('/admin/password',methods=['POST'])
@admin_required
def password():
 with db() as c:c.execute('UPDATE users SET password=? WHERE id=?',(generate_password_hash(request.form['password']),request.form['user_id']))
 flash('Пароль изменён');return redirect(url_for('admin'))
@app.route('/admin/questions')
@admin_required
def questions():
 search=request.args.get('q','');subject=request.args.get('subject','')
 with db() as c:
  rows=c.execute('SELECT * FROM questions WHERE question LIKE ? AND subject LIKE ? ORDER BY id DESC LIMIT 250',('%'+search+'%','%'+subject+'%')).fetchall()
  subs=c.execute('SELECT DISTINCT subject FROM questions ORDER BY subject').fetchall()
 return render_template('questions.html',rows=rows,subs=subs)
@app.route('/admin/question/<int:qid>',methods=['GET','POST'])
@admin_required
def edit(qid):
 with db() as c:
  q=c.execute('SELECT * FROM questions WHERE id=?',(qid,)).fetchone() if qid else None
  if request.method=='POST':
   subject=request.form['subject'].strip();typ=request.form['type'];question=request.form['question'].strip();answer=request.form.get('answer','').strip();opts=request.form.get('options','')
   if typ not in TYPES:abort(400)
   try:options=json.loads(opts or '[]');assert isinstance(options,list)
   except Exception:flash('Варианты ответа: неверный JSON');return redirect(request.url)
   image=q['image'] if q else ''
   f=request.files.get('image')
   if f and f.filename:
    ext=os.path.splitext(secure_filename(f.filename))[1].lower()
    if ext not in ('.png','.jpg','.jpeg','.webp','.gif'):abort(400)
    image=secrets.token_hex(12)+ext;f.save(os.path.join(BASE,'uploads',image))
   vals=(subject,typ,question,json.dumps(options,ensure_ascii=False),answer,image,int('reviewed' in request.form),int('active' in request.form))
   if q:c.execute('UPDATE questions SET subject=?,type=?,question=?,options=?,answer=?,image=?,reviewed=?,active=? WHERE id=?',vals+(qid,))
   else:c.execute('INSERT INTO questions(subject,type,question,options,answer,image,reviewed,active) VALUES(?,?,?,?,?,?,?,?)',vals)
   flash('Сохранено');return redirect(url_for('questions'))
 return render_template('edit.html',q=q,types=TYPES)
@app.route('/admin/import',methods=['POST'])
@admin_required
def import_file():
 f=request.files.get('file')
 if not f or not f.filename:abort(400)
 ext=os.path.splitext(f.filename)[1].lower()
 if ext not in ('.docx','.pdf'):flash('Поддерживаются DOCX и PDF');return redirect(url_for('admin'))
 name=secure_filename(f.filename) or ('upload'+ext);path=os.path.join(BASE,'uploads',secrets.token_hex(8)+'_'+name);f.save(path)
 try:
  if ext=='.docx':records=parse(path)
  else:
   import fitz
   doc=fitz.open(path);text='\n'.join(page.get_text() for page in doc)
   # PDF text is staged as a DOCX so the same conservative parser applies.
   from docx import Document
   temp=Document()
   for line in text.splitlines():temp.add_paragraph(line)
   p=path+'.docx';temp.save(p);records=parse(p)
   for r in records:r['source']=f.filename
  count=insert_questions(records)
  flash(f'Найдено {len(records)} заданий; добавлено {count}. Проверьте корректность распознавания в банке вопросов.')
 except Exception as e:flash('Ошибка импорта: '+str(e)[:180])
 return redirect(url_for('admin'))
@app.route('/admin/grade/<int:rid>',methods=['POST'])
@admin_required
def grade(rid):
 val=int(request.form.get('correct',0))
 with db() as c:c.execute('UPDATE responses SET correct=?,teacher_note=? WHERE id=?',(val,request.form.get('note',''),rid))
 return redirect(request.referrer or url_for('admin'))
@app.route('/admin/attempt/<int:aid>')
@admin_required
def review_attempt(aid):
 with db() as c:
  a=c.execute('SELECT a.*,u.username,u.full_name FROM attempts a JOIN users u ON u.id=a.user_id WHERE a.id=?',(aid,)).fetchone()
  if not a:abort(404)
  rs=c.execute('SELECT r.*,q.question,q.type,q.answer AS key_answer FROM responses r JOIN questions q ON q.id=r.question_id WHERE attempt_id=?',(aid,)).fetchall()
 return render_template('review.html',a=a,rs=rs)
@app.route('/admin/attempts')
@admin_required
def attempts():
 with db() as c:
  rs=c.execute('''SELECT a.*,u.username,u.full_name,u.group_name,COUNT(r.id) total,SUM(CASE WHEN r.correct=1 THEN 1 ELSE 0 END) good FROM attempts a JOIN users u ON u.id=a.user_id LEFT JOIN responses r ON r.attempt_id=a.id GROUP BY a.id ORDER BY a.id DESC LIMIT 300''').fetchall()
 return render_template('attempts.html',rs=rs)
@app.route('/admin/export')
@admin_required
def export():
 with db() as c:
  rows=c.execute('''SELECT u.group_name,u.username,u.full_name,a.id,a.subject,a.started,a.finished,COUNT(r.id) total,SUM(CASE WHEN r.correct=1 THEN 1 ELSE 0 END) correct,SUM(CASE WHEN r.correct IS NULL THEN 1 ELSE 0 END) pending FROM attempts a JOIN users u ON u.id=a.user_id JOIN responses r ON r.attempt_id=a.id GROUP BY a.id ORDER BY a.id DESC''').fetchall()
 s=io.StringIO();w=csv.writer(s);w.writerow(['Группа','Логин','ФИО','Тест','Предмет','Начало','Завершение','Вопросов','Верно','Ожидают проверки','Процент'])
 for r in rows:w.writerow(list(r)+[round(100*r['correct']/r['total'],1) if r['total'] else 0])
 return send_file(io.BytesIO(s.getvalue().encode('utf-8-sig')),mimetype='text/csv',as_attachment=True,download_name='results.csv')
@app.route('/uploads/<path:name>')
@login_required
def upload(name):
 from flask import send_from_directory
 return send_from_directory(os.path.join(BASE,'uploads'),name)
init()
if __name__=='__main__':
 app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')),debug=False)
