"""Deterministic rules for the assessment's verified extraction contract."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import re


def norm(value):
    return ' '.join(str(value or '').casefold().split())


_LABEL = re.compile(r'^\s*(bill(ed)?\s+to|buyer|sold\s+to|supplier|vendor|seller|from)\s*[:\-]\s*', re.I)
_SUFFIXES = ((r'\bprivate\b', 'pvt'), (r'\blimited\b', 'ltd'))


def norm_name(value):
    """Legal name only: drop a leading label, anything after the first comma, punctuation and suffix spellings.

    'Bill to: Meridian Threads Pvt. Ltd., Indore' -> 'meridian threads pvt ltd'
    'Meridian Threads Private Limited'            -> 'meridian threads pvt ltd'
    'Meridian Threads Pvt Ltd Holdings'           -> 'meridian threads pvt ltd holdings' (a different entity)
    """
    text = _LABEL.sub('', str(value or ''))
    text = text.split(',')[0].casefold()
    text = re.sub(r'[^\w\s&]', ' ', text)
    for pattern, short in _SUFFIXES:
        text = re.sub(pattern, short, text)
    return ' '.join(text.split())


def inr(paise):
    return f"INR {Decimal(paise) / 100:,.2f}"


def line_identity(line):
    return norm(line.get('item_code')) or re.sub(r'\s+', '', norm(line.get('description')))


def material(doc):
    fields = {key: doc.get(key) for key in ('invoice_date', 'po_ref', 'currency',
              'subtotal_paise', 'round_off_paise', 'total_paise', 'place_of_supply_state_code',
              'supplier_state_code')}
    fields['lines'] = sorted((line_identity(line), str(line.get('hsn') or ''),
                              line.get('qty'), line.get('unit_price_paise'), line.get('amount_paise'))
                             for line in doc['lines'])
    fields['taxes'] = sorted((t['type'], Decimal(str(t['rate_pct'])), t['amount_paise']) for t in doc['taxes'])
    return fields


def compare_versions(old, new):
    before, after = material(old), material(new)
    changes = []
    for key in before:
        if before[key] == after[key]:
            continue
        if key == 'lines':
            if len(before[key]) != len(after[key]):
                changes.append(f'Line count: {len(before[key])} → {len(after[key])}')
            for n, (a, b) in enumerate(zip(before[key], after[key]), 1):
                name = a[0].upper() if len(a[0]) <= 12 else f'Line {n}'  # an item code as printed, else the line number
                for i, label in enumerate(('item', 'HSN', 'quantity', 'unit price', 'line amount')):
                    if a[i] != b[i]:
                        x, y = (inr(a[i]), inr(b[i])) if i >= 3 else (a[i], b[i])
                        changes.append(f'{name} {label}: {x} → {y}')
        elif key.endswith('_paise'):
            label = key.removesuffix('_paise').replace('_', ' ')
            changes.append(f'{label}: {inr(before[key])} → {inr(after[key])}')
        elif key == 'taxes':
            def rates(rows):
                return ', '.join(f'{kind} {rate}% {inr(amount)}' for kind, rate, amount in rows)
            changes.append(f'Tax: {rates(before[key])} → {rates(after[key])}')
        else:
            changes.append(f'{key}: {before[key]} → {after[key]}')
    return changes


def missing_fields(doc):
    errors = []
    for key in ('vendor_name','invoice_no','invoice_date','buyer_name','supplier_state_code',
                'place_of_supply_state_code','currency'):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            errors.append(key)
    for key in ('subtotal_paise','total_paise','round_off_paise'):
        if type(doc.get(key)) is not int:
            errors.append(key.removesuffix('_paise').replace('_', ' '))
    if doc.get('po_ref_unverified'):
        # a PO number the model read but the page check could not confirm: never treat it as "no PO given"
        errors.append('PO number')
    for group in ('lines','taxes'):
        if not isinstance(doc.get(group), list) or not doc[group]:
            errors.append(group)
            continue
        for index, entry in enumerate(doc[group], 1):
            if not isinstance(entry,dict):
                errors.append(f'{group} {index}')
                continue
            if group == 'lines' and not entry.get('item_code') and not entry.get('description'):
                # neither the item code nor the description was confirmed on this line: never guess the PO line
                errors.append(f'{group} {index} item')
            keys = ('qty','unit_price_paise','amount_paise') if group == 'lines' else ('amount_paise',)
            for key in keys:
                value = entry.get(key)
                if type(value) is not int or value < (1 if group=='lines' else 0):
                    errors.append(f'{group} {index} {key.replace("_paise","").replace("_"," ")}')
            if group == 'taxes':
                try:
                    rate = Decimal(str(entry.get('rate_pct')))
                    if not rate.is_finite() or rate < 0 or not entry.get('type'):
                        raise ValueError
                except (ValueError, InvalidOperation):
                    errors.append(f'tax {index} type or rate')
    return errors


def check_arithmetic(doc, add):
    for index,line in enumerate(doc['lines'],1):
        if line['qty'] * line['unit_price_paise'] != line['amount_paise']:
            add('TOTAL_MISMATCH',f'Invoice line {index} amount differs from quantity times price.')
    subtotal = sum(line['amount_paise'] for line in doc['lines'])
    total = subtotal + sum(t['amount_paise'] for t in doc['taxes']) + doc['round_off_paise']
    if subtotal != doc['subtotal_paise'] or total != doc['total_paise'] or abs(doc['round_off_paise']) > 100:
        add('TOTAL_MISMATCH',f"Computed subtotal {inr(subtotal)} and gross {inr(total)}; printed gross {inr(doc['total_paise'])}.")


def check_identity(seed,doc,add):
    vendor = next((v for v in seed['vendors'] if norm_name(v['name'])==norm_name(doc['vendor_name']) and v['approved']),None)
    if not vendor:
        add('VENDOR_NOT_APPROVED',f"{doc['vendor_name']} is not on the approved vendor list.")
        return None
    buyer = seed['buyer']
    if norm_name(doc['buyer_name']) != norm_name(buyer['name']):
        add('IDENTITY_MISMATCH',f"The invoice is billed to '{doc['buyer_name']}', not {buyer['name']}.")
    if vendor['state_code'] != doc['supplier_state_code']:
        add('IDENTITY_MISMATCH',f"The invoice gives supplier state code {doc['supplier_state_code']}; "
                                f"{vendor['name']} is registered in {vendor['state']} ({vendor['state_code']}).")
    return vendor


def match_lines(line, order):
    if line.get('item_code'):
        return [p for p in order['lines'] if norm(p['item'])==norm(line['item_code'])]
    return [p for p in order['lines'] if norm(p['description'])==norm(line.get('description'))]


def match_order(seed,vendor,doc,add):
    printed=sorted(set(doc.get('po_refs_printed') or []))
    if len(printed)>1:
        # one PO per invoice: never let the model's choice decide which order is charged
        add('PO_AMBIGUOUS',f"The invoice prints {len(printed)} PO numbers ({', '.join(printed)}). "
                           "One PO per invoice: procurement must say which order authorises this charge.")
        return None
    valid=[o for o in seed['purchase_orders'] if o['vendor_id']==vendor['id'] and o['status']=='open' and o['currency']==doc['currency']]
    ref=doc.get('po_ref')
    if ref:
        match=next((o for o in valid if o['po']==ref),None)
        if not match:
            add('PO_UNMATCHED',f"{ref} is not an open {doc['currency']} order for {vendor['name']}.")
        return match
    candidates=[o for o in valid if all(len(match_lines(line,o))==1 for line in doc['lines'])]
    if len(candidates)==1:
        return candidates[0]
    add('PO_AMBIGUOUS' if candidates else 'PO_UNMATCHED',
        'Possible orders: '+', '.join(o['po'] for o in candidates) if candidates else 'No open order matches this vendor, currency and items.')
    return None


def reconcile(doc,order,balance,buyer,add):
    quantities, amounts = Counter(),Counter()
    expected_tax=defaultdict(Decimal)
    types=order['expected_tax_type'].split('+')
    if ('IGST' in types) != (doc['supplier_state_code']!=doc['place_of_supply_state_code']) or doc['place_of_supply_state_code']!=buyer['state_code']:
        add('TAX_TYPE_MISMATCH','Place of supply or supplier state conflicts with the fixture purchase terms.')
    complete=True
    for line in doc['lines']:
        matches=match_lines(line,order)
        if len(matches)!=1:
            complete=False
            add('CHARGE_NOT_ON_PO',f"{line.get('description')} has no unique line on {order['po']}.")
            continue
        term=matches[0]
        key=term['line_no']
        quantities[key]+=line['qty']
        amounts[key]+=line['amount_paise']
        difference=(line['unit_price_paise']-term['unit_price_paise'])*line['qty']
        if difference:
            direction='above' if difference>0 else 'below'
            add('PRICE_VARIANCE',f"{line['qty']} units at {inr(line['unit_price_paise'])} versus {inr(term['unit_price_paise'])} agreed; {inr(abs(difference))} {direction} the authorised line value.")
        rate=Decimal(str(term['gst_rate_pct'])) / len(types)
        for component in types:
            expected_tax[component,rate]+=line['amount_paise']*rate/100
    for key,qty in quantities.items():
        left=balance[str(key)]
        if qty>left['qty']:
            add('QTY_EXCEEDS_REMAINING',f"Line {key} bills {qty} units; {left['qty']} remain.")
        if amounts[key]>left['paise']:
            add('AMOUNT_EXCEEDS_REMAINING',f"Line {key} bills {inr(amounts[key])}; {inr(left['paise'])} remains.")
    actual=Counter()
    for t in doc['taxes']:
        actual[t['type'],Decimal(str(t['rate_pct']))]+=t['amount_paise']
    if {key[0] for key in actual}!=set(types):
        add('TAX_TYPE_MISMATCH',f"Supplier state {doc['supplier_state_code']}, place of supply {doc['place_of_supply_state_code']}. Expected {' + '.join(types)} on {order['po']}. Invoice shows {' + '.join(sorted({k[0] for k in actual}))}.")
    elif complete and (set(actual)!=set(expected_tax) or any(abs(actual[k]-due)>100 for k,due in expected_tax.items())):
        def breakdown(groups):
            return ', '.join(f'{kind} {rate}% {inr(amount)}' for (kind,rate),amount in sorted(groups.items()))
        add('TAX_VARIANCE', f'Expected {breakdown(expected_tax)}. Invoice shows {breakdown(actual)}.')
    return [(k,qty,amounts[k]) for k,qty in quantities.items()]
