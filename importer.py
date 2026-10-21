import re, json, hashlib, os
from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
TYPES=['single','matching','sequence','fill','short','essay']
HEADINGS=[('на выбор правильного ответа','single'),('на установление соответствия','matching'),('на установление правильной последовательности','sequence'),('на дополнение','fill'),('с кратким свободным ответом','short'),('с развернутым ответом','essay'),('с развёрнутым ответом','essay')]
CHOICE=re.compile(r'^\s*([А-ЯA-ZЁ])\s*[).]\s*(.+)$')
ANS=re.compile(r'^\s*(?:Правильн(?:ый|ые)\s+ответ(?:ы)?|Ответ)\s*[:：]?\s*(.*)$',re.I)
NUM=re.compile(r'^\s*(\d{1,3})\s*[.)]\s*(.*)$')

def parts(path):
 d=Document(path)
 for child in d.element.body.iterchildren():
  if child.tag==qn('w:p'):
   p=Paragraph(child,d); s=p.text.strip()
   if s: yield ('p',s)
  elif child.tag==qn('w:tbl'):
   t=Table(child,d); lines=[]
   for row in t.rows:
    cells=[c.text.replace('\n',' / ').strip() for c in row.cells]
    if any(cells):lines.append(' | '.join(cells))
   if lines:yield ('table','\n'.join(lines))

def clean_answer(s):
 return re.sub(r'\s*Компетенции.*','',s,flags=re.I).strip(' .;')

def parse(path):
 items=list(parts(path)); title=os.path.basename(path).rsplit('.',1)[0]
 for _,s in items[:8]:
  m=re.search(r'«([^»]+)»',s)
  if m:title=m.group(1).strip();break
 mode=None; stage=None; buf=[]; records=[]
 def flush():
  nonlocal buf
  if not buf or not mode:buf=[];return
  content=[]; answer=''; found=False
  for kind,raw in buf:
   if kind=='table':
    # Preserve table, including answers which may be in table form.
    content.append(raw);continue
   for line in raw.split('\n'):
    line=line.strip()
    a=ANS.match(line)
    if a:answer=clean_answer(a.group(1));found=True;continue
    if re.match(r'Компетенции\s*\(',line,re.I):continue
    if line:content.append(line)
  if not found:buf=[];return
  # Answers for matching tasks often occupy a separate table after the 'Правильный ответ:' line.
  if mode=='single':
   opts=[]; question=[]
   for line in content:
    m=CHOICE.match(line)
    if m:opts.append({'id':m.group(1),'text':m.group(2)})
    elif not re.match(r'^\d+\s*[.)]\s*Выберите',line,re.I):question.append(line)
   if len(opts)<2:buf=[];return
   question='\n'.join(question).strip()
   if not question:buf=[];return
   records.append(dict(subject=title,type=mode,question=question,options=opts,answer=answer,source=os.path.basename(path)))
  else:
   question='\n'.join(content).strip()
   if question and (answer or mode=='essay'):
    records.append(dict(subject=title,type=mode,question=question,options=[],answer=answer,source=os.path.basename(path)))
  buf=[]
 for idx,(kind,s) in enumerate(items):
  low=s.lower().replace('ё','е')
  newmode=None
  for key,val in HEADINGS:
   if key.replace('ё','е') in low and ('задани' in low or 'тип' in low):newmode=val;break
  if newmode:
   flush();mode=newmode;stage=None;continue
  if 'задания открытого типа'==low or 'задания закрытого типа'==low:
   flush();mode=None;continue
  if mode is None:continue
  if kind=='p' and NUM.match(s) and not ANS.match(s) and (not buf or any(ANS.match(x) for k,t in buf if k=='p' for x in t.split('\n'))):
   flush()
  # After answer, competence marks the end of question, but table answers may follow answer line.
  if kind=='p' and re.match(r'^Компетенции\s*\(',s,re.I):
   flush();continue
  buf.append((kind,s))
 flush()
 # Resolve answer keys from table when text answer is empty
 for r in records:
  if r['type']=='single':
   a=r['answer'].strip().upper().replace('A','А').replace('B','В').replace('C','С').replace('D','Д').replace('E','Е')
   # Latin B is ambiguous: often means Cyrillic В, but could mean Б. Flag for review.
   if a in [o['id'] for o in r['options']]:r['answer']=a
 return records
