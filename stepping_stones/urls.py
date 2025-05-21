"""stepping_stones URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/3.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path, reverse_lazy
from django.views.generic import RedirectView
from djangoplugins.utils import include_plugins
from django.shortcuts import redirect, reverse
from django.http import HttpResponseRedirect
from event_tracker.models import Task, Operation
from django.conf import settings
from django.db import DEFAULT_DB_ALIAS
import logging

logger = logging.getLogger(__name__)

from event_tracker.plugins import EventReportingPluginPoint, CredentialReportingPluginPoint, \
    EventStreamSourcePluginPoint


def root_view(request):
    logger.info(f"[DEBUG] root_view: Path: {request.path}")
    active_operation_name = request.session.get('active_operation_name')
    logger.info(f"[DEBUG] root_view: active_operation_name from session: {active_operation_name}")

    if active_operation_name:
        try:
            # Verify operation exists in default DB, though this is mainly for integrity.
            # The active_op_db connection is what matters for Task query.
            Operation.objects.using(DEFAULT_DB_ALIAS).get(name=active_operation_name)
            logger.info(f"[DEBUG] root_view: Active operation '{active_operation_name}' confirmed in default DB.")
            
            # Query tasks from the active_op_db
            # OperationMiddleware should have set up the 'active_op_db' connection correctly.
            first_task = Task.objects.using('active_op_db').order_by('pk').first()
            if first_task:
                logger.info(f"[DEBUG] root_view: Found first task (pk={first_task.pk}) in active_op_db. Redirecting to event-list.")
                return HttpResponseRedirect(reverse('event_tracker:event-list', kwargs={"task_id": first_task.pk}))
            else:
                logger.info(f"[DEBUG] root_view: No tasks found in active_op_db for operation '{active_operation_name}'. Redirecting to initial-config-task.")
                return HttpResponseRedirect(reverse('event_tracker:initial-config-task'))
        except Operation.DoesNotExist:
            logger.warning(f"[DEBUG] root_view: Active operation '{active_operation_name}' from session not found in default DB. Clearing session and redirecting to select_operation.")
            request.session.pop('active_operation_name', None)
            return HttpResponseRedirect(reverse('event_tracker:select_operation'))
        except Exception as e:
            logger.error(f"[DEBUG] root_view: Exception occurred: {e}. Redirecting to select_operation.")
            return HttpResponseRedirect(reverse('event_tracker:select_operation'))
    else:
        logger.info("[DEBUG] root_view: No active operation in session. Redirecting to select_operation.")
        return HttpResponseRedirect(reverse('event_tracker:select_operation'))


urlpatterns = [
    path('', root_view),
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path("event-tracker/", include("event_tracker.urls")),
    path('taggit/', include('taggit_bulk.urls')),
    path('plugins/events-reports/', include_plugins(EventReportingPluginPoint)),
    path('plugins/cred-reports/', include_plugins(CredentialReportingPluginPoint)),
    path('plugins/eventstream-sources/', include_plugins(EventStreamSourcePluginPoint)),
]
