"""Canonical customer need vocabulary shared by the form, advisor, and recommender."""
from __future__ import annotations

from typing import Any
from .languages import NEEDS as REGIONAL_NEEDS

NEEDS: dict[str, dict[str, Any]] = {
    "internet": {
        "label": "Business internet",
        "skus": ["P04", "P01", "P02", "P03", "P05", "P06"],
        "aliases": ["internet", "broadband", "broad band", "wifi", "wi-fi", "fibre", "fiber", "इंटरनेट", "ब्रॉडबैंड"],
    },
    "dedicated_line": {
        "label": "Dedicated internet line",
        "skus": ["P05", "P06"],
        "aliases": ["leased line", "dedicated line", "dedicated bandwidth", "लीज्ड लाइन", "लीज लाइन", "डेडिकेटेड"],
    },
    "backup": {
        "label": "Backup connection",
        "skus": ["P09"],
        "aliases": ["backup", "failover", "secondary connection", "बैकअप", "बॅकअप"],
    },
    "static_ip": {
        "label": "Static IP",
        "skus": ["P11"],
        "aliases": ["static ip", "fixed ip", "स्टैटिक आईपी"],
    },
    "central_management": {
        "label": "Manage multiple branches",
        "skus": ["P10"],
        "aliases": ["sd-wan", "sdwan", "central management", "manage all branches", "all branches together", "सभी शाखाओं"],
    },
    "email": {
        "label": "Business email and office tools",
        "skus": ["P16"],
        "aliases": ["email", "e-mail", "cloud email", "ईमेल", "ई-मेल"],
    },
    "cloud_backup": {
        "label": "Cloud file backup",
        "skus": ["P17"],
        "aliases": ["cloud backup", "data backup", "file backup", "क्लाउड बैकअप", "डेटा बैकअप"],
    },
    "security": {
        "label": "Device security",
        "skus": ["P20"],
        "aliases": ["security", "antivirus", "endpoint security", "cybersecurity", "सुरक्षा", "सुरक्षितता"],
    },
    "firewall": {
        "label": "Managed firewall",
        "skus": ["P21"],
        "aliases": ["firewall", "फ़ायरवॉल", "फायरवॉल"],
    },
    "secure_wifi": {
        "label": "Secure guest Wi-Fi",
        "skus": ["P22"],
        "aliases": ["guest wifi", "guest wi-fi", "guest network", "secure wifi", "secure wi-fi", "गेस्ट वाईफाई"],
    },
    "calling": {
        "label": "Business calling",
        "skus": ["P12"],
        "aliases": ["calling", "voice", "landline", "telephony", "कॉलिंग", "कॉल", "फोन"],
    },
    "toll_free": {
        "label": "Toll-free number",
        "skus": ["P13"],
        "aliases": ["toll-free", "toll free", "टोल फ्री", "टोल-फ्री"],
    },
    "sms": {
        "label": "Business text messaging",
        "skus": ["P14"],
        "aliases": ["bulk sms", "business sms", "sms campaign", "sms", "एसएमएस"],
    },
    "whatsapp": {
        "label": "WhatsApp Business",
        "skus": ["P15"],
        "aliases": ["whatsapp", "व्हाट्सएप", "व्हॉट्सअॅप", "वाट्सएप"],
    },
    "mobile": {
        "label": "Business mobile lines",
        "skus": ["P07", "P08"],
        "aliases": ["mobile sim", "mobile service", "mobile line", "business mobile line", "sim card", "postpaid", "मोबाइल सिम", "सिम कार्ड", "सिम"],
    },
    "website": {
        "label": "Website and domain",
        "skus": ["P18"],
        "aliases": ["website", "web presence", "online store", "domain", "वेबसाइट"],
    },
    "pos": {
        "label": "Point-of-sale and billing",
        "skus": ["P19"],
        "aliases": ["pos", "billing", "point of sale", "point-of-sale", "पॉस", "बिलिंग"],
    },
    "cctv": {
        "label": "CCTV and cameras",
        "skus": ["P23"],
        "aliases": ["cctv", "surveillance camera", "security camera", "सीसीटीवी", "कैमरा"],
    },
    "iot": {
        "label": "Machine monitoring",
        "skus": ["P24"],
        "aliases": ["machine monitoring", "iot", "monitor machines", "मशीन मॉनिटरिंग"],
    },
    "fleet": {
        "label": "Fleet and vehicle tracking",
        "skus": ["P25"],
        "aliases": ["fleet", "vehicle tracking", "gps tracker", "vehicles", "गाड़ियों"],
    },
}

ALLOWED_NEEDS = list(NEEDS)
NEED_SKUS = {key: list(value["skus"]) for key, value in NEEDS.items()}
NEED_ALIASES = {key: list(value["aliases"]) for key, value in NEEDS.items()}
for key, words in REGIONAL_NEEDS.items():
    NEED_ALIASES[key] = [*NEED_ALIASES[key], *words]

NEED_LABELS = {key: str(value["label"]) for key, value in NEEDS.items()}

# These are question prompts only. They never add a product or need to a quote.
SEGMENT_HINTS = {
    "clinic": "Typical priorities may include reliable internet and backup, cloud email, device security, and business calling.",
    "retail": "Ask whether the business needs reliable internet for UPI or POS, mobile lines, WhatsApp Business, or billing. Respect the stated budget.",
    "restaurant": "For several branches, ask about site count, branch network management, CCTV, and backup before suggesting anything.",
    "manufacturing": "Ask whether dedicated bandwidth, a firewall, machine monitoring, or vehicle tracking would solve a stated operational need.",
}
