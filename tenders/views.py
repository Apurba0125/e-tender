import csv
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import User, Vendor
from . import services as svc
from .decorators import role_required
from .forms import (BidForm, DecisionForm, EvaluationForm, ExtendForm, ForwardForm, NoticeForm, RequisitionForm,
                    TenderDocsForm, TenderForm)
from .models import (Approval, AuditLog, Bid, BidVersion, ComparativeStatement, EmailLog, Notice, Notification, Stage,
                     Requisition, StageDocument, StageForward, Tender, TenderDocument, TenderInvite)
from .services import WorkflowError

S = Tender.Status


# ------------------------------ visibility ------------------------------
def tenders_for(user):
    qs = Tender.objects.all()
    if user.role in ('ADMIN', 'PO'):
        return qs
    if user.role == 'VENDOR':
        v = getattr(user, 'vendor', None)
        if not v or v.status != Vendor.Status.APPROVED:       # blacklisted / suspended see nothing (VEN-04)
            return qs.none()
        return qs.exclude(status__in=[S.DRAFT]).filter(Q(visibility='OPEN') | Q(invites__vendor=v)).distinct()
    if user.role == 'HOD':
        return qs.exclude(status=S.DRAFT)
    stage = {'TC': Stage.TECHNICAL, 'FC': Stage.FINANCE, 'CFO': Stage.CFO}[user.role]
    return qs.filter(forwards__stage=stage).distinct()


def get_tender(request, pk):
    t = get_object_or_404(tenders_for(request.user), pk=pk)       # 404 (not 403) for hidden tenders (T2/T18)
    t.sync_status()
    return t


def fail(request, exc):
    messages.error(request, str(exc))


# ------------------------------ dashboard ------------------------------
@role_required('ADMIN', 'PO', 'HOD', 'TC', 'FC', 'CFO', 'VENDOR')
def dashboard(request):
    u = request.user
    for t in Tender.objects.filter(status__in=[S.PUBLISHED, S.BIDDING_OPEN]):
        t.sync_status()
    ctx = {}
    if u.role == 'VENDOR':
        v = u.vendor
        qs = tenders_for(u)
        my_ids = set(v.bids.exclude(status='WITHDRAWN').values_list('tender_id', flat=True))
        ctx.update(open_tenders=qs.filter(status__in=[S.PUBLISHED, S.BIDDING_OPEN]).exclude(pk__in=my_ids),
                   invited=qs.filter(invites__vendor=v, status__in=[S.PUBLISHED, S.BIDDING_OPEN]),
                   my_bids=v.bids.select_related('tender'),
                   awarded=qs.filter(award__vendor=v),
                   notices=Notice.objects.filter(status='PUBLISHED')[:5], vendor=v)
    elif u.role == 'HOD':
        ctx['requisitions'] = Requisition.objects.filter(requested_by=u)
    elif u.role in ('PO', 'ADMIN'):
        ctx.update(status_counts=Tender.objects.values('status').annotate(n=Count('id')).order_by('status'),
                   deadlines=Tender.objects.filter(status__in=[S.PUBLISHED, S.BIDDING_OPEN]).order_by('bid_end_at')[:8],
                   pending=Tender.objects.filter(status__in=[S.BIDDING_CLOSED, S.OPENED, S.TECHNICAL_APPROVED, S.QUOTATION_VALIDATION,
                                                             S.APPROVED, S.REJECTED, S.SENT_BACK]),
                   pending_vendors=Vendor.objects.filter(status='PENDING').count())
        ctx['pending_requisitions'] = Requisition.objects.filter(status='SUBMITTED')
    else:
        stage = {'TC': Stage.TECHNICAL, 'FC': Stage.FINANCE, 'CFO': Stage.CFO}[u.role]
        waiting = Tender.objects.filter(status=svc.STATUS_FOR_STAGE[stage])
        waiting = [t for t in waiting if not t.approvals.filter(stage=stage, round=t.round, approver=u).exists()]
        ctx.update(waiting=waiting, stage=stage)
    return render(request, 'tenders/dashboard.html', ctx)


