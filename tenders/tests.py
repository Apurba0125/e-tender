from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core import mail
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import User, Vendor
from . import services as svc
from .forms import TenderForm
from .models import Approval, AuditLog, Bid, BidVersion, Notice, Tender
from .services import WorkflowError


@override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend')
class EmailDeliveryTests(TestCase):
    def test_console_backend_is_not_reported_as_delivered(self):
        log = svc.deliver_email('vendor@example.com', 'Award', 'Award details')

        self.assertEqual(log.status, 'FAILED')
        self.assertIn('email was not delivered', log.error)


@override_settings(BID_ENCRYPTION_KEY='old-test-key')
class BidKeyRotationTests(TestCase):
    def test_rotation_reencrypts_bid_payloads(self):
        call_command('seed_demo', verbosity=0)
        purchase_officer = User.objects.get(username='po')
        vendor = Vendor.objects.get(user__username='vendor1')
        now = timezone.now()
        tender = Tender.objects.create(
            title='Key rotation test',
            category='IT',
            description='Test',
            estimated_value=100,
            bid_start_at=now,
            bid_end_at=now + timedelta(hours=1),
            opening_at=now + timedelta(hours=2),
            created_by=purchase_officer,
        )
        bid = Bid.objects.create(tender=tender, vendor=vendor)
        version = BidVersion.objects.create(bid=bid, version_no=1, encrypted_payload='')
        version.set_payload({'price': 123})
        version.save(update_fields=['encrypted_payload'])
        old_payload = version.encrypted_payload

        with patch.dict('os.environ', {'NEW_BID_ENCRYPTION_KEY': 'new-test-key'}):
            call_command('rotate_bid_encryption_key')

        version.refresh_from_db()
        self.assertNotEqual(version.encrypted_payload, old_payload)
        with override_settings(BID_ENCRYPTION_KEY='new-test-key'):
            self.assertEqual(version.get_payload(as_owner=True), {'price': 123})


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class FullFlow(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', verbosity=0)
        cls.po = User.objects.get(username='po')
        cls.tc1, cls.fc1, cls.cfo = (User.objects.get(username=n) for n in ('tc1', 'fc1', 'cfo'))
        cls.vendors = [Vendor.objects.get(user__username=f'vendor{i}') for i in (1, 2, 3, 4)]

    def page(self, user, *urls):
        from django.test import Client
        c = Client()
        c.login(username=user, password='Tender@12345')
        for u in urls:
            r = c.get(u)
            self.assertEqual(r.status_code, 200, f'{user} {u}')

    def make_tender(self, visibility='OPEN', invites=()):
        n = Notice.objects.create(title='N', description='d', created_by=self.po)
        svc.publish_notice(n, self.po)
        now = timezone.now()
        t = Tender.objects.create(notice=n, title='Laptops', category='IT', description='x', estimated_value=1000000,
                                  bid_start_at=now - timedelta(hours=1), bid_end_at=now + timedelta(hours=1),
                                  opening_at=now + timedelta(hours=2), created_by=self.po, visibility=visibility)
        for v in invites:
            t.invites.create(vendor=v)
        svc.publish_tender(t, self.po)
        return t

    def bid(self, t, v, price, days=10):
        return svc.submit_bid(t, v, dict(price=price, taxes=0, delivery_days=days, validity_days=90, remarks='ok'), None, None)

    def test_limited_tender_vendors_render_as_checkboxes(self):
        field_html = str(TenderForm()['invited'])

        self.assertIn('type="checkbox"', field_html)
        self.assertNotIn('<select', field_html)

    def close_deadline(self, t):
        Tender.objects.filter(pk=t.pk).update(bid_end_at=timezone.now() - timedelta(minutes=1))
        t.refresh_from_db()
        t.sync_status()

    def test_full_happy_path(self):
        t = self.make_tender()
        self.assertEqual(t.status, 'BIDDING_OPEN')
        bids = [self.bid(t, v, p) for v, p in zip(self.vendors, (900000, 800000, 850000, 950000))]
        # sealed: nobody can read bids pre-opening
        with self.assertRaises(PermissionDenied):
            bids[0].latest.get_payload()
        self.assertFalse(t.can_open)                          # T7
        with self.assertRaises(WorkflowError):
            svc.open_tender(t, self.po)
        # vendor revises -> version kept (T5)
        self.bid(t, self.vendors[0], 870000)
        self.assertEqual(bids[0].versions.count(), 2)
        self.close_deadline(t)
        with self.assertRaises(WorkflowError):                # T4 late bid
            self.bid(t, self.vendors[0], 1)
        svc.open_tender(t, self.po)
        self.assertEqual(Bid.objects.get(pk=bids[0].pk).latest.get_payload()['price'], 870000)
        # TC
        svc.forward(t, self.po, 'TECHNICAL')
        self.page('tc1', f'/tenders/{t.pk}/review/', f'/tenders/{t.pk}/')
        self.page('po', f'/tenders/{t.pk}/', f'/tenders/{t.pk}/forward/technical/')
        with self.assertRaises(WorkflowError):                # T10 remarks mandatory
            svc.decide(t, self.tc1, 'TECHNICAL', 'REJECT', '')
        with self.assertRaises(WorkflowError):                # must evaluate all bids first
            svc.decide(t, self.tc1, 'TECHNICAL', 'APPROVE')
        for b in Bid.objects.filter(tender=t):
            svc.save_evaluation(t, self.tc1, b, b.vendor != self.vendors[3], 80, '')
        svc.decide(t, self.tc1, 'TECHNICAL', 'APPROVE')
        t.refresh_from_db()
        self.assertEqual(t.status, 'TECHNICAL_APPROVED')
        with self.assertRaises(WorkflowError):                # cannot skip stages
            svc.forward(t, self.po, 'CFO')
        svc.forward(t, self.po, 'FINANCE')
        svc.decide(t, self.fc1, 'FINANCE', 'APPROVE')
        t.refresh_from_db()
        self.assertEqual(t.status, 'QUOTATION_VALIDATION')
        stmt = t.statements.first()
        self.page('fc1', f'/tenders/{t.pk}/review/', f'/tenders/{t.pk}/statement/', f'/tenders/{t.pk}/statement/?export=csv')
        self.assertEqual([i.bid.vendor_id for i in stmt.items.all()], [self.vendors[1].pk, self.vendors[2].pk, self.vendors[0].pk])
        self.assertFalse(stmt.fewer_than_three)
        svc.validate_statement(t, self.po)
        t.refresh_from_db()
        self.assertEqual(t.status, 'CFO_REVIEW')
        l2 = stmt.items.get(rank=2)
        self.page('cfo', f'/tenders/{t.pk}/cfo/', f'/tenders/{t.pk}/')
        with self.assertRaises(WorkflowError):                # T15
            svc.decide(t, self.cfo, 'CFO', 'APPROVE', selected_item=l2)
        svc.decide(t, self.cfo, 'CFO', 'APPROVE', selected_item=l2, justification='Better delivery')
        t.refresh_from_db()
        self.assertEqual(t.status, 'APPROVED')
        self.page('po', f'/tenders/{t.pk}/award/', f'/tenders/{t.pk}/')
        mail.outbox.clear()
        svc.send_award_email(t.award, self.po, regret=True)
        t.refresh_from_db()
        self.assertEqual(t.status, 'AWARDED')
        to = [m.to[0] for m in mail.outbox]
        self.assertIn(self.vendors[2].user.email, to)
        self.assertIn('Award of Tender', next(m.subject for m in mail.outbox if m.to[0] == self.vendors[2].user.email))
        self.page('po', f'/tenders/{t.pk}/award/', f'/tenders/{t.pk}/', '/reports/?export=csv', '/tenders/?export=csv')
        self.page('vendor3', f'/tenders/{t.pk}/', '/my-bids/')
        # audit chain intact
        prev = ''
        for l in AuditLog.objects.order_by('id'):
            self.assertEqual(l.prev_hash, prev)
            self.assertEqual(l.hash, l.compute_hash())
            prev = l.hash

    def test_limited_tender_visibility(self):
        from .views import tenders_for
        t = self.make_tender('LIMITED', invites=self.vendors[:3])
        self.assertIn(t, tenders_for(self.vendors[0].user))
        self.assertNotIn(t, tenders_for(self.vendors[3].user))      # T2

    def test_blacklisted_vendor_sees_nothing(self):
        from .views import tenders_for
        t = self.make_tender()
        Vendor.objects.filter(pk=self.vendors[0].pk).update(status='BLACKLISTED')
        self.assertEqual(tenders_for(User.objects.get(pk=self.vendors[0].user_id)).count(), 0)   # T20
        with self.assertRaises(WorkflowError):
            self.bid(t, Vendor.objects.get(pk=self.vendors[0].pk), 1)

    def test_fewer_than_three_needs_justification(self):
        t = self.make_tender()
        self.bid(t, self.vendors[0], 100)
        self.bid(t, self.vendors[1], 200)
        self.close_deadline(t)
        svc.open_tender(t, self.po)
        svc.forward(t, self.po, 'TECHNICAL')
        for b in Bid.objects.filter(tender=t):
            svc.save_evaluation(t, self.tc1, b, True, None, '')
        svc.decide(t, self.tc1, 'TECHNICAL', 'APPROVE')
        svc.forward(t, self.po, 'FINANCE')
        svc.decide(t, self.fc1, 'FINANCE', 'APPROVE')
        with self.assertRaises(WorkflowError):                      # T13
            svc.validate_statement(t, self.po)
        svc.validate_statement(t, self.po, 'Only two compliant vendors')

    def test_reject_send_back_and_failed_email_retry(self):
        t = self.make_tender()
        self.bid(t, self.vendors[0], 100)
        self.close_deadline(t)
        svc.open_tender(t, self.po)
        svc.forward(t, self.po, 'TECHNICAL')
        svc.decide(t, self.tc1, 'TECHNICAL', 'SEND_BACK', 'Clarify specs')
        t.refresh_from_db()
        self.assertEqual(t.status, 'SENT_BACK')
        svc.forward(t, self.po, 'TECHNICAL', 'Clarified')          # re-forward to same stage
        svc.decide(t, User.objects.get(username='tc2'), 'TECHNICAL', 'REJECT', 'No')
        t.refresh_from_db()
        self.assertEqual(t.status, 'REJECTED')
        svc.reevaluate(t, self.po)
        t.refresh_from_db()
        self.assertEqual(t.status, 'OPENED')

    def test_pages_render(self):
        from django.test import Client
        t = self.make_tender()
        c = Client()
        self.assertTrue(c.login(username='po', password='Tender@12345'))
        for url in ('/', '/notices/', '/tenders/', f'/tenders/{t.pk}/', '/reports/', '/audit/', '/vendors/', '/emails/', '/notifications/'):
            self.assertEqual(c.get(url).status_code, 200, url)
        c = Client()
        self.assertTrue(c.login(username='vendor1', password='Tender@12345'))
        for url in ('/', '/tenders/', f'/tenders/{t.pk}/', f'/tenders/{t.pk}/bid/', '/my-bids/', '/profile/'):
            self.assertEqual(c.get(url).status_code, 200, url)
        r = c.post(f'/tenders/{t.pk}/bid/', dict(price='500', taxes='0', delivery_days='5', validity_days='30', remarks='r', confirm='on'))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(c.get('/vendors/').status_code, 403)       # RBAC
        self.assertEqual(c.get(f'/tenders/{t.pk}/review/').status_code, 403)
