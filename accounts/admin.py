from django.contrib import admin
from .models import Users, Profile


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False
    verbose_name_plural = 'Профиль'
    fields = ('full_name', 'age', 'rating', 'image', 'bio')


@admin.register(Users)
class MyUserAdmin(admin.ModelAdmin):
    list_display = (
        'username', 'email', 'role',
        'region', 'is_email_verified',
        'is_active', 'date_joined'
    )
    list_filter = (
        'is_volunteer', 'is_client',
        'is_curator', 'is_staff',
        'is_email_verified', 'region'
    )
    search_fields = ('username', 'email')
    readonly_fields = ('date_joined', 'last_login')

    fieldsets = (
        (None, {'fields': ('username', 'password')}),
        ('Персональная информация', {
            'fields': ('email', 'region')
        }),
        ('Роли и Доступ', {
            'fields': (
                'is_volunteer', 'is_client',
                'is_curator', 'is_active',
                'is_staff', 'is_superuser'
            )
        }),
        ('Безопасность', {
            'classes': ('collapse',),
            'fields': (
                'is_email_verified',
                'email_verification_token',
                'reset_password_token'
            )
        }),
        ('Даты', {'fields': ('last_login', 'date_joined')}),
    )

    inlines = (ProfileInline,)

    def get_readonly_fields(self, request, obj=None):
        if obj and obj.is_superuser and not request.user.is_superuser:
            return [f.name for f in self.model._meta.fields]
        return self.readonly_fields


@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'full_name', 'rating', 'age', 'created_at')
    search_fields = ('user__username', 'full_name')
    list_filter = ('created_at',)
    readonly_fields = ('created_at', 'updated_at')