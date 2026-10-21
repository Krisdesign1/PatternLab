"""Add reviewed daily nine-match blocks while preserving the original boundaries."""
import json
from pathlib import Path

def day_sequences(coupons):
    paths = sorted((Path(__file__).resolve().parent / 'data').glob('forebet_*_scenario_batch.json'))
    active = {c['id']: c for c in coupons}
    result = []
    seen = set()
    for path in paths:
        for item in json.loads(path.read_text())['imported']:
            coupon = active.get(item['coupon_id'])
            if coupon and coupon['id'] not in seen and coupon['note'] == item['note'] and coupon['sequence'] == item['sequence']:
                result.append(tuple(coupon['sequence']))
                seen.add(coupon['id'])
    return result
