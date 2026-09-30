import hashlib
import json
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import models
from django.utils import timezone

from accounts.models import Vendor
from . import crypto
from .validators import validate_upload

User = settings.AUTH_USER_MODEL


class Config(models.Model):
    """Key/value system settings (editable in admin)."""
    key = models.CharField(max_length=60, primary_key=True)
    value = models.CharField(max_length=200)
    help = models.CharField(max_length=200, blank=True)

    DEFAULTS = {
        'approval_mode': ('ANY', 'TC/FC approval rule: ANY, MAJORITY or ALL members'),
        'require_notice': ('1', '1 = tender can be published only if linked to a published notice'),
        'allow_fewer_than_3': ('1', '1 = allow statement with <3 bids (justification required)'),
        'sla_days': ('3', 'Days before a pending approval triggers a reminder'),
        'unique_approver_per_tender': ('1', '1 = one person cannot approve at more than one stage'),
    }

    @classmethod
    def get(cls, key):
        try:
            return cls.objects.get(key=key).value
        except cls.DoesNotExist:
            return cls.DEFAULTS[key][0]

    def __str__(self):
        return f'{self.key}={self.value}'


class Notice(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        PUBLISHED = 'PUBLISHED', 'Published'
        ARCHIVED = 'ARCHIVED', 'Archived'

    notice_no = models.CharField(max_length=30, blank=True, unique=True)
    title = models.CharField(max_length=250)
    description = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    attachment = models.FileField(upload_to='notices/', blank=True, validators=[validate_upload])
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='notices')
    published_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *a, **k):
        super().save(*a, **k)
        if not self.notice_no:
            self.notice_no = f'NOT/{timezone.now().year}/{self.pk:04d}'
            super().save(update_fields=['notice_no'])

    def __str__(self):
        return f'{self.notice_no} {self.title}'


class Tender(models.Model):
    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Draft'
        PUBLISHED = 'PUBLISHED', 'Published'
        BIDDING_OPEN = 'BIDDING_OPEN', 'Bidding open'
        BIDDING_CLOSED = 'BIDDING_CLOSED', 'Bidding closed'
        OPENED = 'OPENED', 'Opened'
        TECHNICAL_REVIEW = 'TECHNICAL_REVIEW', 'Technical review'
        TECHNICAL_APPROVED = 'TECHNICAL_APPROVED', 'Technically approved'
        FINANCE_REVIEW = 'FINANCE_REVIEW', 'Finance review'
        QUOTATION_VALIDATION = 'QUOTATION_VALIDATION', 'Quotation validation'
        CFO_REVIEW = 'CFO_REVIEW', 'CFO review'
        APPROVED = 'APPROVED', 'Approved (award pending)'
        AWARDED = 'AWARDED', 'Awarded'
        CLOSED = 'CLOSED', 'Closed'
        REJECTED = 'REJECTED', 'Rejected'
        SENT_BACK = 'SENT_BACK', 'Sent back'
        CANCELLED = 'CANCELLED', 'Cancelled'

    class Visibility(models.TextChoices):
        OPEN = 'OPEN', 'Open – all approved vendors'
        LIMITED = 'LIMITED', 'Limited – invited vendors only'

    tender_no = models.CharField(max_length=30, blank=True, unique=True)
    notice = models.ForeignKey(Notice, null=True, blank=True, on_delete=models.PROTECT, related_name='tenders')
    title = models.CharField(max_length=250)
    category = models.CharField(max_length=80)
    description = models.TextField('Description / specification')
    quantity = models.DecimalField(max_digits=14, decimal_places=2, default=1)
    unit = models.CharField(max_length=30, default='Nos')
    estimated_value = models.DecimalField(max_digits=16, decimal_places=2)
    currency = models.CharField(max_length=5, default='INR')
    emd_amount = models.DecimalField('EMD / security deposit', max_digits=14, decimal_places=2, default=0)
    eligibility = models.TextField(blank=True)
    terms = models.TextField('Payment & delivery terms / T&C', blank=True)
    evaluation_criteria = models.TextField(blank=True)
    contact_person = models.CharField(max_length=200, blank=True)
    visibility = models.CharField(max_length=8, choices=Visibility.choices, default=Visibility.OPEN)
    bid_start_at = models.DateTimeField()
    bid_end_at = models.DateTimeField()
    opening_at = models.DateTimeField()
    status = models.CharField(max_length=22, choices=Status.choices, default=Status.DRAFT)
    return_stage = models.CharField(max_length=10, blank=True)   # stage that rejected / sent back
    status_reason = models.TextField(blank=True)
    round = models.PositiveIntegerField(default=0)                # increments on each forward
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='tenders_created')
    opened_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    opened_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *a, **k):
        super().save(*a, **k)
        if not self.tender_no:
            self.tender_no = f'TND/{timezone.now().year}/{self.pk:04d}'
            super().save(update_fields=['tender_no'])

    def __str__(self):
        return f'{self.tender_no} {self.title}'

    # ---- time-driven state (scheduler + lazy) ----
    def sync_status(self):
        if self.status in (self.Status.PUBLISHED, self.Status.BIDDING_OPEN):
            now = timezone.now()
            new = self.status
            if now >= self.bid_end_at:
                new = self.Status.BIDDING_CLOSED
            elif now >= self.bid_start_at:
                new = self.Status.BIDDING_OPEN
            if new != self.status:
                self.status = new
                self.save(update_fields=['status'])
        return self.status

    @property
    def accepting_bids(self):
        self.sync_status()
        now = timezone.now()
        return (self.status in (self.Status.PUBLISHED, self.Status.BIDDING_OPEN)
                and self.bid_start_at <= now < self.bid_end_at)

    @property
    def can_open(self):
        self.sync_status()
        return self.status == self.Status.BIDDING_CLOSED and timezone.now() >= self.bid_end_at

    @property
    def bids_visible(self):
        return self.opened_at is not None

    @property
    def is_live(self):
        return self.status not in (self.Status.DRAFT,)