# ------------------------------ notices ------------------------------
@role_required('PO', 'ADMIN', 'HOD', 'VENDOR', 'TC', 'FC', 'CFO')
def notice_list(request):
    qs = Notice.objects.all() if request.user.role in ('PO', 'ADMIN') else Notice.objects.filter(status='PUBLISHED')
    return render(request, 'tenders/notice_list.html', {'notices': qs})


# ------------------------------ requisitions ------------------------------
@role_required('HOD', 'PO', 'ADMIN')
def requisition_list(request):
    qs = Requisition.objects.select_related('requested_by', 'reviewed_by', 'tender')
    if request.user.role == 'HOD':
        qs = qs.filter(requested_by=request.user)
    return render(request, 'tenders/requisition_list.html', {'requisitions': qs})


@role_required('HOD')
def requisition_create(request):
    form = RequisitionForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        req = form.save(commit=False)
        req.requested_by = request.user
        req.save()
        svc.audit(request.user, 'REQUISITION_SUBMITTED', req, request=request)
        svc.notify(svc.users_with_role('PO'), 'REQUISITION', f'{req.request_no} needs procurement review.',
                   '/requisitions/', email_subject=f'New requisition – {req.request_no}')
        messages.success(request, f'{req.request_no} submitted to the Purchase Officer.')
        return redirect('requisition_list')
    return render(request, 'tenders/form.html', {'form': form, 'title': 'New purchase requisition'})


@role_required('PO')
@require_POST
def requisition_action(request, pk):
    req = get_object_or_404(Requisition, pk=pk)
    action = request.POST.get('action')
    if action == 'approve' and req.status == Requisition.Status.SUBMITTED:
        req.status, req.reviewed_by, req.reviewed_at = Requisition.Status.APPROVED, request.user, timezone.now()
        req.reason = ''
        req.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'reason'])
        svc.audit(request.user, 'REQUISITION_APPROVED', req, request=request)
        svc.notify([req.requested_by], 'REQUISITION', f'{req.request_no} was approved for tender preparation.',
                   '/requisitions/')
        messages.success(request, f'{req.request_no} approved. It can now generate a tender.')
    elif action == 'reject' and req.status == Requisition.Status.SUBMITTED:
        reason = request.POST.get('reason', '').strip()
        if not reason:
            messages.error(request, 'A reason is required when rejecting a requisition.')
        else:
            req.status, req.reason, req.reviewed_by, req.reviewed_at = Requisition.Status.REJECTED, reason, request.user, timezone.now()
            req.save(update_fields=['status', 'reason', 'reviewed_by', 'reviewed_at'])
            svc.audit(request.user, 'REQUISITION_REJECTED', req, new=reason, request=request)
            svc.notify([req.requested_by], 'REQUISITION', f'{req.request_no} was rejected: {reason}', '/requisitions/')
            messages.success(request, f'{req.request_no} rejected.')
    elif action == 'generate':
        try:
            tender = svc.generate_tender_from_requisition(req, request.user, request)
            messages.success(request, f'{tender.tender_no} generated as a draft. Publish its notice, then the tender.')
            return redirect('tender_edit', pk=tender.pk)
        except svc.WorkflowError as exc:
            fail(request, exc)
    else:
        messages.error(request, 'That requisition action is no longer available.')
    return redirect('requisition_list')


@role_required('PO')
def notice_create(request):
    form = NoticeForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        n = form.save(commit=False)
        n.created_by = request.user
        n.save()
        svc.audit(request.user, 'NOTICE_CREATED', n, request=request)
        messages.success(request, f'Notice {n.notice_no} saved as draft.')
        return redirect('notice_list')
    return render(request, 'tenders/form.html', {'form': form, 'title': 'New notice', 'multipart': True})


