"""Smart-KYC extraction helpers. OCR *extracts*; registry/checksum/cross-document rules *verify* (in main.verify).

Per upload: quality gate (blur) -> deskew -> Tesseract -> classify -> parse -> GSTIN repair.
A GSTIN is auto-corrected only when the corrected value is checksum-valid AND present in the registry.
A looser registry-anchored guess (same trade name, <=3 differing characters) is only ever a *suggestion*
the owner must confirm; it is never treated as read or verified.
"""
from __future__ import annotations
import itertools, os, re
from datetime import date

FIXTURE_TODAY = date.fromisoformat(os.getenv("ONEJOURNEY_FIXTURE_DATE", "2026-07-15"))          # README: use 2026-07-15 as "today" for date checks
ADDRESS_PROOF_MAX_AGE_DAYS = 90
BLUR_MIN = 60.0                            # Laplacian variance; specimens: blurred=1.0, all others >=937
GSTIN_RE = re.compile(r'\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b')   # 15 chars (the old pattern needed 16)
UDYAM_RE = re.compile(r'UDYAM\s*-?\s*[A-Z]{2}\s*-?\s*\d{2}\s*-?\s*\d{7}')

_CH = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
_CONF = {'O': '0', '0': 'O', 'I': '1J', 'J': '1I', '1': 'IJ', 'T': '1I', 'B': '8', '8': 'B', 'S': '5', '5': 'S',
         'Z': '2', '2': 'Z', 'G': '6', '6': 'G', 'D': '0', 'Q': '0'}


def _checksum(g14):
    tot = 0
    for i, c in enumerate(g14):
        v = _CH.index(c) * (1 if i % 2 == 0 else 2)
        tot += v // 36 + v % 36
    return _CH[(36 - tot % 36) % 36]


def gstin_valid(g):
    return bool(g) and len(g) == 15 and all(c in _CH for c in g) and _checksum(g[:14]) == g[14]


def repair_gstin(raw, registry):
    raw = (raw or '').upper().replace(' ', '')
    if gstin_valid(raw): return raw, 'AS_READ'
    pos = [i for i, c in enumerate(raw) if c in _CONF]
    cands = set()
    for n in (1, 2):
        for combo in itertools.combinations(pos, n):
            for rep in itertools.product(*[_CONF[raw[i]] for i in combo]):
                s = list(raw)
                for i, c in zip(combo, rep): s[i] = c
                s = ''.join(s)
                if gstin_valid(s): cands.add(s)
    hit = [c for c in cands if c in registry]
    if len(hit) == 1: return hit[0], 'REPAIRED_REGISTRY_MATCH'
    return None, 'REPAIR_UNCONFIRMED' if cands else 'UNREADABLE'


def _norm(s): return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def suggest_gstin(raw, trade_name, registry, max_dist=3):
    raw = (raw or '').upper().replace(' ', '')
    if len(raw) != 15: return None
    hits = [g for g, r in registry.items() if _norm(r['trade_name']) == _norm(trade_name)
            and sum(a != b for a, b in zip(g, raw)) <= max_dist]
    return hits[0] if len(hits) == 1 else None


def mask(g): return None if not g else g[:2] + '*' * 9 + g[-4:]


def _ocr_bytes(raw: bytes):
    import cv2, numpy as np, pytesseract
    if os.getenv('ONEJOURNEY_TESSERACT_CMD'):
        pytesseract.pytesseract.tesseract_cmd=os.environ['ONEJOURNEY_TESSERACT_CMD']
    g = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    if g is None: raise ValueError('undecodable image')
    blur = float(cv2.Laplacian(g, cv2.CV_64F).var())
    small = cv2.resize(g, None, fx=0.4, fy=0.4)
    bw = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    best, ang = -1.0, 0.0
    for a in np.arange(-15, 15.1, 0.5):
        M = cv2.getRotationMatrix2D((bw.shape[1] / 2, bw.shape[0] / 2), a, 1)
        s = float(np.var(cv2.warpAffine(bw, M, (bw.shape[1], bw.shape[0])).sum(axis=1)))
        if s > best: best, ang = s, float(a)
    if abs(ang) >= 2:                       # only rotate when the skew is real; leave clean scans untouched
        M = cv2.getRotationMatrix2D((g.shape[1] / 2, g.shape[0] / 2), ang, 1)
        g = cv2.warpAffine(g, M, (g.shape[1], g.shape[0]), borderValue=255, flags=cv2.INTER_CUBIC)
    else: ang = 0.0
    return pytesseract.image_to_string(g, config='--psm 6'), {'blur': round(blur, 1), 'deskewed_deg': ang}