class TenderInvite(models.Model):
    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name='invites')
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='invites')
    invited_at = models.DateTimeField(auto_now_add=True)
    viewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('tender', 'vendor')


class TenderDocument(models.Model):
    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name='documents')
    file = models.FileField(upload_to='tender_docs/', validators=[validate_upload])
    uploaded_by = models.ForeignKey(User, on_delete=models.PROTECT)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    @property
    def name(self):
        return self.file.name.split('/')[-1]


class Corrigendum(models.Model):
    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name='corrigenda')
    text = models.TextField()
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']


# ------------------------------ Bids ------------------------------
class Bid(models.Model):
    class Status(models.TextChoices):
        SUBMITTED = 'SUBMITTED', 'Submitted'
        REVISED = 'REVISED', 'Revised'
        WITHDRAWN = 'WITHDRAWN', 'Withdrawn'
        OPENED = 'OPENED', 'Opened'
        TECH_QUALIFIED = 'TECH_QUALIFIED', 'Technically qualified'
        TECH_DISQUALIFIED = 'TECH_DISQUALIFIED', 'Technically disqualified'
        SHORTLISTED = 'SHORTLISTED', 'Shortlisted'
        SELECTED = 'SELECTED', 'Selected'
        NOT_SELECTED = 'NOT_SELECTED', 'Not selected'

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name='bids')
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name='bids')
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SUBMITTED)
    current_version = models.PositiveIntegerField(default=0)
    receipt_no = models.CharField(max_length=40, blank=True)
    submitted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ('tender', 'vendor')       # BID-05: one active bid per vendor

    @property
    def latest(self):
        return self.versions.order_by('-version_no').first()

    def __str__(self):
        return f'{self.receipt_no} – {self.vendor}'


class BidVersion(models.Model):
    bid = models.ForeignKey(Bid, on_delete=models.CASCADE, related_name='versions')
    version_no = models.PositiveIntegerField()
    encrypted_payload = models.TextField()
    document = models.FileField(upload_to='bid_docs/', blank=True, validators=[validate_upload])
    emd_proof = models.FileField(upload_to='bid_emd/', blank=True, validators=[validate_upload])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('bid', 'version_no')
        ordering = ['-version_no']

    def set_payload(self, data: dict):
        self.encrypted_payload = crypto.encrypt(self.bid.tender_id, json.dumps(data, default=str))

    def get_payload(self, as_owner=False):
        """BID-06: contents are sealed until the PO opens the tender."""
        if not as_owner and not self.bid.tender.bids_visible:
            raise PermissionDenied('Bids are sealed until the tender is opened.')
        return json.loads(crypto.decrypt(self.bid.tender_id, self.encrypted_payload))


# ---------------------------- Approvals ----------------------------
class Stage(models.TextChoices):
    TECHNICAL = 'TECHNICAL', 'Technical'
    FINANCE = 'FINANCE', 'Finance'
    CFO = 'CFO', 'CFO'


class StageForward(models.Model):
    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name='forwards')
    stage = models.CharField(max_length=10, choices=Stage.choices)
    round = models.PositiveIntegerField()
    forwarded_by = models.ForeignKey(User, on_delete=models.PROTECT)
    forwarded_at = models.DateTimeField(auto_now_add=True)
    remarks = models.TextField(blank=True)
    reminded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['forwarded_at']


class StageDocument(models.Model):          # optional supporting documents (APR-44)
    forward = models.ForeignKey(StageForward, on_delete=models.CASCADE, related_name='documents')
    file = models.FileField(upload_to='stage_docs/', validators=[validate_upload])

    @property
    def name(self):
        return self.file.name.split('/')[-1]