@role_required('PO')
@require_POST
def notice_action(request, pk):
    n = get_object_or_404(Notice, pk=pk)
    act = request.POST.get('action')
    try:
        if act == 'publish':
            svc.publish_notice(n, request.user, request)
        elif act == 'archive':
            n.status = 'ARCHIVED'
            n.save()
            svc.audit(request.user, 'NOTICE_ARCHIVED', n, request=request)
    except WorkflowError as e:
        fail(request, e)
    return redirect('notice_list')


# ------------------------------ tenders ------------------------------
@role_required('ADMIN', 'PO', 'HOD', 'TC', 'FC', 'CFO', 'VENDOR')
def tender_list(request):
    qs = tenders_for(request.user)
    for t in qs.filter(status__in=[S.PUBLISHED, S.BIDDING_OPEN]):
        t.sync_status()
    qs = tenders_for(request.user)
    q, st, cat = request.GET.get('q'), request.GET.get('status'), request.GET.get('category')
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(tender_no__icontains=q))
    if st:
        qs = qs.filter(status=st)
    if cat:
        qs = qs.filter(category__icontains=cat)
    if request.GET.get('export') == 'csv' and request.user.role in ('PO', 'ADMIN'):
        return _csv('tenders.csv', ['Tender No', 'Title', 'Category', 'Status', 'Est. value', 'Deadline'],
                    [[t.tender_no, t.title, t.category, t.status, t.estimated_value, t.bid_end_at] for t in qs])
    return render(request, 'tenders/tender_list.html', {'tenders': qs, 'statuses': Tender.Status.choices, 'sel': st, 'q': q or ''})


def _csv(name, header, rows):
    r = HttpResponse(content_type='text/csv')
    r['Content-Disposition'] = f'attachment; filename="{name}"'
    w = csv.writer(r)
    w.writerow(header)
    w.writerows(rows)
    return r


def _save_tender(form, docs, user, request):
    t = form.save(commit=False)
    if not t.pk:
        t.created_by = user
    t.save()
    t.invites.exclude(vendor__in=form.cleaned_data['invited']).delete()
    if t.visibility == 'LIMITED':
        for v in form.cleaned_data['invited']:
            TenderInvite.objects.get_or_create(tender=t, vendor=v)
    else:
        t.invites.all().delete()
    for f in docs:
        TenderDocument.objects.create(tender=t, file=f, uploaded_by=user)
    return t


@role_required('PO')
def tender_create(request):
    form = TenderForm(request.POST or None)
    dform = TenderDocsForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid() and dform.is_valid():
        t = _save_tender(form, dform.cleaned_data['files'], request.user, request)
        svc.audit(request.user, 'TENDER_CREATED', t, request=request)
        if 'publish' in request.POST:
            try:
                svc.publish_tender(t, request.user, request)
                messages.success(request, f'{t.tender_no} published.')
            except WorkflowError as e:
                fail(request, e)
        else:
            messages.success(request, f'{t.tender_no} saved as draft.')
        return redirect('tender_detail', pk=t.pk)
    return render(request, 'tenders/tender_form.html', {'form': form, 'dform': dform, 'title': 'New tender'})


@role_required('PO')
def tender_edit(request, pk):
    t = get_tender(request, pk)
    if t.status != S.DRAFT:
        messages.error(request, 'Published tenders can only change via corrigendum / deadline extension.')
        return redirect('tender_detail', pk=pk)
    form = TenderForm(request.POST or None, instance=t)
    dform = TenderDocsForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid() and dform.is_valid():
        _save_tender(form, dform.cleaned_data['files'], request.user, request)
        svc.audit(request.user, 'TENDER_EDITED', t, request=request)
        if 'publish' in request.POST:
            try:
                svc.publish_tender(t, request.user, request)
            except WorkflowError as e:
                fail(request, e)
        return redirect('tender_detail', pk=t.pk)
    return render(request, 'tenders/tender_form.html', {'form': form, 'dform': dform, 'title': f'Edit {t.tender_no}'})


