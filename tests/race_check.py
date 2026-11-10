import json, sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
sys.path.insert(0, "tests"); sys.path.insert(0, ".")
from engine import Ledger
from verify import seed, root
L = Ledger("postgresql://postgres@/ps1test?host=/tmp&port=5433")
gt = lambda k: json.loads((root/'ground_truth'/f'{k}.json').read_text())
pdf = lambda k: (root/'invoices'/f'{k}.pdf').read_bytes()
over = 0
for trial in range(20):
    sid = L.start(seed); L.process(sid, gt('R1'), pdf('R1'))
    gate = Barrier(2)
    def go(k):
        gate.wait(); return L.process(sid, gt(k), pdf(k))['status']
    with ThreadPoolExecutor(2) as ex:
        res = list(ex.map(go, ['B12a', 'B12b']))
    with L.transaction() as db:
        left = L.remaining(db, sid, 'PO-104', seed['purchase_orders'][0]['lines'][0])[0]
    over += left < 0
print(f"{sys.argv[1]}: over-deducted in {over} of 20 trials")
