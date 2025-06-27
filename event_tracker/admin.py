from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from import_export import resources
from import_export.admin import ImportExportModelAdmin, ImportExportMixin
from reversion.admin import VersionAdmin
from taggit.admin import TagAdmin
from taggit.models import Tag
from taggit_bulk.actions import tag_wizard
from django.utils.translation import gettext_lazy as _

from .models import Task, Context, Event, AttackTactic, AttackTechnique, AttackSubTechnique, File, FileDistribution, \
    UserPreferences, Credential, Webhook, LDAPSettings

# Register your models here.

admin.site.register(Task)


@admin.register(Context)
class ContextAdmin(VersionAdmin):
    list_display = ["host", "user", "process"]


@admin.register(Event)
class EventAdmin(VersionAdmin):
    actions = [
        tag_wizard
    ]


@admin.register(FileDistribution)
class FileDistributionAdmin(VersionAdmin):
    pass


@admin.register(File)
class FileAdmin(VersionAdmin):
    list_display = ["filename", "md5_hash", "size"]

    def __repr__(self):
        return "XXX"


admin.site.register(Credential)

admin.site.register(AttackTactic)
admin.site.register(AttackTechnique)
admin.site.register(AttackSubTechnique)

# Define an inline admin descriptor for Employee model
# which acts a bit like a singleton
class UserPreferencesInline(admin.StackedInline):
    model = UserPreferences
    can_delete = False
    verbose_name_plural = 'preferences'


class UserResource(resources.ModelResource):

    class Meta:
        name = "User"
        model = User


class UserPreferencesResource(resources.ModelResource):
    class Meta:
        name = "User's Preferences"
        model = UserPreferences


# Define a new User admin
class UserAdmin(ImportExportMixin, BaseUserAdmin):
    resource_classes = [UserResource, UserPreferencesResource]
    inlines = (UserPreferencesInline,)

# Re-register UserAdmin
admin.site.unregister(User)
admin.site.register(User, UserAdmin)

# -- Make Tags importable/exportable and add buttons to the admin UI

class TagResource(resources.ModelResource):

    class Meta:
        model = Tag


class MyTagAdmin(ImportExportMixin, TagAdmin):
    resource_classes = [TagResource]

# Re-register TagAdmin
admin.site.unregister(Tag)
admin.site.register(Tag, MyTagAdmin)


# -- Make WebHooks importable/exportable and add buttons to the admin UI
class WebhookResource(resources.ModelResource):

    class Meta:
        model = Webhook


@admin.register(Webhook)
class WebhookAdmin(ImportExportModelAdmin):
    resource_classes = [WebhookResource]
    list_display = ["url"]


@admin.register(LDAPSettings)
class LDAPSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        (_('Basic Settings'), {
            'fields': ('enabled', 'server_uri', 'bind_dn', 'bind_password')
        }),
        (_('User Search'), {
            'fields': ('user_search_base', 'user_search_filter', 'user_attr_map')
        }),
        (_('Group Settings'), {
            'fields': ('group_search_base', 'group_search_filter', 'require_group', 'deny_group', 'user_flags_by_group')
        }),
        (_('Cache Settings'), {
            'fields': ('cache_timeout',)
        }),
        (_('TLS/Connection Options'), {
            'fields': ('tls_require_cert', 'tls_demand', 'tls_cacertfile')
        }),
    )

    def has_add_permission(self, request):
        # Only allow one LDAP settings instance
        if self.model.objects.exists():
            return False
        return super().has_add_permission(request)

    def has_delete_permission(self, request, obj=None):
        # Prevent deletion of LDAP settings
        return False
