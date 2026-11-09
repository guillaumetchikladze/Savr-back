from django.contrib import admin

from .models import Table, TableMember


class TableMemberInline(admin.TabularInline):
    model = TableMember
    extra = 0
    raw_id_fields = ('user', 'invited_by')


@admin.register(Table)
class TableAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'emoji', 'created_by', 'created_at')
    search_fields = ('name',)
    raw_id_fields = ('created_by',)
    inlines = [TableMemberInline]
