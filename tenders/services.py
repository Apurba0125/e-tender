"""Workflow engine: every state transition is validated here and audited."""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from accounts.models import User, Vendor
from .models import (Approval, AuditLog, Award, Bid, ComparativeStatement, Config, EmailLog,
                     Notification, Stage, StageDocument, StageForward, StatementItem, Tender, TenderInvite)

S = Tender.Status


class WorkflowError(Exception):
    pass


# ------------------------------ helpers ------------------------------
def client_ip(request):
    if request is None:
        return None
    fwd = request.META.get('HTTP_X_FORWARDED_FOR')
    return (fwd.split(',')[0].strip() if fwd else request.META.get('REMOTE_ADDR')) or None


def audit(actor, action, entity=None, old='', new='', request=None):
    AuditLog.objects.create(
        actor=actor if getattr(actor, 'pk', None) else None,
        actor_name=getattr(actor, 'username', 'system'),
        action=action,
        entity_type=entity.__class__.__name__ if entity is not None else '',
        entity_id=str(getattr(entity, 'pk', '')) if entity is not None else '',
        old_value=str(old), new_value=str(new), ip=client_ip(request),
        user_agent=(request.META.get('HTTP_USER_AGENT', '')[:250] if request else ''))


def notify(users, kind, message, link='', email_subject=None, tender=None):
    """In-app notification (+ email when email_subject given)."""
    for u in users:
        Notification.objects.create(user=u, kind=kind, message=message[:400], link=link)
        if email_subject and u.email:
            deliver_email(u.email, email_subject, f'{message}\n\n{settings.SITE_URL}{link}', tender=tender, kind='NOTIFY')


def deliver_email(to, subject, body, tender=None, kind='GENERAL', log=None):
    """Idempotent-ish sending: a log row is always written; failures can be retried."""
    log = log or EmailLog.objects.create(tender=tender, kind=kind, recipient=to, subject=subject, body=body)
    log.attempts += 1
    try:
        if settings.EMAIL_BACKEND == 'django.core.mail.backends.console.EmailBackend':
            raise RuntimeError(
                'Console email backend is active; email was not delivered. '
                'Configure EMAIL_HOST_USER and EMAIL_HOST_PASSWORD for SMTP, then restart the server.'
            )
        send_mail(log.subject, log.body, settings.DEFAULT_FROM_EMAIL, [log.recipient])
        log.status, log.sent_at, log.error = 'SENT', timezone.now(), ''
    except Exception as exc:  # SMTP down etc.
        log.status, log.error = 'FAILED', str(exc)[:500]
    log.save()
    return log


def users_with_role(*roles):
    return list(User.objects.filter(role__in=roles, is_active=True))


def approved_vendors():
    return Vendor.objects.filter(status=Vendor.Status.APPROVED, user__is_active=True)


def eligible_vendors(tender):
    if tender.visibility == Tender.Visibility.OPEN:
        return approved_vendors()
    return approved_vendors().filter(invites__tender=tender)


# ------------------------------ publishing ------------------------------
def publish_notice(notice, user, request=None):
    from .models import Notice
    if notice.status != Notice.Status.DRAFT:
        raise WorkflowError('Only draft notices can be published.')
    notice.status, notice.published_by, notice.published_at = Notice.Status.PUBLISHED, user, timezone.now()
    notice.save()
    audit(user, 'NOTICE_PUBLISHED', notice, new=notice.notice_no, request=request)
    notify([v.user for v in approved_vendors()], 'NOTICE', f'New notice published: {notice.notice_no} – {notice.title}',
           '/notices/', email_subject=f'Notice {notice.notice_no}: {notice.title}')