@role_required('ADMIN', 'PO', 'HOD', 'TC', 'FC', 'CFO', 'VENDOR')
def tender_detail(request, pk):
    t = get_tender(request, pk)
    u = request.user
    ctx = {'t': t, 'extend_form': ExtendForm()}
    if u.role == 'VENDOR':
        TenderInvite.objects.filter(tender=t, vendor=u.vendor, viewed_at__isnull=True).update(viewed_at=timezone.now())
        ctx['my_bid'] = Bid.objects.filter(tender=t, vendor=u.vendor).first()
        ctx['award'] = getattr(t, 'award', None) if getattr(t, 'award', None) and t.award.vendor_id == u.vendor.pk else None
    else:
        ctx['bids'] = svc.live_bids(t) if t.bids_visible else None      # sealed until opened
        ctx['stmt'] = t.statements.exclude(status='SUPERSEDED').first()
        ctx['timeline'] = AuditLog.objects.filter(entity_type='Tender', entity_id=str(t.pk)).order_by('timestamp')
        ctx['forwards'] = t.forwards.prefetch_related('documents')
        ctx['approvals'] = t.approvals.select_related('approver')
    if t.bids_visible and u.role != 'VENDOR':
        rows = []
        for b in ctx['bids']:
            p = b.latest.get_payload()
            rows.append({'bid': b, 'p': p})
        ctx['opening_summary'] = rows
    return render(request, 'tenders/tender_detail.html', ctx)


@role_required('PO')
@require_POST
def tender_action(request, pk):
    t = get_tender(request, pk)
    act = request.POST.get('action')
    try:
        if act == 'publish':
            svc.publish_tender(t, request.user, request)
        elif act == 'cancel':
            svc.cancel_tender(t, request.user, request.POST.get('reason', ''), request)
        elif act == 'extend':
            f = ExtendForm(request.POST)
            if not f.is_valid():
                raise WorkflowError('Provide a valid new deadline and reason.')
            svc.extend_deadline(t, request.user, f.cleaned_data['new_end'], f.cleaned_data['reason'], request)
        elif act == 'corrigendum':
            if not request.POST.get('text', '').strip():
                raise WorkflowError('Corrigendum text is required.')
            svc.add_corrigendum(t, request.user, request.POST['text'], request)
        elif act == 'open':
            svc.open_tender(t, request.user, request)
        elif act == 'reevaluate':
            svc.reevaluate(t, request.user, request)
        elif act == 'retender':
            new = svc.clone_tender(Tender.objects.get(pk=t.pk), request.user, request)
            t.refresh_from_db()
            svc.cancel_tender(t, request.user, 'Re-tendered as ' + new.tender_no, request)
            return redirect('tender_edit', pk=new.pk)
        elif act == 'clone':
            new = svc.clone_tender(Tender.objects.get(pk=t.pk), request.user, request)
            return redirect('tender_edit', pk=new.pk)
        elif act == 'close':
            if t.status != S.AWARDED:
                raise WorkflowError('Only awarded tenders can be closed.')
            t.status = S.CLOSED
            t.save(update_fields=['status'])
            svc.audit(request.user, 'TENDER_CLOSED', t, request=request)
        else:
            raise WorkflowError('Unknown action.')
    except WorkflowError as e:
        fail(request, e)
    return redirect('tender_detail', pk=pk)


# ------------------------------ vendor bidding ------------------------------
@role_required('VENDOR')
def bid_form(request, pk):
    t = get_tender(request, pk)
    v = request.user.vendor
    bid = Bid.objects.filter(tender=t, vendor=v).first()
    initial = {}
    if bid and bid.status != Bid.Status.WITHDRAWN and bid.latest:
        initial = bid.latest.get_payload(as_owner=True)
    form = BidForm(request.POST or None, request.FILES or None, initial=initial, tender=t,
                   has_emd_proof=bool(bid and bid.latest and bid.latest.emd_proof))
    if request.method == 'POST' and form.is_valid():
        d = form.cleaned_data
        try:
            b = svc.submit_bid(t, v, {k: d[k] for k in ('price', 'taxes', 'delivery_days', 'validity_days', 'remarks')},
                               d.get('document'), d.get('emd_proof'), request)
            messages.success(request, f'Bid recorded. Receipt {b.receipt_no}, version {b.current_version}.')
            return redirect('my_bids')
        except WorkflowError as e:
            fail(request, e)
    return render(request, 'tenders/bid_form.html', {'t': t, 'form': form, 'bid': bid})


