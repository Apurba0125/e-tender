from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = 'ADMIN', 'Admin'
        PO = 'PO', 'Purchase Officer'
        TC = 'TC', 'Technical Committee'
        FC = 'FC', 'Finance Committee'
        CFO = 'CFO', 'CFO'
        VENDOR = 'VENDOR', 'Vendor'

    email = models.EmailField(unique=True)
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.VENDOR)
    phone = models.CharField(max_length=30, blank=True)
    email_verified = models.BooleanField(default=False)
    failed_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    INTERNAL_ROLES = ('ADMIN', 'PO', 'TC', 'FC', 'CFO')

    @property
    def is_vendor(self):
        return self.role == self.Role.VENDOR

    @property
    def is_internal(self):
        return self.role in self.INTERNAL_ROLES

    def __str__(self):
        return f'{self.get_full_name() or self.username} ({self.role})'


class Vendor(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending'
        APPROVED = 'APPROVED', 'Approved'
        REJECTED = 'REJECTED', 'Rejected'
        SUSPENDED = 'SUSPENDED', 'Suspended'
        BLACKLISTED = 'BLACKLISTED', 'Blacklisted'

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='vendor')
    company_name = models.CharField(max_length=200)
    contact_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=30)
    address = models.TextField()
    tax_id = models.CharField('GST / Tax ID', max_length=40)
    pan = models.CharField('PAN', max_length=20)
    bank_details = models.TextField(blank=True)
    category = models.CharField('Category of supply', max_length=80)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    status_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['company_name']

    @property
    def can_bid(self):
        return self.status == self.Status.APPROVED and self.user.email_verified

    def __str__(self):
        return self.company_name


class VendorDocument(models.Model):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='documents')
    title = models.CharField(max_length=120)
    file = models.FileField(upload_to='vendor_docs/')
    expires_on = models.DateField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