def publish_tender(tender, user, request=None):
    if tender.status != S.DRAFT:
        raise WorkflowError('Only draft tenders can be published.')
    if Config.get('require_notice') == '1' and not (tender.notice and tender.notice.status == 'PUBLISHED'):
        raise WorkflowError('Link a published notice before publishing this tender.')
    if tender.visibility == tender.Visibility.LIMITED and not tender.invites.exists():
        raise WorkflowError('A limited tender needs at least one invited vendor.')
    if tender.bid_end_at <= timezone.now():
        raise WorkflowError('Bid end date must be in the future.')
    tender.status = S.PUBLISHED
    tender.save(update_fields=['status'])
    tender.sync_status()
    audit(user, 'TENDER_PUBLISHED', tender, new=tender.tender_no, request=request)
    notify([v.user for v in eligible_vendors(tender)], 'TENDER', f'New tender: {tender.tender_no} – {tender.title}',
           f'/tenders/{tender.pk}/', email_subject=f'Tender {tender.tender_no}: {tender.title}', tender=tender)


def extend_deadline(tender, user, new_end, reason, request=None):
    tender.sync_status()
    if tender.status not in (S.PUBLISHED, S.BIDDING_OPEN):
        raise WorkflowError('Deadline can only be extended while bidding is live.')
    if new_end <= tender.bid_end_at:
        raise WorkflowError('New deadline must be later than the current one.')
    old = tender.bid_end_at
    tender.bid_end_at = new_end
    if tender.opening_at < new_end:
        tender.opening_at = new_end
    tender.save(update_fields=['bid_end_at', 'opening_at'])
    add_corrigendum(tender, user, f'Deadline extended from {old:%d %b %Y %H:%M} to {new_end:%d %b %Y %H:%M}. Reason: {reason}', request)
    audit(user, 'DEADLINE_EXTENDED', tender, old=old, new=new_end, request=request)


def add_corrigendum(tender, user, text, request=None):
    tender.corrigenda.create(text=text, created_by=user)
    audit(user, 'CORRIGENDUM', tender, new=text, request=request)
    notify([v.user for v in eligible_vendors(tender)], 'CORRIGENDUM', f'Corrigendum on {tender.tender_no}: {text}',
           f'/tenders/{tender.pk}/', email_subject=f'Corrigendum – {tender.tender_no}', tender=tender)


def cancel_tender(tender, user, reason, request=None):
    if tender.status in (S.AWARDED, S.CLOSED, S.CANCELLED):
        raise WorkflowError('This tender can no longer be cancelled.')
    if not reason.strip():
        raise WorkflowError('A reason is required.')
    old = tender.status
    tender.status, tender.status_reason = S.CANCELLED, reason
    tender.save(update_fields=['status', 'status_reason'])
    audit(user, 'TENDER_CANCELLED', tender, old=old, new=reason, request=request)
    if old != S.DRAFT:
        notify([v.user for v in eligible_vendors(tender)], 'TENDER', f'Tender {tender.tender_no} was cancelled.',
               f'/tenders/{tender.pk}/', email_subject=f'Tender cancelled – {tender.tender_no}', tender=tender)


def clone_tender(tender, user, request=None):
    old_pk = tender.pk
    invites = list(tender.invites.values_list('vendor_id', flat=True))
    tender.pk = tender.tender_no = None
    tender.status, tender.status_reason, tender.return_stage, tender.round = S.DRAFT, '', '', 0
    tender.opened_at = tender.opened_by = None
    tender.created_by = user
    tender.save()
    for vid in invites:
        TenderInvite.objects.create(tender=tender, vendor_id=vid)
    audit(user, 'TENDER_CLONED', tender, old=old_pk, new=tender.pk, request=request)
    return tender


