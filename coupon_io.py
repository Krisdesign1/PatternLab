"""CSV coupon parsing with visible per-line validation failures."""
import csv
import io
from analytics import normalize_value
from coupon_sizes import validate_size


def parse_coupons(file_bytes):
    sequences, rejected = [], []
    for line, row in enumerate(csv.reader(io.StringIO(file_bytes.decode('utf-8-sig', errors='replace'))), 1):
        clean = [c.strip() for c in row if c.strip()]
        if not clean:
            continue
        if len(clean) == 1:
            raw = clean[0]
            for sep in (';', '|', '>', '→'):
                raw = raw.replace(sep, ',')
            clean = [c.strip() for c in raw.split(',') if c.strip()]
        try:
            sequence = [normalize_value(c) for c in clean]
            validate_size(len(sequence))
            sequences.append(sequence)
        except ValueError as error:
            rejected.append({'Ligne': line, 'Motif': str(error)})
    return sequences, rejected