@role_required('VENDOR')
def my_bids(request):
    bids = request.user.vendor.bids.select_related('tender').prefetch_related('versions')
    for b in bids:
        b.tender.sync_status()
    return render(request, 'tenders/my_bids.html', {'bids': bids})


@role_required('VENDOR')
@require_POST
def bid_withdraw(request, pk):
    b = get_object_or_404(Bid, pk=pk, vendor=request.user.vendor)
    try:
        svc.withdraw_bid(b, request.user, request)
        messages.success(request, 'Bid withdrawn. You may submit again before the deadline.')
    except WorkflowError as e:
        fail(request, e)
    return redirect('my_bids')


# ------------------------------ forward / review ------------------------------
@role_required('PO')
def forward_view(request, pk, stage):
    t = get_tender(request, pk)
    stage = stage.upper()
    if stage not in (Stage.TECHNICAL, Stage.FINANCE, Stage.CFO):
        raise Http404
    form = ForwardForm(request.POST or None, request.FILES or None)
    if request.method == 'POST' and form.is_valid():
        try:
            svc.forward(t, request.user, stage, form.cleaned_data['remarks'], form.cleaned_data['files'])
            messages.success(request, f'Forwarded to {stage.title()} stage.')
            return redirect('tender_detail', pk=pk)
        except WorkflowError as e:
            fail(request, e)
    return render(request, 'tenders/forward.html', {'t': t, 'form': form, 'stage': stage})


@role_required('TC', 'FC')
def review(request, pk):
    t = get_tender(request, pk)
    u = request.user
    stage = Stage.TECHNICAL if u.role == 'TC' else Stage.FINANCE
    active = t.status == svc.STATUS_FOR_STAGE[stage]
    if request.method == 'POST':
        try:
            if request.POST.get('form') == 'evaluate':
                f = EvaluationForm(request.POST)
                if f.is_valid():
                    bid = get_object_or_404(Bid, pk=f.cleaned_data['bid_id'], tender=t)
                    svc.save_evaluation(t, u, bid, f.cleaned_data['qualified'] == '1', f.cleaned_data['score'], f.cleaned_data['remarks'])
                    messages.success(request, 'Evaluation saved.')
            else:
                f = DecisionForm(request.POST)
                if f.is_valid():
                    svc.decide(t, u, stage, f.cleaned_data['decision'], f.cleaned_data['remarks'], request)
                    messages.success(request, 'Decision recorded.')
        except WorkflowError as e:
            fail(request, e)
        return redirect('review', pk=pk)
    rows = []
    bids = svc.live_bids(t)
    if stage == Stage.FINANCE:
        bids = bids.filter(status__in=[Bid.Status.TECH_QUALIFIED, Bid.Status.SHORTLISTED])
    for b in bids:
        rows.append({'bid': b, 'p': b.latest.get_payload(), 'ev': getattr(b, 'evaluation', None)})
    rows.sort(key=lambda r: Decimal(str(r['p']['price'])) + Decimal(str(r['p'].get('taxes') or 0)))
    mine = t.approvals.filter(stage=stage, round=t.round, approver=u).first()
    return render(request, 'tenders/review.html', {
        't': t, 'rows': rows, 'stage': stage, 'active': active and not mine, 'mine': mine,
        'dform': DecisionForm(), 'forward': t.forwards.filter(stage=stage).last(),
        'approvals': t.approvals.filter(stage=stage, round=t.round)})


