import os
from django.conf import settings
from django.core.exceptions import ValidationError


def validate_upload(f):
    ext = os.path.splitext(f.name)[1].lstrip('.').lower()
    if ext not in settings.ALLOWED_UPLOAD_EXT:
        raise ValidationError(f'File type .{ext} not allowed. Allowed: {", ".join(settings.ALLOWED_UPLOAD_EXT)}')
    if f.size > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValidationError(f'File too large (max {settings.MAX_UPLOAD_MB} MB).')
