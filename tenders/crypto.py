import base64
import hashlib
from cryptography.fernet import Fernet
from django.conf import settings


def _fernet(tender_id):
    raw = hashlib.sha256(f'{settings.BID_ENCRYPTION_KEY}:{tender_id}'.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt(tender_id, text):
    return _fernet(tender_id).encrypt(text.encode()).decode()


def decrypt(tender_id, token):
    return _fernet(tender_id).decrypt(token.encode()).decode()