@role_required('PO', 'FC', 'CFO', 'ADMIN')
def statement_view(request, pk):
    t = get_tender(request, pk)
    stmt = t.statements.exclude(status='SUPERSEDED').first()
    if not stmt:
        messages.error(request, 'No comparative statement yet.')
        return redirect('tender_detail', pk=pk)
    if request.method == 'POST' and request.POST.get('action') == 'validate':
        try:
            svc.validate_statement(t, request.user, request.POST.get('justification', ''), request)
            messages.success(request, 'Statement validated and sent to CFO.')
        except WorkflowError as e:
            fail(request, e)
        return redirect('statement', pk=pk)
    if request.GET.get('export') == 'csv':
        return _csv(f'{t.tender_no.replace("/", "-")}-statement.csv',
                    ['Rank', 'Vendor', 'Price', 'Taxes', 'Total', 'Delivery days', 'Validity days', 'Tech score', 'Deviation % vs L1'],
                    [[i.label, i.bid.vendor.company_name, i.price, i.taxes, i.total, i.delivery_days, i.validity_days, i.tech_score, i.deviation_pct] for i in stmt.items.all()])
    return render(request, 'tenders/statement.html', {'t': t, 'stmt': stmt})


@role_required('CFO')
def cfo_view(request, pk):
    t = get_tender(request, pk)
    stmt = t.statements.filter(status='VALIDATED').first()
    if not stmt:
        raise Http404
    if request.method == 'POST':
        f = DecisionForm(request.POST)
        if f.is_valid():
            item = stmt.items.filter(pk=request.POST.get('item') or 0).first()
            try:
                svc.decide(t, request.user, Stage.CFO, f.cleaned_data['decision'], f.cleaned_data['remarks'], request,
                           selected_item=item, justification=request.POST.get('justification', ''))
                messages.success(request, 'Decision recorded.')
            except WorkflowError as e:
                fail(request, e)
        return redirect('cfo', pk=pk)
    return render(request, 'tenders/cfo.html', {'t': t, 'stmt': stmt, 'dform': DecisionForm(),
                                                 'active': t.status == S.CFO_REVIEW,
                                                 'forward': t.forwards.filter(stage=Stage.CFO).last()})


# ------------------------------ award ------------------------------
@role_required('PO', 'CFO', 'ADMIN')
def award_view(request, pk):
    t = get_tender(request, pk)
    award = getattr(t, 'award', None)
    if not award:
        messages.error(request, 'No approved award yet.')
        return redirect('tender_detail', pk=pk)
    if request.method == 'POST':
        try:
            log = svc.send_award_email(award, request.user, regret=bool(request.POST.get('regret')), request=request)
            (messages.success if log.status == 'SENT' else messages.error)(
                request, 'Award email sent.' if log.status == 'SENT' else f'Email FAILED: {log.error}. You can retry.')
        except WorkflowError as e:
            fail(request, e)
        return redirect('award', pk=pk)
    subject, body = svc.render_award_email(award)
    return render(request, 'tenders/award.html', {'t': t, 'award': award, 'subject': subject, 'body': body,
                                                   'logs': t.emails.filter(kind__in=['AWARD', 'REGRET'])})


@role_required('VENDOR')
@require_POST
def award_accept(request, pk):
    t = get_tender(request, pk)
    a = getattr(t, 'award', None)
    if a and a.vendor_id == request.user.vendor.pk and a.status == 'SENT':
        a.status, a.accepted_at = 'ACCEPTED', timezone.now()
        a.save()
        svc.audit(request.user, 'AWARD_ACCEPTED', a, request=request)
        svc.notify(svc.users_with_role('PO'), 'AWARD', f'{request.user.vendor} accepted award of {t.tender_no}.', f'/tenders/{t.pk}/')
        messages.success(request, 'Award accepted.')
    return redirect('tender_detail', pk=pk)


# ------------------------------ misc ------------------------------
@role_required('ADMIN', 'PO', 'HOD', 'TC', 'FC', 'CFO', 'VENDOR')
def notifications(request):
    qs = Notification.objects.filter(user=request.user)
    items = list(qs[:100])
    qs.filter(read_at__isnull=True).update(read_at=timezone.now())
    return render(request, 'tenders/notifications.html', {'items': items})


