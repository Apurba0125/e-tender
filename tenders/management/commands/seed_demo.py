"""Creates the admin + one demo user per internal role + two approved demo vendors."""
from django.conf import settings
from django.core.management.base import BaseCommand

from accounts.models import User, Vendor
from tenders.models import Config


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument('--password', default='Tender@12345')

    def handle(self, *a, **o):
        pw = o['password']
        for key, (val, hlp) in Config.DEFAULTS.items():
            Config.objects.get_or_create(key=key, defaults=dict(value=val, help=hlp))
        admin, created = User.objects.get_or_create(username='admin', defaults=dict(
            email=settings.ADMIN_EMAIL, role='ADMIN', is_staff=True, is_superuser=True, email_verified=True))
        if created:
            admin.set_password(pw)
            admin.save()
        for role, name in (('PO', 'po'), ('HOD', 'hod'), ('TC', 'tc1'), ('TC', 'tc2'), ('FC', 'fc1'), ('FC', 'fc2'), ('CFO', 'cfo')):
            u, c = User.objects.get_or_create(username=name, defaults=dict(
                email=f'{name}@example.com', role=role, first_name=name.upper(), email_verified=True))
            if c:
                u.set_password(pw)
                u.save()
        for i, (co, cat) in enumerate((('Alpha Supplies Pvt Ltd', 'IT'), ('Beta Traders', 'IT'), ('Gamma Infra', 'Civil'), ('Delta Tech', 'IT')), 1):
            u, c = User.objects.get_or_create(username=f'vendor{i}', defaults=dict(
                email=f'vendor{i}@example.com', role='VENDOR', first_name=co.split()[0], email_verified=True))
            if c:
                u.set_password(pw)
                u.save()
                Vendor.objects.create(user=u, company_name=co, contact_name=co.split()[0], phone='9000000000', address='Kolkata',
                                      tax_id=f'GST{i:04d}', pan=f'PAN{i:04d}', category=cat, status='APPROVED')
        self.stdout.write(self.style.SUCCESS(
            f'Seeded. Login with admin / po / tc1 / tc2 / fc1 / fc2 / cfo (internal portal) and vendor1..4 (vendor portal). Password: {pw}'))
