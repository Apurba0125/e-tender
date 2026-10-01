from django.contrib import admin
from . import models as m

admin.site.register([m.Notice, m.Requisition, m.Tender, m.TenderInvite, m.TenderDocument, m.Corrigendum, m.Bid, m.Approval,
                     m.TechnicalEvaluation, m.ComparativeStatement, m.StatementItem, m.Award, m.EmailLog, m.Notification])


@admin.register(m.Config)
class ConfigAdmin(admin.ModelAdmin):
    list_display = ('key', 'value', 'help')


@admin.register(m.AuditLog)
class AuditAdmin(admin.ModelAdmin):          # read-only
    list_display = ('timestamp', 'actor_name', 'action', 'entity_type', 'entity_id', 'ip')
    def has_add_permission(self, r): return False
    def has_change_permission(self, r, obj=None): return False
    def has_delete_permission(self, r, obj=None): return False