@role_required('ADMIN', 'PO', 'CFO')
def reports(request):
    tenders = Tender.objects.all()
    awarded = tenders.filter(award__isnull=False)
    rows = []
    for t in awarded.select_related('award'):
        rows.append({'t': t, 'saving': t.estimated_value - t.award.awarded_value})
    data = {
        'by_status': tenders.values('status').annotate(n=Count('id')).order_by('status'),
        'awarded_value': awarded.aggregate(s=Sum('award__awarded_value'))['s'] or 0,
        'participation': Tender.objects.annotate(nb=Count('bids', filter=~Q(bids__status='WITHDRAWN'))).values('tender_no', 'title', 'nb'),
        'savings': rows,
        'turnaround': [(a.tender.tender_no, a.stage, a.decided_at) for a in Approval.objects.select_related('tender')[:30]],
    }
    if request.GET.get('export') == 'csv':
        return _csv('awards.csv', ['Tender', 'Estimate', 'Awarded', 'Saving'],
                    [[r['t'].tender_no, r['t'].estimated_value, r['t'].award.awarded_value, r['saving']] for r in rows])
    return render(request, 'tenders/reports.html', data)


@role_required('ADMIN', 'PO', 'TC', 'FC', 'CFO')
def audit_log(request):
    qs = AuditLog.objects.all()
    if request.user.role in ('TC', 'FC'):
        qs = qs.filter(Q(actor=request.user))                     # "limited" access
    q = request.GET.get('q')
    if q:
        qs = qs.filter(Q(action__icontains=q) | Q(actor_name__icontains=q) | Q(entity_type__icontains=q))
    if request.GET.get('export') == 'csv' and request.user.role in ('ADMIN', 'PO'):
        return _csv('audit.csv', ['Time', 'Actor', 'Action', 'Entity', 'ID', 'IP', 'Hash'],
                    [[a.timestamp, a.actor_name, a.action, a.entity_type, a.entity_id, a.ip, a.hash] for a in qs])
    return render(request, 'tenders/audit.html', {'logs': qs[:300], 'q': q or ''})


@role_required('ADMIN', 'PO')
def email_log(request):
    if request.method == 'POST':
        log = get_object_or_404(EmailLog, pk=request.POST.get('id'), status='FAILED')
        svc.deliver_email(log.recipient, log.subject, log.body, log=log)
        return redirect('email_log')
    return render(request, 'tenders/email_log.html', {'logs': EmailLog.objects.all()[:200]})


@role_required('ADMIN', 'PO', 'HOD', 'TC', 'FC', 'CFO', 'VENDOR')
def download(request, kind, pk):
    """All files are private and go through a permission check."""
    u = request.user
    if kind == 'tender':
        d = get_object_or_404(TenderDocument, pk=pk)
        get_tender(request, d.tender_id)
        f = d.file
    elif kind == 'notice':
        d = get_object_or_404(Notice, pk=pk)
        if u.role == 'VENDOR' and d.status != 'PUBLISHED':
            raise Http404
        f = d.attachment
    elif kind in ('bid', 'emd'):
        v = get_object_or_404(BidVersion, pk=pk)
        if u.role == 'VENDOR':
            if v.bid.vendor_id != u.vendor.pk:
                raise Http404
        else:
            get_tender(request, v.bid.tender_id)
            if not v.bid.tender.bids_visible:
                raise PermissionDenied('Sealed until opening.')
        f = v.document if kind == 'bid' else v.emd_proof
    elif kind == 'stage':
        d = get_object_or_404(StageDocument, pk=pk)
        if u.role == 'VENDOR':
            raise PermissionDenied
        get_tender(request, d.forward.tender_id)
        f = d.file
    else:
        raise Http404
    if not f:
        raise Http404
    return FileResponse(f.open('rb'), as_attachment=True, filename=f.name.split('/')[-1])
