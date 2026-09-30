def globals(request):
    from django.conf import settings
    ctx = {'ORG_NAME': settings.ORG_NAME}
    u = getattr(request, 'user', None)
    if u is not None and u.is_authenticated:
        from .models import Notification
        ctx['unread_count'] = Notification.objects.filter(user=u, read_at__isnull=True).count()
    return ctx
