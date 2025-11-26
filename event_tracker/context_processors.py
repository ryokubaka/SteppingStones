from .models import Operation
from django.db import DEFAULT_DB_ALIAS
from django.utils.safestring import mark_safe

def operations_list_processor(request):
    operations = []
    active_operation_display_name = None
    active_operation_name_session = request.session.get('active_operation_name')

    if request.user.is_authenticated:
        try:
            operations = list(Operation.objects.using(DEFAULT_DB_ALIAS).order_by('display_name'))
            if active_operation_name_session:
                # Get the display name of the active operation
                active_op_obj = next((op for op in operations if op.name == active_operation_name_session), None)
                if active_op_obj:
                    active_operation_display_name = active_op_obj.display_name
        except Exception: # Broad exception if DB isn't ready, etc.
            pass # operations will be empty, no active_op_display_name
            
    return {
        'global_operations_list': operations,
        'active_operation_name_session': active_operation_name_session,
        'active_operation_display_name': active_operation_display_name,
    }


def apply_theme(request):
    return {
        "REPORT_FOOTER_IMAGE": mark_safe('<svg width="1" height="1" xmlns="http://www.w3.org/2000/svg"></svg>'),
        "REPORT_FOOTER_TEXT": "Generated with Stepping Stones"
    } 