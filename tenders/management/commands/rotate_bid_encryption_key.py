import hmac
import os

from cryptography.fernet import InvalidToken
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tenders import crypto
from tenders.models import BidVersion


class Command(BaseCommand):
    help = 'Re-encrypt stored bid versions with NEW_BID_ENCRYPTION_KEY.'

    def handle(self, *args, **options):
        old_key = settings.BID_ENCRYPTION_KEY
        new_key = os.environ.get('NEW_BID_ENCRYPTION_KEY')
        if not new_key:
            raise CommandError('Set NEW_BID_ENCRYPTION_KEY before running this command.')
        if hmac.compare_digest(old_key, new_key):
            raise CommandError('The new bid encryption key must differ from the current key.')

        with transaction.atomic():
            versions = list(BidVersion.objects.select_related('bid').all())
            try:
                plaintexts = [
                    crypto.decrypt(version.bid.tender_id, version.encrypted_payload)
                    for version in versions
                ]
            except InvalidToken as exc:
                raise CommandError(
                    'A bid could not be decrypted with the current key; no records were changed.'
                ) from exc

            settings.BID_ENCRYPTION_KEY = new_key
            try:
                for version, plaintext in zip(versions, plaintexts):
                    version.encrypted_payload = crypto.encrypt(version.bid.tender_id, plaintext)
                BidVersion.objects.bulk_update(versions, ['encrypted_payload'])
            finally:
                settings.BID_ENCRYPTION_KEY = old_key

        self.stdout.write(
            self.style.SUCCESS(
                f'Re-encrypted {len(versions)} bid versions. Set BID_ENCRYPTION_KEY to the new key before restarting the app.'
            )
        )