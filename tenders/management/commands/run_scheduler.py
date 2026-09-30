"""Closes bidding at the deadline (OPN-01) and sends deadline / SLA reminders.
Run every minute:  python manage.py run_scheduler        (cron)   or   --loop  (foreground)"""
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from tenders import services as svc
from tenders.models import Bid, Config, Notification, StageForward, Tender


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true')

    def handle(self, *a, **o):
        while True:
            self.run_once()
            if not o['loop']:
                break
            time.sleep(60)

    def run_once(self):
        now = timezone.now()
        for t in Tender.objects.filter(status__in=['PUBLISHED', 'BIDDING_OPEN']):
            before = t.status
            t.sync_status()
            if before != t.status and t.status == 'BIDDING_CLOSED':
                svc.audit(None, 'BIDDING_CLOSED', t)
                svc.notify(svc.users_with_role('PO'), 'DEADLINE', f'Bidding closed for {t.tender_no}. You can open the tender.', f'/tenders/{t.pk}/')
            for hours, label in ((24, '24h'), (1, '1h')):
                if timedelta(0) < t.bid_end_at - now <= timedelta(hours=hours):
                    for v in svc.eligible_vendors(t):
                        kind = f'REMIND_{label}_{t.pk}'
                        has_bid = Bid.objects.filter(tender=t, vendor=v).exclude(status='WITHDRAWN').exists()
                        if not has_bid and not Notification.objects.filter(user=v.user, kind=kind).exists():
                            svc.notify([v.user], kind, f'Deadline approaching ({label}): {t.tender_no}', f'/tenders/{t.pk}/',
                                       email_subject=f'Reminder: {t.tender_no} closes soon', tender=t)
        # SLA reminders for pending approvals
        sla = timedelta(days=int(Config.get('sla_days')))
        stage_role = {'TECHNICAL': 'TC', 'FINANCE': 'FC', 'CFO': 'CFO'}
        for t in Tender.objects.filter(status__in=['TECHNICAL_REVIEW', 'FINANCE_REVIEW', 'CFO_REVIEW']):
            fw = t.forwards.last()
            if fw and now - (fw.reminded_at or fw.forwarded_at) > sla:
                svc.notify(svc.users_with_role(stage_role[fw.stage]), 'SLA', f'Reminder: {t.tender_no} pending your decision.',
                           f'/tenders/{t.pk}/', email_subject=f'Pending approval – {t.tender_no}', tender=t)
                fw.reminded_at = now
                fw.save(update_fields=['reminded_at'])
        self.stdout.write(f'scheduler tick {now:%H:%M:%S}')