# ------------------------------ bidding ------------------------------
def submit_bid(tender, vendor, data, document, emd_proof, request=None):
    """Create or revise a bid. Server-time deadline is re-checked inside the transaction (BID-04)."""
    from .models import BidVersion
    if not vendor.can_bid:
        raise WorkflowError('Your vendor account is not approved to bid.')
    with transaction.atomic():
        t = Tender.objects.select_for_update().get(pk=tender.pk)
        if not t.accepting_bids:
            raise WorkflowError('Bidding is closed for this tender (server time).')
        bid, created = Bid.objects.get_or_create(tender=t, vendor=vendor)
        bid.current_version += 1
        bid.status = Bid.Status.SUBMITTED if created else Bid.Status.REVISED
        bid.submitted_at = timezone.now()
        if not bid.receipt_no:
            bid.receipt_no = f'RCPT-{timezone.now():%Y%m%d%H%M%S}-{bid.pk:05d}'
        bid.save()
        prev = bid.versions.first()
        ver = BidVersion(bid=bid, version_no=bid.current_version)
        ver.set_payload(data)
        ver.document = document or (prev.document if prev else '')
        ver.emd_proof = emd_proof or (prev.emd_proof if prev else '')
        ver.save()
    audit(vendor.user, 'BID_SUBMITTED' if created else 'BID_REVISED', bid, new=f'v{bid.current_version}', request=request)
    deliver_email(vendor.user.email, f'Bid receipt {bid.receipt_no} – {t.tender_no}',
                  f'Your bid for {t.tender_no} – {t.title} was received.\nReceipt: {bid.receipt_no}\n'
                  f'Version: {bid.current_version}\nTime: {bid.submitted_at:%d %b %Y %H:%M:%S %Z}', tender=t, kind='RECEIPT')
    notify([vendor.user], 'BID', f'Bid {bid.receipt_no} (v{bid.current_version}) recorded.', f'/tenders/{t.pk}/bid/')
    return bid


def withdraw_bid(bid, user, request=None):
    if not bid.tender.accepting_bids:
        raise WorkflowError('Bids can be withdrawn only before the deadline.')
    bid.status = Bid.Status.WITHDRAWN
    bid.save(update_fields=['status'])
    audit(user, 'BID_WITHDRAWN', bid, request=request)
    deliver_email(user.email, f'Bid withdrawn – {bid.tender.tender_no}', f'Your bid {bid.receipt_no} was withdrawn.', tender=bid.tender)


# ------------------------------ opening ------------------------------
def open_tender(tender, user, request=None):
    tender.sync_status()
    if not tender.can_open:
        raise WorkflowError('The tender can be opened only after the bid deadline has passed.')
    with transaction.atomic():
        tender.bids.exclude(status=Bid.Status.WITHDRAWN).update(status=Bid.Status.OPENED)
        tender.status, tender.opened_by, tender.opened_at = S.OPENED, user, timezone.now()
        tender.save(update_fields=['status', 'opened_by', 'opened_at'])
    audit(user, 'TENDER_OPENED', tender, new=f'{tender.bids.exclude(status="WITHDRAWN").count()} bids', request=request)


def live_bids(tender):
    return tender.bids.exclude(status=Bid.Status.WITHDRAWN).select_related('vendor')


# ------------------------------ approvals ------------------------------
ROLE_FOR_STAGE = {Stage.TECHNICAL: 'TC', Stage.FINANCE: 'FC', Stage.CFO: 'CFO'}
STATUS_FOR_STAGE = {Stage.TECHNICAL: S.TECHNICAL_REVIEW, Stage.FINANCE: S.FINANCE_REVIEW, Stage.CFO: S.CFO_REVIEW}


def forward(tender, user, stage, remarks='', files=()):
    """PO forwards to the next stage. Stages are strictly sequential (APR-40)."""
    allowed_from = {
        Stage.TECHNICAL: [S.OPENED],
        Stage.FINANCE: [S.TECHNICAL_APPROVED],
        Stage.CFO: [],
    }[stage]
    sent_back_here = tender.status == S.SENT_BACK and tender.return_stage == stage
    if tender.status not in allowed_from and not sent_back_here:
        raise WorkflowError(f'Tender cannot be forwarded to {stage} from status {tender.get_status_display()}.')
    if stage == Stage.TECHNICAL and not live_bids(tender).exists():
        raise WorkflowError('There are no bids to evaluate. Cancel or re-publish the tender.')
    tender.round += 1
    tender.status, tender.return_stage, tender.status_reason = STATUS_FOR_STAGE[stage], '', ''
    tender.save(update_fields=['round', 'status', 'return_stage', 'status_reason'])
    fw = StageForward.objects.create(tender=tender, stage=stage, round=tender.round, forwarded_by=user, remarks=remarks)
    for f in files:
        StageDocument.objects.create(forward=fw, file=f)
    audit(user, f'FORWARDED_{stage}', tender, new=remarks)
    notify(users_with_role(ROLE_FOR_STAGE[stage]), 'APPROVAL', f'{tender.tender_no} awaits your {stage.lower()} review.',
           f'/tenders/{tender.pk}/review/', email_subject=f'Action required – {tender.tender_no}', tender=tender)
    return fw


