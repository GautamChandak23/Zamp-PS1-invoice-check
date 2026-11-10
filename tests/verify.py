import csv
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from engine import Ledger

root=Path(__file__).resolve().parent.parent/'data'/'test_pack'
seed=json.loads((root/'seed.json').read_text())
def rows(name):
    with (root/name).open() as f:
        return list(csv.DictReader(f))
def verify(path):
    ledger=Ledger(path)
    balances=rows('expected_balances.csv')
    def run(key):
        doc=json.loads((root/'ground_truth'/f'{key}.json').read_text())
        return ledger.process(sid,doc,(root/'invoices'/f'{key}.pdf').read_bytes())
    for row in rows('expected_results.csv'):
        case=row['case']
        if case not in ('R2','R3','R4'):
            sid=ledger.start(seed)
        if row['setup'].startswith('After R1') and case!='R2':
            run('R1')
        if case=='C2':
            ledger.reject(sid,run('R2')['id'],'Request corrected unit price')
        if case in ('B8','B12'):
            gate=Barrier(2)
            def submit(key):
                gate.wait()
                return run(key)
            with ThreadPoolExecutor(2) as pool:
                result=list(pool.map(submit,['B8','B8'] if case=='B8' else ['B12a','B12b']))
        else:
            result=[run(case)]
        codes=sorted(r['reason'] for r in result)
        assert codes==sorted(row['reason_code'].split('+')),(case,codes)
        assert sorted(r['status'] for r in result)==sorted(row['expected_decision'].split(' + ')),case
        with closing(ledger.connect()) as db:
            for bal in (b for b in balances if b['case']==case):
                order=next(o for o in seed['purchase_orders'] if o['po']==bal['po'])
                line=next(l for l in order['lines'] if l['line_no']==int(bal['line_no']))
                assert ledger.remaining(db,sid,order['po'],line)==(int(bal['qty_left']),100*int(bal['pre_tax_left_inr'])),case
        print(case,'PASS',','.join(codes))
    print(f"{len(balances)} line-balance assertions passed.")
if __name__=='__main__':
    with TemporaryDirectory() as tmp:
        verify(Path(tmp)/'acceptance.db')
