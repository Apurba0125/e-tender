from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, Vendor, VendorDocument


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (('e-Tender', {'fields': ('role', 'phone', 'email_verified', 'failed_attempts', 'locked_until')}),)
    add_fieldsets = UserAdmin.add_fieldsets + (('e-Tender', {'fields': ('email', 'role', 'first_name', 'last_name')}),)
    list_display = ('username', 'email', 'role', 'is_active')
    list_filter = ('role', 'is_active')


admin.site.register(Vendor)
admin.site.register(VendorDocument)