class Approval(models.Model):
    class Decision(models.TextChoices):
        APPROVE = 'APPROVE', 'Approve'
        REJECT = 'REJECT', 'Reject'
        SEND_BACK = 'SEND_BACK', 'Send back for clarification'

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name='approvals')
    stage = models.CharField(max_length=10, choices=Stage.choices)
    round = models.PositiveIntegerField()
    approver = models.ForeignKey(User, on_delete=models.PROTECT)
    decision = models.CharField(max_length=10, choices=Decision.choices)
    remarks = models.TextField(blank=True)
    decided_at = models.DateTimeField(auto_now_add=True)
    ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        unique_together = ('tender', 'stage', 'round', 'approver')
        ordering = ['decided_at']


class TechnicalEvaluation(models.Model):
    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name='evaluations')
    bid = models.OneToOneField(Bid, on_delete=models.CASCADE, related_name='evaluation')
    qualified = models.BooleanField()
    score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    remarks = models.TextField(blank=True)
    evaluated_by = models.ForeignKey(User, on_delete=models.PROTECT)
    evaluated_at = models.DateTimeField(auto_now=True)


class ComparativeStatement(models.Model):
    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Pending validation'
        VALIDATED = 'VALIDATED', 'Validated'
        SUPERSEDED = 'SUPERSEDED', 'Superseded'

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name='statements')
    generated_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    fewer_than_three = models.BooleanField(default=False)
    justification = models.TextField(blank=True)
    validated_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.PROTECT)
    validated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-generated_at']


class StatementItem(models.Model):
    statement = models.ForeignKey(ComparativeStatement, on_delete=models.CASCADE, related_name='items')
    bid = models.ForeignKey(Bid, on_delete=models.PROTECT)
    rank = models.PositiveSmallIntegerField()
    price = models.DecimalField(max_digits=16, decimal_places=2)
    taxes = models.DecimalField(max_digits=16, decimal_places=2, default=0)
    delivery_days = models.PositiveIntegerField(default=0)
    validity_days = models.PositiveIntegerField(default=0)
    tech_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    notes = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ['rank']

    @property
    def total(self):
        return self.price + self.taxes

    @property
    def label(self):
        return f'L{self.rank}'

    @property
    def deviation_pct(self):
        l1 = self.statement.items.first()
        if not l1 or not l1.total:
            return 0
        return round((self.total - l1.total) / l1.total * 100, 2)


class Award(models.Model):
    class Status(models.TextChoices):
        APPROVED = 'APPROVED', 'Approved'
        SENT = 'SENT', 'Email sent'
        ACCEPTED = 'ACCEPTED', 'Accepted by vendor'

    tender = models.OneToOneField(Tender, on_delete=models.CASCADE, related_name='award')
    bid = models.ForeignKey(Bid, on_delete=models.PROTECT)
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT)
    awarded_value = models.DecimalField(max_digits=16, decimal_places=2)
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT)
    justification = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.APPROVED)
    sent_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)


class EmailLog(models.Model):
    tender = models.ForeignKey(Tender, null=True, on_delete=models.SET_NULL, related_name='emails')
    kind = models.CharField(max_length=20, default='GENERAL')
    recipient = models.EmailField()
    subject = models.CharField(max_length=250)
    body = models.TextField()
    status = models.CharField(max_length=10, default='PENDING')
    attempts = models.PositiveIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class Notification(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='notifications')
    kind = models.CharField(max_length=30, default='INFO')
    message = models.CharField(max_length=400)
    link = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class AuditLog(models.Model):
    """AUD-01 / NFR-11: append-only, hash-chained (tamper-evident)."""
    actor = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    actor_name = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=60)
    entity_type = models.CharField(max_length=40, blank=True)
    entity_id = models.CharField(max_length=40, blank=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=250, blank=True)
    timestamp = models.DateTimeField(default=timezone.now)
    prev_hash = models.CharField(max_length=64, blank=True)
    hash = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ['-id']

    def compute_hash(self):
        s = '|'.join(str(x) for x in (self.prev_hash, self.actor_name, self.action, self.entity_type,
                                     self.entity_id, self.old_value, self.new_value, self.ip, self.timestamp.isoformat()))
        return hashlib.sha256(s.encode()).hexdigest()

    def save(self, *a, **k):
        if self.pk:
            raise PermissionDenied('Audit log is append-only.')
        last = AuditLog.objects.order_by('-id').first()
        self.prev_hash = last.hash if last else ''
        self.hash = self.compute_hash()
        super().save(*a, **k)

    def delete(self, *a, **k):
        raise PermissionDenied('Audit log is append-only.')