def _required_approvals(role):
    mode = Config.get('approval_mode')
    n = max(User.objects.filter(role=role, is_active=True).count(), 1)
    return {'ANY': 1, 'MAJORITY': n // 2 + 1, 'ALL': n}.get(mode, 1)


def decide(tender, user, stage, decision, remarks='', request=None, selected_item=None, justification=''):
    """Record a TC / FC / CFO decision and advance the workflow when the approval rule is met."""
    if user.role != ROLE_FOR_STAGE[stage]:
        raise WorkflowError('You are not authorised for this stage.')
    if tender.status != STATUS_FOR_STAGE[stage]:
        raise WorkflowError('This tender is not awaiting your decision.')
    if tender.created_by_id == user.pk:
        raise WorkflowError('Segregation of duties: the tender creator cannot approve it.')
    if Config.get('unique_approver_per_tender') == '1' and tender.approvals.filter(approver=user).exclude(stage=stage).exists():
        raise WorkflowError('You already acted at another stage of this tender.')
    if Approval.objects.filter(tender=tender, stage=stage, round=tender.round, approver=user).exists():
        raise WorkflowError('You have already recorded a decision in this round.')
    if decision in (Approval.Decision.REJECT, Approval.Decision.SEND_BACK) and not remarks.strip():
        raise WorkflowError('Remarks are mandatory when rejecting or sending back.')

    if decision == Approval.Decision.APPROVE:
        if stage == Stage.TECHNICAL:
            live = live_bids(tender)
            missing = [b for b in live if not hasattr(b, 'evaluation')]
            if missing:
                raise WorkflowError('Evaluate every bid (qualified / not qualified) before approving.')
            if not live.filter(evaluation__qualified=True).exists():
                raise WorkflowError('No bid is technically qualified – reject or send back instead.')
        if stage == Stage.FINANCE and not live_bids(tender).filter(status=Bid.Status.TECH_QUALIFIED).exists():
            raise WorkflowError('No technically qualified bids to take forward.')
        if stage == Stage.CFO:
            if selected_item is None:
                raise WorkflowError('Select the vendor to award.')
            if selected_item.rank != 1 and not justification.strip():
                raise WorkflowError('Justification is mandatory when choosing a vendor other than L1.')

    with transaction.atomic():
        Approval.objects.create(tender=tender, stage=stage, round=tender.round, approver=user, decision=decision,
                                remarks=remarks, ip=client_ip(request))
        audit(user, f'{stage}_{decision}', tender, new=remarks, request=request)
        pos = users_with_role('PO')
        if decision == Approval.Decision.REJECT:
            tender.status, tender.return_stage, tender.status_reason = S.REJECTED, stage, remarks
            tender.save(update_fields=['status', 'return_stage', 'status_reason'])
            notify(pos, 'APPROVAL', f'{tender.tender_no} rejected at {stage} stage: {remarks}', f'/tenders/{tender.pk}/',
                   email_subject=f'Rejected – {tender.tender_no}', tender=tender)
        elif decision == Approval.Decision.SEND_BACK:
            tender.status, tender.return_stage, tender.status_reason = S.SENT_BACK, stage, remarks
            tender.save(update_fields=['status', 'return_stage', 'status_reason'])
            notify(pos, 'APPROVAL', f'{tender.tender_no} sent back by {stage}: {remarks}', f'/tenders/{tender.pk}/',
                   email_subject=f'Sent back – {tender.tender_no}', tender=tender)
        else:
            got = tender.approvals.filter(stage=stage, round=tender.round, decision='APPROVE').count()
            if got >= _required_approvals(ROLE_FOR_STAGE[stage]):
                _advance(tender, user, stage, selected_item, justification, pos)
            else:
                notify(pos, 'APPROVAL', f'{tender.tender_no}: {got} {stage.lower()} approval(s) recorded, more needed.', f'/tenders/{tender.pk}/')


def _advance(tender, user, stage, selected_item, justification, pos):
    if stage == Stage.TECHNICAL:
        tender.status = S.TECHNICAL_APPROVED
        tender.save(update_fields=['status'])
        notify(pos, 'APPROVAL', f'{tender.tender_no} approved by Technical Committee – forward to Finance.',
               f'/tenders/{tender.pk}/', email_subject=f'TC approved – {tender.tender_no}', tender=tender)
    elif stage == Stage.FINANCE:
        generate_statement(tender)
        notify(pos + users_with_role('FC'), 'STATEMENT', f'Comparative statement ready for {tender.tender_no}.',
               f'/tenders/{tender.pk}/statement/')
    else:
        stmt = tender.statements.exclude(status='SUPERSEDED').first()
        Award.objects.update_or_create(tender=tender, defaults=dict(
            bid=selected_item.bid, vendor=selected_item.bid.vendor, awarded_value=selected_item.total,
            approved_by=user, justification=justification, status=Award.Status.APPROVED))
        for it in stmt.items.all():
            it.bid.status = Bid.Status.SELECTED if it.pk == selected_item.pk else Bid.Status.NOT_SELECTED
            it.bid.save(update_fields=['status'])
        tender.status = S.APPROVED
        tender.save(update_fields=['status'])
        notify(pos, 'APPROVAL', f'CFO approved {tender.tender_no}. You can now send the award email.',
               f'/tenders/{tender.pk}/award/', email_subject=f'CFO approved – {tender.tender_no}', tender=tender)


def save_evaluation(tender, user, bid, qualified, score, remarks):
    from .models import TechnicalEvaluation
    if user.role != 'TC' or tender.status != S.TECHNICAL_REVIEW:
        raise WorkflowError('Technical evaluation is not open for you.')
    TechnicalEvaluation.objects.update_or_create(bid=bid, defaults=dict(
        tender=tender, qualified=qualified, score=score, remarks=remarks, evaluated_by=user))
    bid.status = Bid.Status.TECH_QUALIFIED if qualified else Bid.Status.TECH_DISQUALIFIED
    bid.save(update_fields=['status'])
    audit(user, 'TECH_EVALUATION', bid, new='qualified' if qualified else 'not qualified')


# ------------------------------ comparative statement ------------------------------
def generate_statement(tender):
    """APR-20/21/22: top-3 lowest total price among technically qualified bids."""
    rows = []
    for b in live_bids(tender).filter(status=Bid.Status.TECH_QUALIFIED):
        p = b.latest.get_payload()
        rows.append((Decimal(str(p['price'])) + Decimal(str(p.get('taxes') or 0)), b, p))
    rows.sort(key=lambda r: (r[0], r[1].submitted_at))
    top = rows[:3]
    tender.statements.exclude(status='SUPERSEDED').update(status='SUPERSEDED')
    stmt = ComparativeStatement.objects.create(tender=tender, fewer_than_three=len(top) < 3)
    for i, (total, b, p) in enumerate(top, 1):
        StatementItem.objects.create(
            statement=stmt, bid=b, rank=i, price=Decimal(str(p['price'])), taxes=Decimal(str(p.get('taxes') or 0)),
            delivery_days=int(p.get('delivery_days') or 0), validity_days=int(p.get('validity_days') or 0),
            tech_score=b.evaluation.score, notes=(p.get('remarks') or '')[:300])
        b.status = Bid.Status.SHORTLISTED
        b.save(update_fields=['status'])
    tender.status = S.QUOTATION_VALIDATION
    tender.save(update_fields=['status'])
    audit(None, 'STATEMENT_GENERATED', stmt, new=f'{len(top)} vendors')
    return stmt


def validate_statement(tender, user, justification='', request=None):
    if user.role not in ('PO', 'FC'):
        raise WorkflowError('Only the Purchase Officer or Finance Committee can validate.')
    if tender.status != S.QUOTATION_VALIDATION:
        raise WorkflowError('No statement awaiting validation.')
    stmt = tender.statements.exclude(status='SUPERSEDED').first()
    if stmt.fewer_than_three:
        if Config.get('allow_fewer_than_3') != '1':
            raise WorkflowError('At least three qualified bids are required by policy.')
        if not justification.strip():
            raise WorkflowError('Fewer than three qualified bids: a justification is required to proceed.')
    with transaction.atomic():
        stmt.status, stmt.validated_by, stmt.validated_at, stmt.justification = 'VALIDATED', user, timezone.now(), justification
        stmt.save()
        audit(user, 'STATEMENT_VALIDATED', stmt, new=justification, request=request)
        tender.round += 1
        tender.status = S.CFO_REVIEW
        tender.save(update_fields=['round', 'status'])
        StageForward.objects.create(tender=tender, stage=Stage.CFO, round=tender.round, forwarded_by=user, remarks=justification)
    notify(users_with_role('CFO'), 'APPROVAL', f'{tender.tender_no} awaits CFO approval.', f'/tenders/{tender.pk}/cfo/',
           email_subject=f'Action required – {tender.tender_no}', tender=tender)


# ------------------------------ post-rejection options ------------------------------
def reevaluate(tender, user, request=None):
    if tender.status != S.REJECTED:
        raise WorkflowError('Only rejected tenders can be re-evaluated.')
    tender.statements.exclude(status='SUPERSEDED').update(status='SUPERSEDED')
    live_bids(tender).update(status=Bid.Status.OPENED)
    tender.evaluations.all().delete()
    tender.status, tender.return_stage = S.OPENED, ''
    tender.save(update_fields=['status', 'return_stage'])
    audit(user, 'REEVALUATE', tender, request=request)


# ------------------------------ award ------------------------------
def render_award_email(award):
    p = award.bid.latest.get_payload()
    v = award.vendor
    ctx = dict(vendor_contact_name=v.contact_name, vendor_company_name=v.company_name, tender=award.tender,
               currency=award.tender.currency, awarded_value=f'{award.awarded_value:,.2f}',
               delivery_period=f"{p.get('delivery_days', '-')} days", offer_validity=f"{p.get('validity_days', '-')} days",
               acceptance_date=(timezone.now() + timedelta(days=7)).strftime('%d %b %Y'),
               po=award.tender.created_by, organization_name=settings.ORG_NAME)
    subject = render_to_string('emails/award_subject.txt', ctx).strip()
    return subject, render_to_string('emails/award_body.txt', ctx)


def send_award_email(award, user, regret=False, request=None):
    tender = award.tender
    if tender.status not in (S.APPROVED, S.AWARDED) or user.role != 'PO':
        raise WorkflowError('Award email can be sent by the PO once the CFO has approved.')
    subject, body = render_award_email(award)
    log = EmailLog.objects.filter(tender=tender, kind='AWARD', recipient=award.vendor.user.email, status='FAILED').first()
    log = deliver_email(award.vendor.user.email, subject, body, tender=tender, kind='AWARD', log=log)
    if log.status == 'SENT':
        award.status, award.sent_at = Award.Status.SENT, timezone.now()
        award.save()
        tender.status = S.AWARDED
        tender.save(update_fields=['status'])
        deliver_email(user.email, f'[Copy] {subject}', body, tender=tender, kind='AWARD_COPY')
        notify([award.vendor.user], 'AWARD', f'Tender {tender.tender_no} has been awarded to you.', f'/tenders/{tender.pk}/')
        audit(user, 'AWARD_EMAIL_SENT', award, new=log.recipient, request=request)
        if regret:
            for b in live_bids(tender).exclude(pk=award.bid_id):
                deliver_email(b.vendor.user.email, f'Update on Tender – {tender.tender_no}',
                              render_to_string('emails/regret.txt', dict(tender=tender, v=b.vendor, organization_name=settings.ORG_NAME)),
                              tender=tender, kind='REGRET')
    else:
        notify([user], 'ALERT', f'Award email for {tender.tender_no} FAILED: {log.error}. Retry from the award page.', f'/tenders/{tender.pk}/award/')
        audit(user, 'AWARD_EMAIL_FAILED', award, new=log.error, request=request)
    return log
