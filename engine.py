"""Session-scoped reconciliation for PostgreSQL and local SQLite."""
import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from uuid import uuid4
from rules import (norm,norm_name,missing_fields,check_arithmetic,check_identity,compare_versions,
                   match_order,reconcile)


def now():
    return datetime.now(timezone.utc).isoformat()


class Ledger:
    def __init__(self,path):
        self.path=str(path)
        self.pg=self.path.startswith(('postgres://','postgresql://'))
        serial='BIGSERIAL PRIMARY KEY' if self.pg else 'INTEGER PRIMARY KEY'
        with self.transaction() as db:
            for stmt in (
                'CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,seed TEXT NOT NULL)',
                f'CREATE TABLE IF NOT EXISTS runs(id {serial}, session TEXT NOT NULL,digest TEXT NOT NULL,vendor TEXT,number TEXT,po TEXT,payload TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,detail TEXT NOT NULL,parent BIGINT,rejection TEXT)',
                "CREATE UNIQUE INDEX IF NOT EXISTS approved_number ON runs(session,vendor,number) WHERE status='Approved'",
                'CREATE TABLE IF NOT EXISTS ledger(run BIGINT NOT NULL, session TEXT NOT NULL,po TEXT NOT NULL,line INTEGER NOT NULL,qty BIGINT NOT NULL,amount BIGINT NOT NULL,PRIMARY KEY(run,line))'):
                db.execute(stmt)
            columns={c[0] for c in db.execute('SELECT * FROM runs LIMIT 0').description}
            for col in ('created_at','rejected_at','rejected_by','review'):
                if col not in columns:
                    db.execute(f'ALTER TABLE runs ADD COLUMN {col} TEXT')

    def connect(self):
        if self.pg:
            import psycopg
            from psycopg.rows import dict_row
            # autocommit=True: our own BEGIN starts the transaction (otherwise psycopg opens one first and
            # Postgres logs "already a transaction in progress"). prepare_threshold=None: transaction-mode
            # poolers such as Supabase's port 6543 do not support prepared statements.
            return psycopg.connect(self.path,row_factory=dict_row,autocommit=True,prepare_threshold=None,
                                   connect_timeout=15)
        db=sqlite3.connect(self.path,timeout=30)
        db.row_factory=sqlite3.Row
        return db

    def query(self,db,sql,args=()):
        return db.execute(sql.replace('?','%s') if self.pg else sql,args)

    @contextmanager
    def transaction(self,sid=None):
        with closing(self.connect()) as db:
            try:
                db.execute('BEGIN ISOLATION LEVEL READ COMMITTED' if self.pg else 'BEGIN IMMEDIATE')
                if sid and self.pg:
                    self.query(db,'SELECT pg_advisory_xact_lock(hashtextextended(?,0))',(sid,))
                yield db
                db.execute('COMMIT') if self.pg else db.commit()
            except BaseException:
                db.execute('ROLLBACK') if self.pg else db.rollback()
                raise

    def start(self,seed):
        sid=uuid4().hex
        with self.transaction() as db:
            self.query(db,'INSERT INTO sessions VALUES(?,?)',(sid,json.dumps(seed)))
        return sid

    def session_exists(self,sid):
        if not sid:
            return False
        with closing(self.connect()) as db:
            return self.query(db,'SELECT 1 FROM sessions WHERE id=?',(sid,)).fetchone() is not None

    def balances(self,sid):
        """{po: {line_no: {qty, paise}}} left on every PO in this session's seed."""
        with closing(self.connect()) as db:
            row=self.query(db,'SELECT seed FROM sessions WHERE id=?',(sid,)).fetchone()
            seed=json.loads(row['seed'])
            return {o['po']:self.balance(db,sid,o) for o in seed['purchase_orders']}

    def remaining(self,db,sid,po,line):
        row=self.query(db,'SELECT COALESCE(SUM(qty),0) AS used, COALESCE(SUM(amount),0) AS cost FROM ledger WHERE session=? AND po=? AND line=?',(sid,po,line['line_no'])).fetchone()
        return line['qty']-int(row['used']),line['qty']*line['unit_price_paise']-int(row['cost'])

    def balance(self,db,sid,order):
        if not order:
            return {}
        result={}
        for line in order['lines']:
            qty,paise=self.remaining(db,sid,order['po'],line)
            result[str(line['line_no'])]=dict(qty=qty,paise=paise)
        return result

    def reject(self,sid,run,note,actor='local reviewer'):
        if not note.strip() or not actor.strip():
            raise ValueError('Enter reviewer and reason.')
        with self.transaction(sid) as db:
            cur=self.query(db,"UPDATE runs SET status='Rejected',rejection=?,rejected_at=?,rejected_by=? WHERE session=? AND id=? AND status='Held'",(note,now(),actor,sid,run))
            if cur.rowcount!=1:
                raise ValueError('Select an active hold in this session.')

    def history(self,sid):
        with closing(self.connect()) as db:
            records=self.query(db,'SELECT * FROM runs WHERE session=? ORDER BY id',(sid,)).fetchall()
            return [dict(dict(r),review=json.loads(r['review'] or '{}')) for r in records]

    def process(self,sid,doc,pdf,*,processing_error=None,on_stage=None):
        """Pass processing_error for provider/parse failures. Verified unreadability is a hold."""
        started=now()
        stages=[]
        findings=[]
        def stage(name):
            event=dict(name=name,at=now())
            stages.append(event)
            if on_stage:
                on_stage(dict(event))
        def add(code,text):
            findings.append(dict(code=code,detail=text))
        stage('Intake')
        digest=hashlib.sha256(pdf).hexdigest()
        with self.transaction(sid) as db:
            session=self.query(db,'SELECT seed FROM sessions WHERE id=?',(sid,)).fetchone()
            if not session:
                raise ValueError('Unknown session')
            seed=json.loads(session['seed'])
            doc=doc if isinstance(doc,dict) else {}
            # vendor key ignores punctuation and addresses, so a re-scan read as 'LLP.' still finds earlier uploads
            vendor,number=norm_name(doc.get('vendor_name')),norm(doc.get('invoice_no'))
            ref=doc.get('po_ref')
            order=None
            before={}
            movements=[]
            parent=None
            status='Approved'
            code='APPROVED'
            detail=''
            replacement=False
            stage('Check previous uploads')
            same=self.query(db,"SELECT * FROM runs WHERE session=? AND digest=? AND status IN ('Approved','Held','Rejected') ORDER BY id DESC LIMIT 1",(sid,digest)).fetchone()
            if same and same['status']!='Rejected':
                parent=same['id']
                ref=same['po']
                order=next((p for p in seed['purchase_orders'] if p['po']==ref),None)
                before=self.balance(db,sid,order)
                status,code,detail='Duplicate','DUPLICATE_SAME_FILE',f"Original run {parent}: {same['status']}."
            elif processing_error or not doc:
                status,code,detail='Failed','EXTRACTION_FAILED',str(processing_error or 'Extraction returned no structured document.')
            else:
                stage('Validate extracted fields')
                absent=missing_fields(doc)
                for field in absent:
                    add('FIELD_UNREADABLE',f'Cannot verify {field}.')
                if not absent:
                    check_arithmetic(doc,add)
                stage('Identify supplier and invoice')
                supplier=check_identity(seed,doc,add) if doc.get('vendor_name') and doc.get('buyer_name') and doc.get('supplier_state_code') else None
                if supplier and number:
                    audit=self.query(db,"SELECT * FROM runs WHERE session=? AND vendor=? AND status IN ('Approved','Held','Rejected') ORDER BY (status='Approved') DESC,(status='Held') DESC,id DESC",(sid,vendor)).fetchall()
                    comparable=lambda n: ''.join(c for c in n if c.isalnum())
                    previous=next((r for r in audit if r['number']==number),None)
                    if previous:
                        parent=previous['id']
                        replacement=previous['status']=='Rejected'
                        old=json.loads(previous['payload'])
                        if not replacement and not absent and not missing_fields(old):
                            changes=compare_versions(old,doc)
                            if changes:
                                add('CHANGED_VERSION',f"Compared with run {parent}: "+'; '.join(changes))
                            elif not findings:
                                status,code,detail='Duplicate','DUPLICATE_CONTENT',f'Same material fields as run {parent}.'
                        elif not replacement and not absent:
                            add('CHANGED_VERSION',f'Review new evidence against held run {parent}.')
                    else:
                        collision=next((r for r in audit if r['status']!='Rejected' and comparable(r['number'])==comparable(number)),None)
                        if collision:
                            parent=collision['id']
                            add('NUMBER_COLLISION',f'Confirm invoice number against run {parent}.')
                    stage('Match purchase order')
                    if isinstance(doc.get('lines'),list) and all(isinstance(l,dict) for l in doc['lines']) and (ref or doc['lines']):
                        order=match_order(seed,supplier,doc,add)
                    if order:
                        ref=order['po']
                        before=self.balance(db,sid,order)
                        if status!='Duplicate':
                            for old in audit:
                                prior=json.loads(old['payload'])
                                if (old['status']!='Rejected' and old['number']!=number and old['po']==ref and
                                    all(doc.get(k) is not None and doc[k]==prior.get(k) for k in ('invoice_date','total_paise'))):
                                    add('POSSIBLE_REISSUE',f"Same PO, date and total as {prior.get('invoice_no')} ({old['status']}).")
                                    break
                            stage('Reconcile PO lines and tax')
                            if not absent:
                                movements=reconcile(doc,order,before,seed['buyer'],add)
                if findings:
                    status,code,detail='Held',findings[0]['code'],findings[0]['detail']
                elif status=='Approved':
                    if not order or not movements:
                        raise ValueError('Approval requires a matched order and ledger movements')
                    code='APPROVED_REPLACEMENT' if replacement else 'APPROVED_PO_INFERRED' if not doc.get('po_ref') else 'APPROVED'
                    detail=(f'{ref} (inferred): no PO number is printed, and {ref} is the only open order that covers every line. '
                            'All matched lines approved.') if code=='APPROVED_PO_INFERRED' else f'{ref}: all matched lines approved.'
            stage('Record decision')
            run=self.query(db,'INSERT INTO runs(session,digest,vendor,number,po,payload,status,reason,detail,parent,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING id',
                           (sid,digest,vendor,number,ref,json.dumps(doc),status,code,detail,parent,started)).fetchone()['id']
            if status=='Approved':
                for key,qty,amount in movements:
                    self.query(db,'INSERT INTO ledger VALUES(?,?,?,?,?,?)',(run,sid,ref,key,qty,amount))
            question=QUESTIONS.get(code,'Can procurement confirm the flagged details or request a corrected invoice?') if status=='Held' else None
            review=dict(findings=findings,question=question,po=ref,before=before,after=self.balance(db,sid,order),stages=stages)
            self.query(db,'UPDATE runs SET review=? WHERE id=?',(json.dumps(review),run))
            return dict(id=run,status=status,reason=code,detail=detail,parent=parent,**review)


QUESTIONS={
 'PRICE_VARIANCE':'Can procurement provide an amended PO, or should the supplier correct the price?',
 'CHANGED_VERSION':'Which version did the supplier intend to bill?',
 'FIELD_UNREADABLE':'Can the supplier provide a readable copy of the indicated field?',
 'POSSIBLE_REISSUE':'Does the supplier confirm these are separate deliveries?',
 'PO_UNMATCHED':'Which open PO authorises this purchase?',
 'PO_AMBIGUOUS':'Which of the listed POs should this invoice use?',
 'QTY_EXCEEDS_REMAINING':'Can procurement confirm an amended quantity or request a corrected invoice?',
}
