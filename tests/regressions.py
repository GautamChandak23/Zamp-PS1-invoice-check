"""Reviewer probes, independent of the labelled acceptance matrix."""
from copy import deepcopy
from datetime import datetime
from engine import Ledger
from verify import seed,root
import json


def check_review(path):
    ledger=Ledger(path)
    sid=ledger.start(seed)
    def load(key):
        return json.loads((root/'ground_truth'/f'{key}.json').read_text())
    def run(key,token=None,**changes):
        doc=load(key)
        doc.update(changes)
        return ledger.process(sid,doc,(token or key).encode())
    def codes(result):
        return [f['code'] for f in result['findings']]

    events=[]
    failed=ledger.process(sid,None,b'R1',processing_error='Provider timeout',on_stage=events.append)
    assert failed['status']=='Failed' and events==failed['stages']
    approved=run('R1',buyer_name='Bill to: Meridian Threads Pvt Ltd, Indore, Madhya Pradesh')
    assert approved['status']=='Approved' and approved['after']['1']['qty']==200
    rescan=load('R1')
    rescan['lines'][0]['description']='Cotton crew T-shirt 180GSM'
    assert ledger.process(sid,rescan,b'rescan')['reason']=='DUPLICATE_CONTENT'
    changed=run('R4')
    assert '300 → 250' in changed['detail'] and 'INR 200.00 → INR 240.00' in changed['detail']
    assert 'PRICE_VARIANCE' in codes(changed)
    b10=run('B10')
    assert 'POSSIBLE_REISSUE' in codes(b10) and 'QTY_EXCEEDS_REMAINING' in codes(b10)
    assert b10['before']==b10['after'] and b10['question']
    held=run('R2')
    reissue=run('R2','reissue',invoice_no='INV-120')
    assert reissue['reason']=='POSSIBLE_REISSUE' and 'PRICE_VARIANCE' in codes(reissue)
    ledger.reject(sid,held['id'],'Please correct the price.','Gautam')
    reopened=run('R2')
    assert reopened['status']=='Held' and reopened['parent']==held['id']
    audit=next(r for r in ledger.history(sid) if r['id']==held['id'])
    assert datetime.fromisoformat(audit['rejected_at'])>=datetime.fromisoformat(audit['created_at'])
    assert audit['rejected_by']=='Gautam'
    sid=ledger.start(seed)
    wrong=run('R1','wrongpo',po_ref='PO-110')
    assert wrong['reason']=='PO_UNMATCHED' and 'PO-110' in wrong['detail'] and 'Tapivastra' in wrong['detail']
    assert run('B2')['detail']=='Cannot verify lines 1 unit price.'
    assert run('R1','otherbuyer',buyer_name='Meridian Threads Pvt Ltd Holdings')['reason']=='IDENTITY_MISMATCH'
    sid=ledger.start(seed)
    discount=load('R1')
    discount['lines'][0].update(unit_price_paise=19000,amount_paise=5700000)
    discount.update(subtotal_paise=5700000,total_paise=5985000)
    discount['taxes'][0]['amount_paise']=285000
    assert 'INR 3,000.00 below' in ledger.process(sid,discount,b'discount')['detail']
    mixed=deepcopy(seed)
    mixed['purchase_orders'][-1]['lines'][1]['gst_rate_pct']=12
    sid=ledger.start(mixed)
    doc=load('B11')
    doc['taxes']=[dict(type='IGST',rate_pct='5',amount_paise=146250),
                  dict(type='IGST',rate_pct='12',amount_paise=384000)]
    doc['total_paise']=doc['subtotal_paise']+530250+doc['round_off_paise']
    assert ledger.process(sid,doc,b'mixed')['status']=='Approved'
    sid=ledger.start(mixed)
    doc['taxes'][0]['amount_paise']+=500
    doc['taxes'][1]['amount_paise']-=500
    assert ledger.process(sid,doc,b'wrong-split')['reason']=='TAX_VARIANCE'
    print('Additional reviewer regressions passed.')
