from pathlib import Path
import shutil

import pytest

from app.kyc_pipeline import _CONF, analyse, classify, gstin_valid, mask, repair_gstin, suggest_gstin
from app.main import GST

DOCUMENTS = Path(__file__).parents[1] / 'app' / 'data' / 'kyc_documents'
OCR_AVAILABLE = shutil.which('tesseract') is not None


def test_all_supplied_mock_gstins_have_valid_checksums():
    assert len(GST) == 3534
    assert all(gstin_valid(value) for value in GST)


def test_ocr_confusion_is_repaired_only_when_registry_identifies_one_valid_record():
    original = next(iter(GST))
    raw = None
    for wrong, possible in _CONF.items():
        for index, character in enumerate(original):
            if wrong != character and character in possible:
                raw = original[:index] + wrong + original[index + 1:]
                break
        if raw:
            break
    assert raw
    assert repair_gstin(raw, {original: GST[original]}) == (original, 'REPAIRED_REGISTRY_MATCH')
    assert repair_gstin(raw, {}) == (None, 'REPAIR_UNCONFIRMED')


def test_i_j_ocr_confusion_is_registry_anchored():
    original = next(value for value in GST if 'I' in value)
    index = original.index('I')
    raw = original[:index] + 'J' + original[index + 1:]
    assert repair_gstin(raw, {original: GST[original]}) == (original, 'REPAIRED_REGISTRY_MATCH')


def test_gstin_suggestion_is_trade_name_anchored_and_ambiguous_matches_are_rejected():
    original = next(iter(GST))
    record = GST[original]
    raw = ('0' if original[0] != '0' else '1') + original[1:]
    assert suggest_gstin(raw, record['trade_name'], {original: record}) == original
    assert suggest_gstin(raw, 'A different business', {original: record}) is None

    nearby = ('0' if original[1] != '0' else '1') + original[1:]
    duplicated = {original: record, nearby: record}
    assert suggest_gstin(original, record['trade_name'], duplicated) is None


def test_document_classifier_and_mask_helpers_are_explicit():
    assert classify('GST Registration Certificate') == 'gst_certificate'
    assert classify('Udyam Registration Certificate') == 'udyam_certificate'
    assert classify('Authorised Signatory') == 'signatory_id'
    assert classify('Electricity Bill') == 'address_proof'
    gstin = next(iter(GST))
    assert mask(gstin) == gstin[:2] + '*' * 9 + gstin[-4:]


@pytest.mark.skipif(not OCR_AVAILABLE, reason='Tesseract OCR executable is not installed on this machine')
@pytest.mark.parametrize(
    ('filename', 'expected_type'),
    [
        ('K03_gst_certificate.png', 'gst_certificate'),
        ('K02_gst_certificate.png', 'gst_certificate'),
        # The blur gate deliberately stops classification and field extraction.
        ('K04_gst_certificate.png', None),
        ('K07_gst_certificate.png', 'gst_certificate'),
        ('K08_gst_certificate.png', 'gst_certificate'),
        ('K01_gst_certificate.png', 'gst_certificate'),
        ('K01_udyam_certificate.png', 'udyam_certificate'),
        ('K11_address_proof.png', 'address_proof'),
    ],
)
def test_supplied_kyc_specimens_are_classified_and_review_flags_are_preserved(filename, expected_type):
    result = analyse((DOCUMENTS / filename).read_bytes(), GST)
    assert result['doc_type'] == expected_type
    if filename == 'K04_gst_certificate.png':
        assert result['doc_type'] is None
        assert result['gstin_candidates'] == []
        assert any('BLURRY' in flag for flag in result['flags'])
    elif filename == 'K02_gst_certificate.png':
        assert result['quality']['deskewed_deg'] != 0
        assert result['gstin_candidates'] and result['gstin_candidates'][0] in GST
    elif filename in {'K03_gst_certificate.png', 'K07_gst_certificate.png', 'K08_gst_certificate.png', 'K01_gst_certificate.png'}:
        assert result['gstin_candidates'] and result['gstin_candidates'][0] in GST
        if filename == 'K07_gst_certificate.png':
            assert 'Shri Physio Clinic' in result['fields']['legal_name']
            assert 'Neelgiri Physio Clinic' in result['fields']['trade_name']
    elif filename == 'K01_udyam_certificate.png':
        assert any('UDYAM_NUMBER_NOT_READABLE' in flag for flag in result['flags'])
    elif filename == 'K11_address_proof.png':
        assert any('ADDRESS_PROOF_STALE' in flag for flag in result['flags'])
