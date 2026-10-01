from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from tenders.decorators import role_required
from tenders.services import audit, deliver_email, notify, users_with_role
from .forms import AdminUserCreateForm, VendorDocForm, VendorProfileForm, VendorRegisterForm
from .models import User, Vendor


def _login(request, portal):
    if request.method == 'POST':
        ident = request.POST.get('username', '').strip()
        pw = request.POST.get('password', '')
        u = User.objects.filter(Q(username__iexact=ident) | Q(email__iexact=ident)).first()
        if u and u.locked_until and u.locked_until > timezone.now():
            messages.error(request, 'Account temporarily locked after repeated failures. Try again later.')
        else:
            user = authenticate(request, username=u.username if u else ident, password=pw)
            portal_ok = user and ((portal == 'vendor') == user.is_vendor)
            if user and portal_ok:
                user.failed_attempts, user.locked_until = 0, None
                user.save(update_fields=['failed_attempts', 'locked_until'])
                login(request, user)
                audit(user, 'LOGIN', user, request=request)
                return redirect(request.GET.get('next') or 'dashboard')
            if u:
                u.failed_attempts += 1
                if u.failed_attempts >= settings.MAX_FAILED_LOGINS:
                    u.locked_until = timezone.now() + timedelta(minutes=settings.LOCKOUT_MINUTES)
                    u.failed_attempts = 0
                u.save(update_fields=['failed_attempts', 'locked_until'])
            audit(None, 'LOGIN_FAILED', None, new=ident, request=request)
            messages.error(request, 'Invalid credentials for this portal.')
    return render(request, 'accounts/login.html', {'portal': portal})


def vendor_login(request):
    return _login(request, 'vendor')


def internal_login(request):
    return _login(request, 'internal')


def do_logout(request):
    logout(request)
    return redirect('internal_login')


@role_required('ADMIN')
def user_admin(request):
    form = AdminUserCreateForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        new_user = form.save()
        audit(request.user, 'USER_CREATED', new_user,
              new={'username': new_user.username, 'role': new_user.role}, request=request)
        messages.success(request, f'User {new_user.username} created.')
        return redirect('user_admin')
    users = User.objects.order_by('role', 'username')
    return render(request, 'accounts/user_admin.html', {'form': form, 'users': users})


def register(request):
    form = VendorRegisterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        v = form.save()
        token = signing.dumps(v.user_id, salt='verify-email')
        deliver_email(v.user.email, 'Verify your email – e-Tender portal',
                      f'Hello {v.contact_name},\n\nPlease verify your email: {settings.SITE_URL}/verify/{token}/\n'
                      'After verification, your registration will be reviewed by the Purchase Officer.')
        notify(users_with_role('PO', 'ADMIN'), 'VENDOR', f'New vendor registration: {v.company_name}', '/vendors/',
               email_subject='New vendor registration')
        messages.success(request, 'Registered! Check your email to verify your address; approval follows.')
        return redirect('vendor_login')
    return render(request, 'accounts/register.html', {'form': form})


def verify_email(request, token):
    try:
        uid = signing.loads(token, salt='verify-email', max_age=3 * 86400)
    except signing.BadSignature:
        messages.error(request, 'Verification link is invalid or expired.')
        return redirect('vendor_login')
    User.objects.filter(pk=uid).update(email_verified=True)
    messages.success(request, 'Email verified. You can log in once the vendor account is approved.')
    return redirect('vendor_login')


@role_required('PO', 'ADMIN')
def vendor_list(request):
    qs = Vendor.objects.select_related('user')
    st = request.GET.get('status')
    if st:
        qs = qs.filter(status=st)
    return render(request, 'accounts/vendor_list.html', {'vendors': qs, 'statuses': Vendor.Status.choices, 'sel': st})


@role_required('PO', 'ADMIN')
def vendor_action(request, pk):
    v = get_object_or_404(Vendor, pk=pk)
    if request.method == 'POST':
        action = request.POST.get('action')
        mapping = {'approve': 'APPROVED', 'reject': 'REJECTED', 'suspend': 'SUSPENDED', 'blacklist': 'BLACKLISTED'}
        if action in mapping:
            old, v.status = v.status, mapping[action]
            v.status_reason = request.POST.get('reason', '')
            v.save()
            audit(request.user, f'VENDOR_{v.status}', v, old=old, new=v.status_reason, request=request)
            if old != v.status:
                if v.status == Vendor.Status.APPROVED:
                    deliver_email(
                        v.user.email,
                        f'Vendor verification completed: {v.company_name}',
                        f'Dear {v.contact_name},\n\n'
                        f'The verification of {v.company_name} is complete. '
                        'Your vendor registration has been approved. You can now access eligible tenders.',
                    )
                else:
                    deliver_email(v.user.email, f'Vendor registration update: {v.get_status_display()}',
                                  f'Dear {v.contact_name}, your vendor status is now: {v.get_status_display()}. {v.status_reason}')
            messages.success(request, f'{v.company_name}: {v.get_status_display()}')
    return redirect('vendor_list')


@role_required('VENDOR')
def profile(request):
    v = request.user.vendor
    form = VendorProfileForm(request.POST or None, instance=v, prefix='p')
    dform = VendorDocForm(request.POST or None, request.FILES or None, prefix='d')
    if request.method == 'POST':
        if 'save_profile' in request.POST and form.is_valid():
            audit(request.user, 'VENDOR_PROFILE_EDIT', v, old={k: str(getattr(Vendor.objects.get(pk=v.pk), k)) for k in form.changed_data}, new={k: str(form.cleaned_data[k]) for k in form.changed_data}, request=request)
            form.save()
            messages.success(request, 'Profile updated.')
            return redirect('profile')
        if 'add_doc' in request.POST and dform.is_valid():
            d = dform.save(commit=False)
            d.vendor = v
            d.save()
            messages.success(request, 'Document uploaded.')
            return redirect('profile')
    return render(request, 'accounts/profile.html', {'form': form, 'dform': dform, 'vendor': v})