def classify(t):
    t = t.lower()
    for key, name in (('gst registration', 'gst_certificate'), ('udyam registration', 'udyam_certificate'),
                      ('authorised signatory', 'signatory_id'), ('electricity bill', 'address_proof')):
        if key in t: return name
    return None


def _g(t, label):
    m = re.search(label + r'\s*[:\-]?\s*(.+)', t); return m.group(1).strip() if m else None


def analyse(raw: bytes, registry: dict, today: date = FIXTURE_TODAY) -> dict:
    """Returns text, doc_type, quality, fields, gstin_candidates, udyam_candidates, flags (customer-readable reason codes)."""
    text, q = _ocr_bytes(raw)
    out = {'text': text, 'quality': q, 'doc_type': None, 'fields': {}, 'gstin_candidates': [], 'gstin_suggestion': None,
           'udyam_candidates': [], 'flags': []}
    if q['blur'] < BLUR_MIN:
        out['flags'].append('IMAGE_TOO_BLURRY: please re-upload a sharper photo or type the identifier yourself')
        return out
    t = re.sub(r'\n\s*\n', '\n', text); out['doc_type'] = dt = classify(t)
    up = t.upper()
    out['udyam_candidates'] = [re.sub(r'\s', '', x) for x in UDYAM_RE.findall(up)]
    out['gstin_candidates'] = [c for c in GSTIN_RE.findall(up) if gstin_valid(c)]   # shape alone is not a read: must pass the checksum
    f = out['fields']
    if dt == 'gst_certificate':
        m = re.search(r'\bGSTIN[\s:\-]+([0-9A-Z ]{15,17})', up)
        f['legal_name'] = _g(t, 'Legal name'); f['trade_name'] = _g(t, 'Trade name')
        f['gstin_raw'] = re.sub(r'\s', '', m.group(1)) if m else None
        f['gstin_checksum_valid_as_read'] = gstin_valid(f['gstin_raw']) if f['gstin_raw'] else False
        out['gstin_candidates'] = [f['gstin_raw']] if f['gstin_checksum_valid_as_read'] else []
        if f['gstin_raw'] and not out['gstin_candidates']:
            fixed, how = repair_gstin(f['gstin_raw'], registry); f['gstin_read_quality'] = how
            if fixed: out['gstin_candidates'] = [fixed]
            else:
                sug = suggest_gstin(f['gstin_raw'], f['trade_name'], registry)
                if sug: out['gstin_suggestion'] = sug; out['flags'].append(f'CONFIRM_GSTIN_SUGGESTION: did you mean {mask(sug)}? human review required')
                else: out['flags'].append('GSTIN_NOT_READABLE: human review required')
    elif dt == 'udyam_certificate':
        f['enterprise_name'] = _g(t, 'Enterprise name')
        if not out['udyam_candidates']: out['flags'].append('UDYAM_NUMBER_NOT_READABLE: the number is missing or unreadable; please enter it')
    elif dt == 'address_proof':
        m = re.search(r'Bill date\s+(\d{4}-\d{2}-\d{2})', t); f['billed_to'] = _g(t, 'Billed to'); f['bill_date'] = m.group(1) if m else None
        if f['bill_date']:
            try: age = (today - date.fromisoformat(f['bill_date'])).days
            except ValueError: age = -1
            f['age_days'] = age
            if age < 0: out['flags'].append('ADDRESS_PROOF_DATE_INVALID')
            if age > ADDRESS_PROOF_MAX_AGE_DAYS: out['flags'].append(f'ADDRESS_PROOF_STALE: bill is {age} days old (limit {ADDRESS_PROOF_MAX_AGE_DAYS})')
        else: out['flags'].append('ADDRESS_PROOF_DATE_UNREADABLE')
    elif dt == 'signatory_id':
        n = re.search(r'Name\s*\n\s*(.+)', t); b = re.search(r'For business\s*\n\s*(.+)', t); v = re.search(r'Valid till\s*\n?\s*(\d{4}-\d{2}-\d{2})', t)
        f.update(name=n.group(1).strip() if n else None, business=b.group(1).strip() if b else None, valid_till=v.group(1) if v else None)
        try: valid = date.fromisoformat(f['valid_till'] or '')
        except ValueError: valid = None
        if not valid or valid < today: out['flags'].append('SIGNATORY_ID_EXPIRED_OR_UNREADABLE')
    return out
