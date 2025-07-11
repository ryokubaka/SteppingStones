import csv
import json
import os
import string
import time  # Add time module import
from abc import ABC, abstractmethod
from io import BytesIO
from json import JSONDecodeError

import json2table
import jsonschema
import reversion
from zipfile import ZipFile, ZIP_DEFLATED

from ansi2html import Ansi2HTMLConverter
from dal_select2_taggit.widgets import TaggitSelect2
from django import forms
from django.contrib.auth.decorators import permission_required, login_required
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.mixins import UserPassesTestMixin, PermissionRequiredMixin, LoginRequiredMixin
from django.contrib.auth.models import User, AnonymousUser
from django.contrib.staticfiles import finders
from django.db import transaction, connection, DEFAULT_DB_ALIAS, connections
from django.db.models import Max, Q, Subquery, OuterRef
from django.db.models.functions import Greatest, Coalesce
from django.forms import inlineformset_factory
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, get_object_or_404, redirect
from django.template.defaultfilters import truncatechars_html
from django.utils import timezone, html
from django.utils.dateparse import parse_datetime
from django.utils.http import url_has_allowed_host_and_scheme # CHANGED IMPORT
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.views import View
from django.views.generic import ListView, TemplateView, CreateView, DeleteView, UpdateView, FormView
from django_datatables_view.base_datatable_view import BaseDatatableView
from djangoplugins.models import ENABLED
from jsonschema.exceptions import ValidationError
from neo4j.exceptions import ServiceUnavailable
from reversion.views import RevisionMixin
from taggit.forms import TagField
from taggit.models import Tag

from cobalt_strike_monitor.models import TeamServer, Archive, Beacon, BeaconExclusion, BeaconPresence, \
    Download, CSAction, BeaconLog
from cobalt_strike_monitor.poll_team_server import healthcheck_teamserver
from .models import Task, Event, AttackTactic, AttackTechnique, Context, AttackSubTechnique, FileDistribution, File, \
    EventMapping, Webhook, BeaconReconnectionWatcher, BloodhoundServer, UserPreferences, \
    ImportedEvent, Operation, CurrentOperation
from django.urls import reverse_lazy, reverse
from django.contrib import messages

from dal import autocomplete

from .plugins import EventReportingPluginPoint, EventStreamSourcePluginPoint
from .signals import cs_beacon_to_context, cs_indicator_archive_to_file, notify_webhook_new_beacon, cs_listener_to_context, \
    get_driver_for
from .templatetags.custom_tags import render_ts_local
from .event_detail_suggester.suggester import generate_suggestions
from .views_bloodhound import get_bh_users, get_bh_hosts

from django.conf import settings # For OPS_DATA_DIR

from .forms import OperationForm, ImportOperationForm # Explicit import right before the class

# django-tomselect autocomplete view for Tags
from django_tomselect.autocompletes import AutocompleteModelView # Changed from AutocompleteView
from taggit.models import Tag

import logging # Add this

logger = logging.getLogger(__name__) # Add this

class TagAutocomplete(AutocompleteModelView): # Changed base class
    model = Tag # This is correct for AutocompleteModelView
    search_lookups = ["name__icontains"]

    # get_queryset is often not strictly needed if model and search_lookups are set
    # but it's fine to keep for explicitness or further customization.
    def get_queryset(self):
        # The OperationRouter should handle routing for the Tag model based on its app_label ('taggit')
        # if 'taggit' is in op_specific_apps in db_router.py.
        # For django-tomselect, AutocompleteModelView will use the default manager of self.model.
        # If routing is set up correctly, this should query 'active_op_db'.
        qs = super().get_queryset() # It's good practice to call super().get_queryset()
        if self.q:
            # This custom filtering is redundant if search_lookups is doing its job
            # qs = qs.filter(name__icontains=self.q)
            # However, search_lookups handles this more robustly (e.g. multiple terms)
            pass # Rely on search_lookups defined on the class
        return qs

    # If you need to customize how the JSON response is structured (e.g., value/label fields),
    # you might override get_result_value or get_result_label, but TomSelectConfig
    # in forms.py (value_field='name', label_field='name') should handle this for tags.


@permission_required('event_tracker.view_task')
def index(request):
    tasks = Task.objects.order_by('-start_date')
    context = {'tasks': tasks}
    return render(request, 'index.html', context)


@permission_required('event_tracker.view_attacktechnique')
def techniques_for_tactic(request, tactic):
    tactic = get_object_or_404(AttackTactic, shortname=tactic)
    techniques = AttackTechnique.objects.filter(tactics__exact=tactic)
    result = [{"id":"", "value": "-" * 9}]
    for technique in techniques:
        result.append({"id": technique.id, "value": str(technique)})
    return JsonResponse({"result":result})


@permission_required('event_tracker.view_attacksubtechnique')
def subtechniques_for_technique(request, technique):
    technique = get_object_or_404(AttackTechnique, mitre_id=technique)
    subtechniques = AttackSubTechnique.objects.filter(parent_technique__exact=technique)
    result = [{"id":"", "value": "-" * 9}]
    for subtechnique in subtechniques:
        result.append({"id": subtechnique.id, "value": str(subtechnique)})
    return JsonResponse({"result":result})


@permission_required('event_tracker.admin')
def download_backup(request):
    backup_filename = f"steppingstones-{datetime.now().strftime('%Y%m%d-%H%M%S')}.sqlite3"

    # Defragment the database into a file for export
    with connection.cursor() as cursor:
        cursor.execute(f"vacuum into '{backup_filename}'")

    with open(backup_filename, "rb") as database:
        file_data = database.read()

    os.remove(backup_filename)

    in_memory = BytesIO()
    with ZipFile(in_memory, mode="w", compresslevel=9, compression=ZIP_DEFLATED) as zf:
        # Write database file content to a .zip entry
        zf.writestr(backup_filename, file_data)

    # Go to beginning of the in memory buffer
    in_memory.seek(0)

    return HttpResponse(content=in_memory.read(),
                        headers={'Content-Disposition':
                                f'attachment; filename="steppingstones-{datetime.now().strftime("%Y%m%d-%H%M%S")}.zip"'})


def get_context_queryset():
    return Context.objects\
        .annotate(last_used_source=Max("source__timestamp"), last_used_target=Max("target__timestamp"))\
        .annotate(last_used_of_both=Greatest("last_used_source", "last_used_target"))\
        .annotate(last_used=Coalesce("last_used_of_both","last_used_source","last_used_target"))\
        .order_by("-last_used", "last_used_target")


class ContextAutocomplete(autocomplete.Select2QuerySetView, PermissionRequiredMixin):
    permission_required = 'event_tracker.view_context'

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return Context.objects.none()

        qs = get_context_queryset()
        if self.q:
            qs = qs.filter(process__contains=self.q) | \
                 qs.filter(user__contains=self.q) | \
                 qs.filter(host__contains=self.q)

        return qs


class FileAutocomplete(autocomplete.Select2QuerySetView, PermissionRequiredMixin):
    permission_required = 'event_tracker.view_file'

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return File.objects.none()

        qs = File.objects.all()
        
        if self.q:
            qs = qs.filter(filename__contains=self.q) | \
                 qs.filter(description__contains=self.q)

        return qs


class EventTagAutocomplete(autocomplete.Select2QuerySetView, PermissionRequiredMixin):
    permission_required = 'taggit.view_tag'

    def get_queryset(self):
        if not self.request.user.is_authenticated:
            return Tag.objects.none()

        qs = Tag.objects.all().order_by("name")

        if self.q:
            qs = qs.filter(name__istartswith=self.q)

        return qs


blank_choice = [('', '--- Leave Unchanged ---'),]
class EventBulkEditForm(forms.Form):
    tags = TagField(label="Tag(s) to add", required=False, widget=TaggitSelect2(url='event_tracker:eventtag-autocomplete', attrs={"data-theme": "bootstrap-5"}))
    detected = forms.ChoiceField(label="Set all Detected to", choices=blank_choice + Event.DetectedChoices.choices, initial=None, required=False)
    prevented = forms.ChoiceField(label="Set all Prevented to", choices=blank_choice + Event.PreventedChoices.choices, initial=None, required=False)

class EventBulkEdit(PermissionRequiredMixin, FormView):
    permission_required = 'event_tracker.change_event'
    form_class = EventBulkEditForm
    template_name = "event_tracker/event_bulk_edit.html"
    success_url = "/event-tracker/1"

    def form_valid(self, form):
        for event in Event.objects.filter(starred=True).all():
            event.tags.add(*form.cleaned_data["tags"])
            if form.cleaned_data["detected"]:
                event.detected = form.cleaned_data["detected"]
            if form.cleaned_data["prevented"]:
                event.prevented = form.cleaned_data["prevented"]
            event.save()

        return super().form_valid(form)


class EventListView(PermissionRequiredMixin, ListView):
    permission_required = 'event_tracker.view_event'
    model = Event

    def post(self, request, *args, **kwargs):
        eventfilter = EventFilterForm(request.POST, task_id=kwargs["task_id"])
        if eventfilter.is_valid():
            self.request.session['eventfilter'] = eventfilter.data

        return redirect(request.path)

    def get_queryset(self):
        qs = Event.objects.all()\
            .select_related('mitre_attack_tactic').select_related('mitre_attack_technique').select_related('mitre_attack_subtechnique')\
            .select_related("source").select_related("target")

        eventfilter = EventFilterForm(self.request.session.get('eventfilter'), task_id=self.kwargs["task_id"])

        if eventfilter.is_valid():
            qs = eventfilter.apply_to_queryset(qs)

        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['eventfilter'] = EventFilterForm(self.request.session.get('eventfilter'), task_id=self.kwargs["task_id"])

        context['all_starred'] = not self.get_queryset().filter(starred=False).exists()
        context['contexts'] = Context.objects.filter(source__in=self.get_queryset()).distinct() | Context.objects.filter(target__in=self.get_queryset()).distinct()

        if EventReportingPluginPoint.get_plugins_qs().filter(status=ENABLED).exists():
            context['plugins'] = []
            for plugin in EventReportingPluginPoint.get_plugins():
                if plugin.is_access_permitted(self.request.user):
                    context['plugins'].append(plugin)

        return context


class FileListView(PermissionRequiredMixin, ListView):
    permission_required = 'event_tracker.view_file'

    model = FileDistribution
    template_name = 'event_tracker/file_list.html'


class CSVEventListView(EventListView):
    def render_to_response(self, context, **response_kwargs):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="events.csv"'
        writer = csv.writer(response)
        writer.writerow(["Timestamp", "Timestamp End",
                         "Source Host", "Source User", "Source Process",
                         "Target Host", "Target User", "Target Process",
                         "Description", "Raw Evidence", "Outcome", "Detected", "Prevented",
                         "MITRE Tactic ID", "MITRE Tactic Name",
                         "MITRE Technique ID", "MITRE Technique Name",
                         "MITRE Subtechnique ID", "Mitre Subtechnique Name", "Tags"])

        rows = context.get("event_list").values_list("timestamp", "timestamp_end",
                                                     "source__host", "source__user", "source__process",
                                                     "target__host", "target__user", "target__process",
                                                     "description", "raw_evidence", "outcome", "detected", "prevented",
                                                     "mitre_attack_tactic__mitre_id", "mitre_attack_tactic__name",
                                                     "mitre_attack_technique__mitre_id", "mitre_attack_technique__name",
                                                     "mitre_attack_subtechnique__mitre_id", "mitre_attack_subtechnique__name", "id")

        for event in rows:
            writer.writerow(event[:-1] + (list(Event.objects.get(id=event[-1]).tags.all().values_list("name", flat=True)), ))

        return response


class MitreEventListView(EventListView, ABC):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        # -- Event associated tactics, techniques, subtechniques

        events = self.get_queryset()
        event_tactics = AttackTactic.objects.filter(id__in=events.values_list('mitre_attack_tactic__id'))

        event_tactic_dict = dict()
        for tactic in event_tactics:
            event_tactic_dict[tactic] = dict()
            for technique in tactic.attacktechnique_set.filter(id__in=events.filter(mitre_attack_tactic=tactic).values_list('mitre_attack_technique__id')):
                event_tactic_dict[tactic][technique] = technique.attacksubtechnique_set.filter(id__in=events.filter(mitre_attack_tactic=tactic).filter(mitre_attack_technique=technique).values_list('mitre_attack_subtechnique__id'))

        context["event_tactics"] = event_tactic_dict

        # -- All tactics, techniques, subtechniques published by MITRE

        all_tactics = AttackTactic.objects.all()

        all_tactic_dict = dict()
        for tactic in all_tactics:
            all_tactic_dict[tactic] = dict()
            for technique in tactic.attacktechnique_set.all():
                all_tactic_dict[tactic][technique] = technique.attacksubtechnique_set.all()

        context["all_tactics"] = all_tactic_dict

        return context


class FileForm(forms.ModelForm):
    class Meta:
        model = File
        fields = "__all__"

    def save(self, commit=True):
        """
        Override the standard form save() function to merge this form's data over an existing object if it has some
        key attributes the same.
        """
        existing_instance = File.objects.filter(filename=self.cleaned_data["filename"],
                               size=self.cleaned_data["size"],
                               md5_hash=self.cleaned_data["md5_hash"]).exclude(pk=self.instance.id)

        if existing_instance.exists():
            self.instance = existing_instance.get()

            for data, value in self.cleaned_data.items():
                if value:
                    setattr(self.instance, data, value)

            self.instance.save()
    
            return self.instance
        else:
            return super(FileForm, self).save()


class FileDistributionForm(forms.ModelForm):
    class Meta:
        model = FileDistribution
        fields = "__all__"

    file = forms.ModelChoiceField(queryset=File.objects.all(), required=False, empty_label="New File...",
                                    widget=autocomplete.ModelSelect2(url='event_tracker:file-autocomplete', attrs={
                                        "data-placeholder": "New File...", "data-html": True, "data-theme":"bootstrap-5",
                                        "class": "clonable-dropdown"}))

    def __init__(self, *args, **kwargs):
        # Create a nested form for the file data with a prefix based on the formset entry's prefix for uniqueness
        self.fileform = FileForm(auto_id=kwargs['prefix'] + "_%s", prefix=kwargs['prefix'], use_required_attribute=False)
        super(FileDistributionForm, self).__init__(*args, **kwargs)

    def changed_data(self):
        return self.fileform.changed_data() + super(FileDistributionForm, self).changed_data()

    def clean(self):
        cleaned_data = super().clean()

        parsed_file_form = FileForm(data=self.data, auto_id=self.prefix + "_%s", prefix=self.prefix,
                                    instance=self.cleaned_data["file"])

        # We need all FileDistributionForms in the FileDistributionFormSet to be valid for deletions to be honoured by the underlying library

        if not cleaned_data["DELETE"] and not self.empty_permitted:  # Only validate forms that aren't marked for deletion,
                                                                     # and skip any extra forms based on them being "empty_permitted"
            if (not cleaned_data["file"] and not parsed_file_form.is_valid()):
                self.add_error("file", "Must select an existing file or define a new one.")

        return cleaned_data

    def save(self, commit=True):
        if self.empty_permitted \
                and not self.cleaned_data["file"] \
                and not FileForm(data=self.data, auto_id=self.prefix + "_%s", prefix=self.prefix, instance=self.cleaned_data["file"])\
                          .is_valid():
            # This will skip saving any extra forms which haven't been fully completed.
            # It's preferable to halting the whole form from being submitted
            return

        parsed_file_form = FileForm(data=self.data, auto_id=self.prefix + "_%s", prefix=self.prefix,
                                    instance=self.cleaned_data["file"])

        with transaction.atomic():
            has_data_to_store = False

            # validate the form to populate the cleaned_data attribute, so we can look for meaningful data to store
            if parsed_file_form.is_valid():
                for field in parsed_file_form.changed_data:
                    if parsed_file_form.cleaned_data[field] is not None:
                        has_data_to_store = True
                        break

            if has_data_to_store:
                fileobj = parsed_file_form.save()

                # There's a chance the FileForm.save() returned a different, pre-existing File so explicitly (re)set it.
                self.instance.file = fileobj

            super(FileDistributionForm, self).save(commit=commit)


class UserListAutocomplete(autocomplete.Select2ListView):
    def get_list(self):
        if not self.request.user.has_perm('event_tracker.change_context'):
            return []

        result = set(Context.objects.filter(user__contains=self.q).values_list('user', flat=True).order_by('user').distinct())

        for bloodhound_server in BloodhoundServer.objects.filter(active=True).all():
            driver = get_driver_for(bloodhound_server)

            if driver:
                try:
                    with driver.session() as session:
                        result = result.union(session.execute_read(get_bh_users, self.q))
                except ServiceUnavailable:
                    print("Timeout talking to neo4j for user list autocomplete")

        result = sorted(result, key=lambda s: escape(s.lower()))

        return result


class HostListAutocomplete(autocomplete.Select2ListView):
    def get_list(self):
        if not self.request.user.has_perm('event_tracker.change_context'):
            return []

        result = set(Context.objects.filter(host__contains=self.q).values_list('host', flat=True).order_by('host').distinct())

        for bloodhound_server in BloodhoundServer.objects.filter(active=True).all():
            driver = get_driver_for(bloodhound_server)

            if driver:
                try:
                    with driver.session() as session:
                        result = result.union(session.execute_read(get_bh_hosts, self.q))
                except ServiceUnavailable:
                    print("Timeout talking to neo4j for host list autocomplete")

        result = sorted(result, key=lambda s: escape(s.lower()))

        return result


class ProcessListAutocomplete(autocomplete.Select2ListView):
    def get_list(self):
        if not self.request.user.has_perm('event_tracker.change_context'):
            return []

        result = list(Context.objects.filter(process__contains=self.q).values_list('process', flat=True)
                       .order_by('process').distinct())

        result = sorted(result, key=lambda s: escape(s.lower()))

        return result


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        exclude = ('starred',)

    task = forms.ModelChoiceField(Task.objects)
    timestamp = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    timestamp_end = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), required=False)
    operator = forms.ModelChoiceField(User.objects)
    mitre_attack_tactic = forms.ModelChoiceField(AttackTactic.objects, required=False, label="Tactic", widget=forms.Select(attrs={"hx-post": reverse_lazy("event_tracker:event-field-suggestions"), "hx-target": "#mitre-attack-suggestions", "hx-trigger": "input delay:200ms"}))
    mitre_attack_technique = forms.ModelChoiceField(AttackTechnique.objects, required=False, label="Technique", widget=forms.Select(attrs={"hx-post": reverse_lazy("event_tracker:event-field-suggestions"), "hx-target": "#mitre-attack-suggestions", "hx-trigger": "input delay:200ms"}))
    mitre_attack_subtechnique = forms.ModelChoiceField(AttackSubTechnique.objects, required=False, label="Subtechnique", widget=forms.Select(attrs={"hx-post": reverse_lazy("event_tracker:event-field-suggestions"), "hx-target": "#mitre-attack-suggestions", "hx-trigger": "input delay:200ms"}))
    source = forms.ModelChoiceField(get_context_queryset(), required=False, empty_label="New Source...", widget=autocomplete.ModelSelect2(url='event_tracker:context-autocomplete', attrs={"data-placeholder": "New Source...", "data-html": True, "data-theme":"bootstrap-5", "class": "clonable-dropdown"}))
    target = forms.ModelChoiceField(get_context_queryset(), required=False, empty_label="New Target...", widget=autocomplete.ModelSelect2(url='event_tracker:context-autocomplete', attrs={"data-placeholder": "New Target...", "data-html": True, "data-theme":"bootstrap-5", "class": "clonable-dropdown"}))
    description = forms.CharField(widget=forms.Textarea(attrs={"hx-post": reverse_lazy("event_tracker:event-field-suggestions"), "hx-target": "#mitre-attack-suggestions", "hx-trigger": "input delay:200ms"}))
    raw_evidence = forms.CharField(label="Raw Evidence", required=False, widget=forms.Textarea(attrs={'class': 'font-monospace', "spellcheck": "false", "hx-post": reverse_lazy("event_tracker:event-field-suggestions"), "hx-target": "#mitre-attack-suggestions", "hx-trigger": "input delay:500ms, load"}))
    source_user = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:user-list-autocomplete', attrs={'class': 'context-field user-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null", "data-language": "ss"}))
    source_host = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:host-list-autocomplete', attrs={'class': 'context-field host-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null", "data-language": "ss"}))
    source_process = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:process-list-autocomplete', attrs={'class': 'context-field process-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null", "data-language": "ss"}))
    target_user = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:user-list-autocomplete', attrs={'class': 'context-field user-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null", "data-language": "ss"}))
    target_host = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:host-list-autocomplete', attrs={'class': 'context-field host-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null", "data-language": "ss"}))
    target_process = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:process-list-autocomplete', attrs={'class': 'context-field process-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null"}))

    tags = TagField(required=False, widget=TaggitSelect2(url='event_tracker:eventtag-autocomplete', attrs={"data-theme": "bootstrap-5"}))

    def clean(self):
        cleaned_data = super().clean()
        if (not cleaned_data["source"] and
                not cleaned_data["source_user"] and
                not cleaned_data["source_host"] and
                not cleaned_data["source_process"]):
            self.add_error("source", "Must select an existing source or specify a new one.")

        if (not cleaned_data["target"] and
                not cleaned_data["target_user"] and
                not cleaned_data["target_host"] and
                not cleaned_data["target_process"]):
            self.add_error("target", "Must select an existing target or specify a new one.")

        if not cleaned_data["timestamp_end"]:
            cleaned_data["timestamp_end"] = None

        return cleaned_data

    def save(self, commit=True):
        # Create a source
        if not self.cleaned_data["source"]:
            obj, created = Context.objects.get_or_create(host=self.cleaned_data["source_host"],
                                                         user=self.cleaned_data["source_user"],
                                                         process=self.cleaned_data["source_process"],)

            self.instance.source = obj
        # Update a source
        elif self.cleaned_data["source_host"] \
                or self.cleaned_data["source_user"] \
                or self.cleaned_data["source_process"]:
            source_to_mod = self.cleaned_data["source"]
            source_to_mod.host = self.cleaned_data["source_host"]
            source_to_mod.user = self.cleaned_data["source_user"]
            source_to_mod.process = self.cleaned_data["source_process"]
            source_to_mod.save()

        # Create a target
        if not self.cleaned_data["target"]:
            obj, created = Context.objects.get_or_create(user=self.cleaned_data["target_user"],
                                                         host=self.cleaned_data["target_host"],
                                                         process=self.cleaned_data["target_process"],)

            self.instance.target = obj
        # Update a target
        elif self.cleaned_data["target_host"] \
                or self.cleaned_data["target_user"] \
                or self.cleaned_data["target_process"]:
            target_to_mod = self.cleaned_data["target"]
            target_to_mod.host = self.cleaned_data["target_host"]
            target_to_mod.user = self.cleaned_data["target_user"]
            target_to_mod.process = self.cleaned_data["target_process"]
            target_to_mod.save()

        return super().save(commit=commit)


class LimitedEventForm(EventForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        for field_name, field in self.fields.items():
            if field_name not in ["outcome", "detected"]:
                field.disabled = True

        del self.fields["tags"]


class EventFilterForm(forms.Form):
    tactic = forms.ModelChoiceField(AttackTactic.objects,
                                    required=False,
                                    empty_label="All Tactics",
                                    widget=forms.Select(attrs={'class': 'form-select form-select-sm submit-on-change'}))
    starred = forms.BooleanField(required=False, widget=forms.CheckboxInput(attrs={'class': 'submit-on-change'}))
    tag = forms.ModelChoiceField(Event.tags.get_queryset().order_by("name"), required=False, empty_label="All Tags",
                                 widget=forms.Select(attrs={'class': 'form-select form-select-sm submit-on-change'}))

    class Media:
        js = ["scripts/ss-forms.js"]

    def __init__(self, *args, **kwargs):
        task_id = kwargs.pop("task_id", None)
        super().__init__(*args, **kwargs)

        if task_id:
            self.task = get_object_or_404(Task, id=task_id)

        if self.is_valid():
            qs = self.apply_to_queryset(Event.objects.all())

            # Disable widgets if there is no sane choice
            self.fields['tag'].disabled = not self.fields['tag'].queryset.exists()
            self.fields['starred'].disabled = not qs.filter(starred=True).exists()
            self.fields['tactic'].disabled = not self.fields['tactic'].queryset.exists()


    def get_tag_string(self):
        if 'tag' in self.data and self.data['tag']:
            return self.fields['tag'].choices.queryset.get(pk=self.data['tag']).name
        else:
            return ''

    def apply_to_queryset(self, qs):
        qs = qs.filter(task=self.task)

        tactic = self.cleaned_data['tactic']
        if tactic:
            qs = qs.filter(mitre_attack_tactic=tactic)

        tag = self.cleaned_data['tag']
        if tag:
            qs = qs.filter(tags__name=tag)

        if self.cleaned_data['starred']:
            qs = qs.filter(starred=True)

        return qs


FileDistributionFormSet = inlineformset_factory(Event, FileDistribution, form=FileDistributionForm, exclude=[], extra=1, can_delete=True)


class EventCreateView(PermissionRequiredMixin, RevisionMixin, CreateView):
    permission_required = 'event_tracker.add_event'
    model = Event
    form_class = EventForm

    def get_success_url(self):
        if "task_id" in self.kwargs:
            task_id = self.kwargs["task_id"]
        else:
            task_id = Task.objects.order_by("-id").first().id

        return reverse_lazy('event_tracker:event-list',
                            kwargs={"task_id": task_id})

    def get_initial(self):
        task = get_object_or_404(Task, pk=self.kwargs.get('task_id'))
        return {
            "task": task,
            "timestamp": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "operator": self.request.user,
        }

    def get_context_data(self, **kwargs):
        context = super(EventCreateView, self).get_context_data(**kwargs)
        context['action'] = "Create"

        context['contexts'] = Context.objects.all()

        if self.request.POST:
            context["file_distributions_formset"] = FileDistributionFormSet(self.request.POST)
        else:
            context["file_distributions_formset"] = FileDistributionFormSet()
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        file_distributions_formset = context["file_distributions_formset"]

        with reversion.create_revision(atomic=True):
            self.object = form.save()

            # Call "is_valid()" to populate the cleaned_data, we don't care if the formset is 
            # invalid, as we're only going to save valid forms within the formset
            file_distributions_formset.is_valid()

            file_distributions_formset.instance = self.object
            file_distributions_formset.save()

        return super(EventCreateView, self).form_valid(form)


class EventCloneView(EventCreateView):
    def get_initial(self):
        task = get_object_or_404(Task, pk=self.kwargs.get('task_id'))
        original_event = get_object_or_404(Event, pk=self.kwargs.get('event_id'))

        return {
            "task": task,
            "timestamp": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "operator": original_event.operator,

            "mitre_attack_tactic": original_event.mitre_attack_tactic,
            "mitre_attack_technique": original_event.mitre_attack_technique,
            "mitre_attack_subtechnique": original_event.mitre_attack_subtechnique,

            "source": original_event.source,
            "target": original_event.target,

            "tags": ",".join(original_event.tags.names()),

            "description": original_event.description,
            "raw_evidence": original_event.raw_evidence,

            "detected": original_event.detected,
            "prevented": original_event.prevented,

            "outcome": original_event.outcome
        }

    def get_context_data(self, **kwargs):
        context = super(EventCloneView, self).get_context_data(**kwargs)

        if self.request.POST:
            context["file_distributions_formset"] = FileDistributionFormSet(self.request.POST)
        else:
            original_event = get_object_or_404(Event, pk=self.kwargs.get('event_id'))

            initial = []

            for filedistribution in original_event.filedistribution_set.all():
                initial.append({"location": filedistribution.location,
                                "file": filedistribution.file,
                                "removed": filedistribution.removed})

            context["file_distributions_formset"] = FileDistributionFormSet(instance=self.object, initial=initial)
            context["file_distributions_formset"].extra += len(initial)

        return context


class EventLatMoveCloneView(EventCreateView):
    def get_initial(self):
        task = get_object_or_404(Task, pk=self.kwargs.get('task_id'))
        original_event = get_object_or_404(Event, pk=self.kwargs.get('event_id'))

        return {
            "task": task,
            "timestamp": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "operator": original_event.operator,

            "source": original_event.target,
        }


class EventUpdateView(PermissionRequiredMixin, RevisionMixin, UpdateView):
    permission_required = 'event_tracker.change_event'
    model = Event
    form_class = EventForm

    def get_success_url(self):
        return reverse_lazy('event_tracker:event-list',
                            kwargs={"task_id": self.kwargs["task_id"]})

    def get_initial(self):
        initial = {"timestamp": timezone.localtime(self.object.timestamp).strftime("%Y-%m-%dT%H:%M")}
        if self.object.timestamp_end:
            initial["timestamp_end"] = timezone.localtime(self.object.timestamp_end).strftime("%Y-%m-%dT%H:%M")
        return initial

    def get_context_data(self, **kwargs):
        context = super(EventUpdateView, self).get_context_data(**kwargs)
        context['action'] = "Update"

        context['contexts'] = Context.objects.all()

        if self.request.POST:
            context["file_distributions_formset"] = FileDistributionFormSet(self.request.POST, instance=self.object)
        else:
            context["file_distributions_formset"] = FileDistributionFormSet(instance=self.object)
        return context

    def form_valid(self, form):
        context = self.get_context_data()
        file_distributions_formset = context["file_distributions_formset"]

        with reversion.create_revision(atomic=True):
            self.object = form.save()
            
            # Call "is_valid()" to populate the cleaned_data, we don't care if the formset is 
            # invalid, as we're only going to save valid forms within the formset
            file_distributions_formset.is_valid()

            file_distributions_formset.instance = self.object
            file_distributions_formset.save()

        return super(EventUpdateView, self).form_valid(form)


class LimitedEventUpdateView(EventUpdateView):
    permission_required = 'event_tracker.change_event_limited'
    model = Event
    form_class = LimitedEventForm
    template_name = "event_tracker/event_form_limited.html"


class EventDeleteView(PermissionRequiredMixin, DeleteView):
    permission_required = 'event_tracker.delete_event'
    model = Event

    def get_success_url(self):
        return reverse_lazy('event_tracker:event-list',
                            kwargs={"task_id": self.kwargs["task_id"]})


# --- Team Server Views ---
class TeamServerListView(PermissionRequiredMixin, ListView):
    permission_required = 'cobalt_strike_monitor.view_teamserver'
    model = TeamServer
    ordering = ['description']


class TeamServerConfigView(TeamServerListView):
    template_name = "cobalt_strike_monitor/teamserver_config.html"


class TeamServerCreateView(PermissionRequiredMixin, CreateView):
    permission_required = 'cobalt_strike_monitor.add_teamserver'
    model = TeamServer
    fields = ['description', 'hostname', 'port', 'password', 'active']

    def get_success_url(self):
        return reverse_lazy('event_tracker:team-server-list')

    def get_context_data(self, **kwargs):
        context = super(TeamServerCreateView, self).get_context_data(**kwargs)
        context['action'] = "Create"
        return context


class TeamServerUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = 'cobalt_strike_monitor.change_teamserver'
    model = TeamServer
    fields = ['description', 'hostname', 'port', 'password', 'active']

    def get_success_url(self):
        return reverse_lazy('event_tracker:team-server-list')

    def get_context_data(self, **kwargs):
        context = super(TeamServerUpdateView, self).get_context_data(**kwargs)
        context['action'] = "Update"
        return context


class TeamServerDeleteView(PermissionRequiredMixin, DeleteView):
    permission_required = 'cobalt_strike_monitor.delete_teamserver'
    model = TeamServer

    def get_success_url(self):
        return reverse_lazy('event_tracker:team-server-list')


class TeamServerHealthCheckView(TemplateView):
    template_name = "cobalt_strike_monitor/teamserver_healthcheck.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        tcp_error, aggressor_output, ssbot_status, found_jvm = healthcheck_teamserver(kwargs["serverid"])
        context["tcp_error"] = tcp_error
        context["found_jvm"] = found_jvm

        if aggressor_output:
            conv = Ansi2HTMLConverter()
            context["aggressor_output"] = mark_safe(conv.convert(aggressor_output, full=False))

        if ssbot_status:
            conv = Ansi2HTMLConverter()
            context["ssbot_status"] = mark_safe(conv.convert(ssbot_status, full=False))
        return context


# -- CS Action views
class CSActionListView(PermissionRequiredMixin, TemplateView):
    permission_required = 'cobalt_strike_monitor.view_archive'
    template_name = "cobalt_strike_monitor/cs_action_list.html"


class GlobalSearchView(PermissionRequiredMixin, TemplateView):
    permission_required = 'cobalt_strike_monitor.view_archive'
    template_name = "cobalt_strike_monitor/global_search.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # Get all available operations
        from .models import Operation
        from django.db import DEFAULT_DB_ALIAS
        operations = Operation.objects.using(DEFAULT_DB_ALIAS).order_by('display_name')
        context['operations'] = operations
        return context


class GlobalSearchJSONView(PermissionRequiredMixin, View):
    permission_required = 'cobalt_strike_monitor.view_archive'
    
    def get(self, request, *args, **kwargs):
        from .models import Operation
        from django.db import DEFAULT_DB_ALIAS
        from django.http import JsonResponse
        from django.db import connections
        from django.conf import settings
        import json
        
        # --- JUMP TO ID SUPPORT ---
        page_for_id = request.GET.get('page_for_id')
        operation_name = request.GET.get('operation')
        if page_for_id and operation_name:
            # This is a jump-to-ID request for a specific operation
            try:
                operation = Operation.objects.using(DEFAULT_DB_ALIAS).get(name=operation_name)
                db_path = settings.OPS_DATA_DIR / f"{operation.name}.sqlite3"
                if not db_path.exists():
                    return JsonResponse({'found': False, 'error': 'Database file not found'})
                temp_db_name = f'temp_{operation_name}_{id(db_path)}'
                connections.databases[temp_db_name] = connections.databases['active_op_db'].copy()
                connections.databases[temp_db_name]['NAME'] = str(db_path)
                try:
                    from cobalt_strike_monitor.models import CSAction
                    queryset = CSAction.objects.using(temp_db_name).all().order_by('-start')
                    try:
                        target_id = int(page_for_id)
                    except ValueError:
                        return JsonResponse({'found': False, 'error': 'Invalid ID'})
                    # Find the record
                    target_record = queryset.filter(id=target_id).first()
                    all_ids = list(queryset.values_list('id', flat=True).order_by('id'))
                    if not target_record:
                        return JsonResponse({'found': False, 'error': 'ID not found', 'debug': {'all_ids': all_ids, 'target_id': target_id}})
                    # Count records with newer timestamps
                    records_before = queryset.filter(start__gt=target_record.start).count()
                    try:
                        page_length = int(request.GET.get('length', 10))
                    except (ValueError, TypeError):
                        page_length = 10
                    page_number = (records_before // page_length) + 1
                    debug_info = {
                        'all_ids': all_ids,
                        'target_id': target_id,
                        'target_timestamp': str(target_record.start),
                        'records_before': records_before,
                        'page_length': page_length,
                        'page_number': page_number,
                        'total_records': queryset.count(),
                    }
                    return JsonResponse({'found': True, 'page': page_number, 'debug': debug_info})
                finally:
                    if temp_db_name in connections.databases:
                        del connections.databases[temp_db_name]
            except Exception as e:
                return JsonResponse({'found': False, 'error': str(e)})
        # --- END JUMP TO ID ---
        
        # Get selected operations from request
        selected_operations = request.GET.getlist('operations[]')
        global_search = request.GET.get('global_search', '')
        
        if not selected_operations:
            return JsonResponse({'data': []})
        
        results = {}
        
        for operation_name in selected_operations:
            try:
                # Get operation details
                operation = Operation.objects.using(DEFAULT_DB_ALIAS).get(name=operation_name)
                
                # Create a temporary database connection for this operation
                db_path = settings.OPS_DATA_DIR / f"{operation.name}.sqlite3"
                logger.info(f"Database path: {db_path}")
                
                # Check if the database file exists
                if not db_path.exists():
                    logger.warning(f"Database file not found: {db_path}")
                    results[operation_name] = {
                        'display_name': operation.display_name,
                        'data': [],
                        'error': 'Database file not found'
                    }
                    continue
                
                # Create a temporary connection to this operation's database
                temp_db_name = f'temp_{operation_name}_{id(db_path)}'  # Make unique
                connections.databases[temp_db_name] = connections.databases['active_op_db'].copy()
                connections.databases[temp_db_name]['NAME'] = str(db_path)
                
                logger.info(f"Created temporary database connection: {temp_db_name}")
                
                try:
                    # Query CSAction data for this operation using Django ORM
                    from cobalt_strike_monitor.models import CSAction, Beacon, BeaconLog, Archive, Listener
                    
                    # Build the query with explicit database usage
                    queryset = CSAction.objects.using(temp_db_name).select_related(
                        'beacon__listener'
                    ).prefetch_related(
                        'beaconlog_set',
                        'archive_set'
                    )
                    
                    logger.info(f"Built queryset for operation {operation_name}")
                    
                    # Apply global search filter if provided
                    if global_search:
                        queryset = queryset.filter(
                            Q(beacon__computer__icontains=global_search) |
                            Q(beacon__user__icontains=global_search) |
                            Q(beacon__process__icontains=global_search) |
                            Q(beaconlog__data__icontains=global_search) |
                            Q(archive__data__icontains=global_search) |
                            Q(beaconlog__operator__icontains=global_search) |
                            Q(archive__tactic__icontains=global_search)
                        ).distinct()
                        logger.info(f"Applied search filter for operation {operation_name}")
                    
                    # Order by start time (no limit - let DataTables handle pagination)
                    queryset = queryset.order_by('-start')
                    
                    # Format the results
                    formatted_rows = []
                    for cs_action in queryset:
                        # Get operator from beacon logs using the same database
                        operator = cs_action.beaconlog_set.using(temp_db_name).filter(operator__isnull=False).first()
                        operator_name = operator.operator if operator else None
                        
                        # Get tactic from archive using the same database
                        tactic_archive = cs_action.archive_set.using(temp_db_name).filter(tactic__isnull=False).first()
                        tactic = tactic_archive.tactic if tactic_archive else None
                        
                        # Build source info
                        source = "-"
                        if cs_action.beacon and cs_action.beacon.listener:
                            source = cs_action.beacon.listener.althost or cs_action.beacon.listener.host or "-"
                        
                        # Build target info
                        target_parts = []
                        if cs_action.beacon:
                            if cs_action.beacon.computer:
                                target_parts.append(f"Computer: {cs_action.beacon.computer}")
                            if cs_action.beacon.user:
                                target_parts.append(f"User: {cs_action.beacon.user}")
                            if cs_action.beacon.process:
                                target_parts.append(f"Process: {cs_action.beacon.process} (PID: {cs_action.beacon.pid})")
                        target = "; ".join(target_parts) if target_parts else "-"
                        
                        # Build description like the existing CSActionListJSON view
                        formatted_description = ""
                        
                        # Get description (task data from archive)
                        rowdescription = ", ".join(cs_action.archive_set.using(temp_db_name).filter(type="task").exclude(data="").values_list('data', flat=True))
                        if rowdescription:
                            formatted_description = f"<div class='description'>{html.escape(rowdescription)}</div>"

                        # Get input (input data from archive)
                        rowinput = chr(10).join(cs_action.archive_set.using(temp_db_name).filter(type="input").exclude(data="").values_list('data', flat=True))
                        if rowinput:
                            formatted_description += f"<div class='input'>{html.escape(rowinput)}</div>"

                        # Get output (output and error data from beaconlog)
                        rowoutput = chr(10).join(cs_action.beaconlog_set.using(temp_db_name)
                                                .filter(Q(type__startswith="output") | Q(type="error")).exclude(data="")
                                                .values_list('data', flat=True)).rstrip("\n")
                        if rowoutput:
                            formatted_description += f"<div class='output'>{html.escape(rowoutput)}</div>"

                        if not formatted_description:
                            formatted_description = "-"
                        
                        formatted_rows.append({
                            'id': cs_action.id,
                            'start': cs_action.start.isoformat() if cs_action.start else None,
                            'operator': operator_name or "-",
                            'source': source,
                            'target': target,
                            'description': formatted_description,
                            'tactic': tactic or "-",
                            'operation': operation.display_name
                        })
                    
                    logger.info(f"Formatted {len(formatted_rows)} rows for operation {operation_name}")
                    
                    # Add some sample data for debugging
                    if formatted_rows:
                        sample_row = formatted_rows[0]
                        logger.info(f"Sample row for {operation_name}: source={sample_row['source']}, target={sample_row['target'][:50]}...")
                    
                    results[operation_name] = {
                        'display_name': operation.display_name,
                        'data': formatted_rows
                    }
                    
                finally:
                    # Clean up the temporary connection
                    if temp_db_name in connections.databases:
                        del connections.databases[temp_db_name]
                        logger.info(f"Cleaned up temporary database connection: {temp_db_name}")
                    
            except Exception as e:
                # Log error and continue with other operations
                logger.error(f"Error querying operation {operation_name}: {e}", exc_info=True)
                results[operation_name] = {
                    'display_name': operation.display_name if 'operation' in locals() else operation_name,
                    'data': [],
                    'error': str(e)
                }
                continue
        
        logger.info(f"Returning results for {len(results)} operations")
        return JsonResponse({'data': results})


class FilterableDatatableView(ABC, BaseDatatableView):
    filter_column_mapping = {}

    def filter_search_builder(self):
        q = None

        criteria = 0
        while f'searchBuilder[criteria][{criteria}][data]' in self.request.GET.dict().keys():
            prefix = f'searchBuilder[criteria][{criteria}]'
            criteria += 1

            column = self.request.GET.get(prefix + "[data]")
            condition = self.request.GET.get(prefix + "[condition]")
            value1 = self.request.GET.get(prefix + "[value1]")
            value2 = self.request.GET.get(prefix + "[value2]", None)

            if not column or not condition or not value1:
                continue

            if column in self.filter_column_mapping:
                query_column = self.filter_column_mapping[column]
                value1 = timezone.make_aware(parse_datetime(value1))
                if value2:
                    value2 = timezone.make_aware(parse_datetime(value2))
            else:
                query_column = "unknown_column"

            multivalue = False
            if condition == "<":
                query_condition = "lte"
            elif condition == ">":
                query_condition = "gte"
            elif condition == "between":
                if value1 and value2:
                    query_condition = "range"
                    multivalue = True
                elif not value2:
                    # Handle the case that user has only partially completed the between form
                    query_condition = "gte"
            else:
                query_condition = "unknown_condition"

            kwarg = dict()
            key = f'{query_column}__{query_condition}'

            if multivalue:
                kwarg[key] = [value1, value2]
            else:
                kwarg[key] = value1

            if q is None:
                q = Q(**kwarg)
            else:
                if self.request.GET.get('searchBuilder[logic]') == 'AND':
                    q &= Q(**kwarg)
                elif self.request.GET.get('searchBuilder[logic]') == 'OR':
                    q |= Q(**kwarg)

        return q

    def filter_queryset(self, qs):
        # Handle SearchBuilder params
        search_builder_q = self.filter_search_builder()
        if search_builder_q is not None:
            qs = qs.filter(search_builder_q)

        # Handle free text search params
        search = self.request.GET.get('search[value]', None)
        if search:
            terms = search.split(" ")
            for term in terms:
                qs = self.filter_queryset_by_searchterm(qs, term)

        return qs

    @abstractmethod
    def filter_queryset_by_searchterm(self, qs, terms):
        pass


class CSActionListJSON(PermissionRequiredMixin, FilterableDatatableView):
    permission_required = 'cobalt_strike_monitor.view_archive'
    model = CSAction
    columns = ['id', 'start', 'operator', 'source', 'target', 'data', 'tactic', '']
    order_columns = ['id', 'start', 'operator_anno', '', '', '', 'tactic_anno', '']
    filter_column_mapping = {'Timestamp': 'start'}


    def get_initial_queryset(self):
        operator_subquery = BeaconLog.objects.filter(cs_action__id=OuterRef('pk'), operator__isnull=False)
        tactic_subquery = Archive.objects.filter(cs_action__id=OuterRef('pk'), tactic__isnull=False)

        qs = (self.model.objects
                .filter(beacon__in=Beacon.visible_beacons())
                .annotate(operator_anno=Subquery(operator_subquery.values("operator")[:1]))
                .annotate(tactic_anno=Subquery(tactic_subquery.values("tactic")[:1])))
                # .distinct())  # Temporarily remove distinct to see if it's causing issues
        
        # Debug: Check what we're getting
        print(f"DEBUG: get_initial_queryset - Total records: {qs.count()}")
        print(f"DEBUG: get_initial_queryset - IDs: {list(qs.values_list('id', flat=True).order_by('id'))}")
        
        return qs

    def render_column(self, row, column):
        # We want to render some columns in a special way
        if column == 'id':
            # Debug: log the actual ID for this row
            print(f"DEBUG: Rendering ID column for row {row.pk}: {row.id}")
            return str(row.id)
        elif column == 'start':
            return render_ts_local(row.start),
        elif column == 'source':
            if hasattr(row.beacon.listener, "althost") and row.beacon.listener.althost:
                return f'<ul class="fa-ul"><li><span class="fa-li text-muted"><i class="fas fa-network-wired"></i></span>{ escape(row.beacon.listener.althost) }</li></ul>'
            elif hasattr(row.beacon.listener, "host") and row.beacon.listener.host:
                return f'<ul class="fa-ul"><li><span class="fa-li text-muted"><i class="fas fa-network-wired"></i></span>{ escape(row.beacon.listener.host) }</li></ul>'
            else:
                return "-"
        elif column == 'target':
            result = '<ul class="fa-ul">'

            if row.beacon.computer:
                result += f'<li><span class="fa-li text-muted"><i class="fas fa-network-wired"></i></span>{ escape(row.beacon.computer) }</li>\n'

            if row.beacon.user:
                result += f'<li><span class="fa-li text-muted"><i class="fas fa-user"></i></span>{ escape(row.beacon.user) }</li>\n'

            if row.beacon.process:
                result += f'<li><span class="fa-li text-muted"><i class="far fa-window-maximize"></i></span>{ escape(row.beacon.process) } (PID: {escape(row.beacon.pid)})</li>\n'

            if '<li>' not in result:
                result += "-"

            result += '</ul>'
            return result
        elif column == 'data':
            result = ""
            rowdescription = row.description
            if rowdescription:
                result = f"<div class='description'>{html.escape(rowdescription)}</div>"

            rowinput = row.input
            if rowinput:
                result += f"<div class='input'>{html.escape(rowinput)}</div>"

            rowoutput = row.output
            if rowoutput:
                result += f"<div class='output'>{html.escape(rowoutput)}</div>"

            return result
        elif column == '':  # The column with button in
            if row.event_mappings.exists() and self.request.user.has_perm('event_tracker.change_event'):
                return f'<a href="{reverse("event_tracker:event-update", args=[row.event_mappings.first().event.task_id, row.event_mappings.first().event.id])}" role="button" class="btn btn-primary btn-sm" data-toggle="tooltip" title="Edit Event"><i class="fa-regular fa-pen-to-square"></i></a>'
            elif (not row.event_mappings.exists()) and self.request.user.has_perm('event_tracker.add_event'):
                return f'<a href="{reverse("event_tracker:cs-log-to-event", args=[row.id])}" role="button" class="btn btn-success btn-sm" data-toggle="tooltip" title="Clone to Event"><i class="far fa-copy"></i></a>'
            else:
                return ""
        else:
            return truncatechars_html((super(CSActionListJSON, self).render_column(row, column)), 400)

    def filter_queryset_by_searchterm(self, qs, term):
        # Check if the search term is a number (potential ID search)
        try:
            id_search = int(term)
            # If it's a number, search by ID first
            id_qs = qs.filter(id=id_search)
            if id_qs.exists():
                return id_qs
        except ValueError:
            # Not a number, continue with regular search
            pass
        
        q = Q(beacon__listener__althost__icontains=term) | Q(beacon__listener__host__icontains=term) | \
            Q(beacon__computer__icontains=term) | Q(beacon__user__icontains=term) | Q(
            beacon__process__icontains=term) | \
            Q(archive__data__icontains=term) | Q(beaconlog__data__icontains=term) | \
            Q(tactic_anno__icontains=term) | Q(beacon__pid=term) | Q(operator_anno__icontains=term)

        # Adding more Q objects via filter() or using intersection is weirdly very slow, nested queries is equivalent
        # and gives the same output
        return qs.filter(pk__in=self.get_initial_queryset().filter(q).distinct().values("pk"))

    def get_page_for_id(self, target_id):
        """Find the page number for a specific ID without filtering the table."""
        try:
            target_id = int(target_id)
        except ValueError:
            return None
            
        # Get the base queryset
        base_qs = self.get_initial_queryset()
        
        # Debug: Check what IDs exist in the database
        all_ids = list(base_qs.values_list('id', flat=True).order_by('id'))
        print(f"DEBUG: All IDs in database: {all_ids}")
        
        # Check if the ID exists
        target_record = base_qs.filter(id=target_id).first()
        if not target_record:
            print(f"DEBUG: ID {target_id} not found in database")
            print(f"DEBUG: Available IDs: {all_ids}")
            return None
            
        # The table is ordered by timestamp descending (newest first), not by ID
        # So we need to count how many records have timestamps newer than our target
        records_before = base_qs.filter(start__gt=target_record.start).count()
        
        # Get the actual page length from the request or use default
        page_length = 100  # Default page length
        if hasattr(self, 'request') and self.request:
            try:
                page_length = int(self.request.GET.get('length', 100))
            except (ValueError, TypeError):
                pass
        
        # Calculate page number
        page_number = (records_before // page_length) + 1
        
        # Debug logging
        print(f"DEBUG: Looking for ID {target_id}")
        print(f"DEBUG: Target timestamp: {target_record.start}")
        print(f"DEBUG: Records with newer timestamps: {records_before}")
        print(f"DEBUG: Page length: {page_length}")
        print(f"DEBUG: Calculated page: {page_number}")
        
        # Additional verification - let's see what's actually on the calculated page
        total_records = base_qs.count()
        print(f"DEBUG: Total records in database: {total_records}")
        
        # Let's check what's on the first page by simulating the DataTables query
        first_page_records = base_qs.order_by('-start')[:page_length]
        first_page_ids = list(first_page_records.values_list('id', flat=True))
        print(f"DEBUG: IDs on first page: {first_page_ids}")
        
        return page_number

    def get(self, request, *args, **kwargs):
        """Handle GET requests for DataTables and page lookup."""
        # Check if this is a page lookup request
        target_id = request.GET.get('page_for_id')
        if target_id:
            # Debug: log what we received
            print(f"DEBUG: Received page_for_id request for ID: {target_id}")
            print(f"DEBUG: Request GET params: {dict(request.GET)}")
            
            page_number = self.get_page_for_id(target_id)
            if page_number is not None:
                return JsonResponse({'page': page_number, 'found': True})
            else:
                return JsonResponse({'found': False, 'error': 'ID not found'})
        
        # Otherwise, handle as normal DataTables request
        return super().get(request, *args, **kwargs)

# -- EventStream List

class EventStreamListView(PermissionRequiredMixin, TemplateView):
    permission_required = 'event_tracker.view_eventstream'
    template_name = "event_tracker/eventstream_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if EventStreamSourcePluginPoint.get_plugins_qs().filter(status=ENABLED).exists():
            context['source_plugins'] = []
            for plugin in EventStreamSourcePluginPoint.get_plugins():
                if plugin.is_access_permitted(self.request.user):
                    context['source_plugins'].append(plugin)

        return context

class EventStreamListJSON(PermissionRequiredMixin, FilterableDatatableView):
    permission_required = 'event_tracker.view_eventstream'
    model = ImportedEvent
    columns = ['timestamp', 'source', 'target', 'description', 'mitre_tactic', 'additional_data', '']
    order_columns = ['timestamp', '', '', 'description', 'mitre_tactic', '', '']
    filter_column_mapping = {'Timestamp': 'timestamp'}

    #TODO render & sort on MITRE technique if no tactic is provided

    def get_initial_queryset(self):
        return ImportedEvent.objects

    def render_column(self, row, column):
        # We want to render some columns in a special way
        if column == 'timestamp':
            return render_ts_local(row.timestamp)
        elif column == 'source':
            dummy_context = Context(host=row.source_host, user=row.source_user, process=row.source_process)
            return dummy_context.get_visible_html()
        elif column == 'target':
            dummy_context = Context(host=row.target_host, user=row.target_user, process=row.target_process)
            return dummy_context.get_visible_html()
        elif column == 'description':
            result = ""
            if row.description:
                result = f"<div class='description'>{row.description}</div>"

            if row.raw_evidence:
                result += f"<div class='output'>{escape(row.raw_evidence)}</div>"

            return result
        elif column == 'additional_data' and row.additional_data:
            additional_data_dict = json.loads(row.additional_data)
            escaped_dict = {}
            for key, value in additional_data_dict.items():
                escaped_dict[escape(key)] = escape(value)
            return json2table.convert(escaped_dict, table_attributes={'class': 'table shadow-sm table-sm table-borderless table-striped-columns mb-0'})
        elif column == '':  # The column with button in
            if row.event_mappings.exists() and self.request.user.has_perm('event_tracker.change_event'):
                return f'<a href="{reverse("event_tracker:event-update", args=[row.event_mappings.first().event.task_id, row.event_mappings.first().event.id])}" role="button" class="btn btn-primary btn-sm" data-toggle="tooltip" title="Edit Event"><i class="fa-regular fa-pen-to-square"></i></a>'
            elif (not row.event_mappings.exists()) and self.request.user.has_perm('event_tracker.add_event'):
                return f'<a href="{reverse("event_tracker:eventstream-to-event", args=[row.id])}" role="button" class="btn btn-success btn-sm" data-toggle="tooltip" title="Clone to Event"><i class="far fa-copy"></i></a>'
            else:
                return ""
        else:
            return truncatechars_html((super(EventStreamListJSON, self).render_column(row, column)), 400)

    def filter_queryset_by_searchterm(self, qs, term):
        q = Q(operator__icontains=term) | Q(description__icontains=term) | \
            Q(source_user__icontains=term) | Q(source_host__icontains=term) | Q(source_process__icontains=term) | \
            Q(target_user__icontains=term) | Q(target_host__icontains=term) | Q(target_process__icontains=term) | \
            Q(mitre_tactic__icontains=term) | Q(mitre_technique__icontains=term) | Q(additional_data__icontains=term)

        return qs.filter(q)


class EventStreamUploadForm(forms.Form):
    file = forms.FileField(help_text="A text file containing an EventStream JSON blob per line", widget=forms.FileInput(attrs={'accept':'.json,text/json'}))


class EventStreamUpload(PermissionRequiredMixin, TemplateView):
    permission_required = 'event_tracker.add_eventstream'
    template_name = "event_tracker/eventstream_upload.html"

    def __init__(self):
        super().__init__()
        with open(finders.find("eventstream/eventstream.schema.json")) as schemafp:
            self.schema = json.load(schemafp)

    def get_context_data(self, **kwargs):
        context = super(TemplateView, self).get_context_data(**kwargs)
        context['form'] = EventStreamUploadForm()
        return context

    def post(self, request, *args, **kwargs):
        form = EventStreamUploadForm(request.POST, request.FILES)
        if form.is_valid():
            # There's a risk that a hash spans two chunks and therefore won't get captured by regex, so split on
            # newlines
            previous_chunk = ""

            for chunk in request.FILES['file'].chunks():
                chunk_txt = chunk.decode("UTF-8")
                last_newline = chunk_txt.rfind("\n")

                chunk_main = previous_chunk + chunk_txt[:last_newline]
                self.add_single_eventstream(chunk_main)
                previous_chunk = chunk_txt[last_newline:]

            # Handle final part of upload between last newline and EOF
            if previous_chunk:
                self.add_single_eventstream(previous_chunk)

            return redirect(reverse_lazy('event_tracker:eventstream-list'))

    def add_single_eventstream(self, lines_to_parse):
        for line in lines_to_parse.split("\n"):
            line.strip()
            if line:
                try:
                    eventstream_dict = json.loads(line)
                    jsonschema.validate(instance=eventstream_dict, schema=self.schema)
                    imported_event_dict = {  # Optional field defaults:
                        "timestamp_end": None,
                        "operator": "",
                        "source_process": "",
                        "source_user": "",
                        "source_host": "",
                        "target_process": "",
                        "target_user": "",
                        "target_host": "",
                        "mitre_tactic": None,
                        "mitre_technique": None,
                        "outcome": None,
                        "description": "",
                        "raw_evidence": None
                    }
                    imported_event_dict["timestamp"] = parse_datetime(eventstream_dict.pop("ts"))

                    if "d" in eventstream_dict:
                        imported_event_dict["description"] = eventstream_dict.pop("d")

                    if "e" in eventstream_dict:
                        imported_event_dict["raw_evidence"] = eventstream_dict.pop("e")

                    if "te" in eventstream_dict:
                        imported_event_dict["timestamp_end"] = parse_datetime(eventstream_dict.pop("te"))

                    if "op" in eventstream_dict:
                        imported_event_dict["operator"] = (eventstream_dict.pop("op"))

                    if "s" in eventstream_dict:
                        s = eventstream_dict.pop("s")
                        if "h" in s:
                            imported_event_dict["source_host"] = s["h"]
                        if "u" in s:
                            imported_event_dict["source_user"] = s["u"]
                        if "p" in s:
                            imported_event_dict["source_process"] = s["p"]

                    if "t" in eventstream_dict:
                        t = eventstream_dict.pop("t")
                        if "h" in t:
                            imported_event_dict["target_host"] = t["h"]
                        if "u" in t:
                            imported_event_dict["target_user"] = t["u"]
                        if "p" in t:
                            imported_event_dict["target_process"] = t["p"]

                    if "ma" in eventstream_dict:
                        ma = eventstream_dict.pop("ma")
                        if "ta" in ma:
                            imported_event_dict["mitre_tactic"] = ma["ta"]
                        if "t" in ma:
                            imported_event_dict["mitre_technique"] = ma["t"]

                    if "o" in eventstream_dict:
                        imported_event_dict["outcome"] = eventstream_dict.pop("o")

                    if eventstream_dict:  # If there's still data in the JSON
                        imported_event_dict["additional_data"] = json.dumps(eventstream_dict)

                    ImportedEvent.objects.get_or_create(**imported_event_dict)
                except ValidationError as e:
                    print(f"Schema Validation Error: {e}")
                except JSONDecodeError as e:
                    print(f"JSON Error: {e}")

class EventStreamToEventView(EventCreateView):
    def get_initial(self):
        task = Task.objects.order_by("-id").first()
        imported_event = get_object_or_404(ImportedEvent, pk=self.kwargs.get('pk'))

        tactic = None
        technique = None
        subtechnique = None

        if imported_event.mitre_tactic:
            tactic = AttackTactic.objects.get(mitre_id=imported_event.mitre_tactic)

        if imported_event.mitre_technique:
            try:
                if "." in imported_event.mitre_technique:
                    # It will be a subtechnique:
                    subtechnique = AttackSubTechnique.objects.get(mitre_id=imported_event.mitre_technique)
                    if subtechnique:
                        # Reset the string we're parsing into just the technique part
                        imported_event.mitre_technique = imported_event.mitre_technique.split(".")[0]

                # Parse the string as a technique
                technique = AttackTechnique.objects.get(mitre_id=imported_event.mitre_technique)
                if not tactic:
                    # Guess at the first of the applicable tactics
                    tactic = technique.tactics.first()
            except (AttackTechnique.DoesNotExist, AttackSubTechnique.DoesNotExist):
                pass

        if imported_event.operator:
            operator = User.objects.filter(username__iexact=imported_event.operator).first()
        else:
            operator = None

        source = None
        if imported_event.source_host or imported_event.source_user or imported_event.source_process:
            source, _ = Context.objects.get_or_create(host=imported_event.source_host,
                                                   user=imported_event.source_user,
                                                   process=imported_event.source_process)

        target = None
        if imported_event.target_host or imported_event.target_user or imported_event.target_process:
            target, _ = Context.objects.get_or_create(host=imported_event.target_host,
                                                   user=imported_event.target_user,
                                                   process=imported_event.target_process)

        return {
            "task": task,
            "timestamp": imported_event.timestamp,
            "timestamp_end": imported_event.timestamp_end,
            "source": source,
            "target": target,
            "operator": operator,
            "mitre_attack_tactic": tactic,
            "mitre_attack_technique": technique,
            "mitre_attack_subtechnique": subtechnique,
            "description": imported_event.description,
            "raw_evidence": imported_event.raw_evidence,
            "outcome": imported_event.outcome,
        }

    def form_valid(self, form):
        response = super(EventStreamToEventView, self).form_valid(form)

        imported_event = get_object_or_404(ImportedEvent, pk=self.kwargs.get('pk'))

        mapping = EventMapping(source_object=imported_event, event=self.object)
        mapping.save()

        return response

# -- CS Uploads

class CSUploadsListView(PermissionRequiredMixin, ListView):
    permission_required = 'cobalt_strike_monitor.view_archive'
    template_name = "cobalt_strike_monitor/uploads_list.html"

    def get_queryset(self):
        return (Archive.objects.filter(beacon__in=Beacon.visible_beacons())
                .filter(type="indicator", data__startswith="file:").order_by("-when"))


class CSDownloadsListView(PermissionRequiredMixin, ListView):
    permission_required = 'cobalt_strike_monitor.view_download'
    template_name = "cobalt_strike_monitor/downloads_list.html"

    def get_queryset(self):
        return Download.objects.filter(beacon__in=Beacon.visible_beacons()).order_by("-date")


class CSBeaconsListView(PermissionRequiredMixin, ListView):
    permission_required = 'cobalt_strike_monitor.view_beacon'
    template_name = "cobalt_strike_monitor/beacon_list.html"

    def get_queryset(self):
        return Beacon.visible_beacons().order_by("-opened")

    def get_context_data(self, **kwargs):
        context = super(CSBeaconsListView, self).get_context_data()
        context["reconnection_watcher_bids"] = BeaconReconnectionWatcher.objects.values_list("beacon", flat=True)
        return context


@permission_required('cobalt_strike_monitor.add_beaconreconnectionwatcher')
def beaconwatch_add(request, beacon_id):
    BeaconReconnectionWatcher.objects.get_or_create(beacon_id=beacon_id)
    return redirect("event_tracker:cs-beacons-list")


@permission_required('cobalt_strike_monitor.delete_beaconreconnectionwatcher')
def beaconwatch_remove(request, beacon_id):
    try:
        BeaconReconnectionWatcher.objects.get(beacon_id=beacon_id).delete()
    except:
        pass  # The alert may have already fired, so ignore any errors in deleting it
    return redirect("event_tracker:cs-beacons-list")


class CSBeaconsTimelineView(PermissionRequiredMixin, TemplateView):
    permission_required = ('cobalt_strike_monitor.view_beacon','cobalt_strike_monitor.view_beaconpresence')
    template_name = "cobalt_strike_monitor/beacon_timeline.html"

    def get_context_data(self, *, object_list=None, **kwargs):
        data = dict()

        max_sleep = BeaconPresence.objects.filter(beacon__in=Beacon.visible_beacons())\
            .all().aggregate(Max('sleep_seconds'))['sleep_seconds__max']

        for beacon in Beacon.visible_beacons().all():
            if beacon.beaconpresence_set.exists():
                group = f"{beacon.user} {beacon.computer}"
                label = f"{beacon.process} (PID: {beacon.pid})"

                if group not in data:
                    data[group] = dict()

                if label not in data[group]:
                    data[group][label] = []

                for presence in beacon.beaconpresence_set.all():
                    data[group][label].append({"from": presence.first_checkin, "to": presence.last_checkin,
                                               "sleep": presence.sleep_seconds, "jitter": presence.sleep_jitter,
                                               "sleep_scale": 0 if max_sleep == 0 else presence.sleep_seconds / max_sleep})

        return {"timeline":data}


def previous_hop_to_context(beacon):
    """
    Generate a SS Context object for the beacon's previous hop, taking into account the possibility of chained beacons
    """
    if beacon.parent_beacon:
        return cs_beacon_to_context(None, beacon.parent_beacon)
    else:
        return cs_listener_to_context(None, beacon.listener)


class CSLogToEventView(EventCreateView):
    def get_initial(self):
        task = Task.objects.order_by("-id").first()
        cs_action = get_object_or_404(CSAction, pk=self.kwargs.get('pk'))

        tactic = None
        technique = None
        subtechnique = None

        # Find associated MITRE tactic:
        action_tactic = cs_action.tactic

        if action_tactic:
            cs_mitre_refs = action_tactic.split(",")  # These are typically techniques, not tactics, but CS names them wrong
            for cs_mitre_ref in cs_mitre_refs:
                try:
                    if "." in cs_mitre_ref:
                        # It may be a subtechnique:
                        subtechnique = AttackSubTechnique.objects.get_by_natural_key(cs_mitre_ref)
                        if subtechnique:
                            # Reset the string we're parsing into just the technique part
                            cs_mitre_ref = cs_mitre_ref.split(".")[0]

                    # Parse the string as a technique
                    technique = AttackTechnique.objects.get_by_natural_key(cs_mitre_ref)
                    if technique:
                        tactic = technique.tactics.first()
                        break
                except (AttackTechnique.DoesNotExist, AttackSubTechnique.DoesNotExist):
                    pass

        if cs_action.operator:
            # Do a "fuzzy" match to find a user with the same case-insensitive username as the operator,
            # ignoring any trailing digits which are sometimes added to CS operator logins to workaround concurrent
            # logins.
            operator = User.objects.filter(username__istartswith=cs_action.operator.rstrip(string.digits)).first()
        else:
            operator = None

        return {
            "task": task,
            "timestamp": timezone.localtime(cs_action.start).strftime("%Y-%m-%dT%H:%M"),
            "source": previous_hop_to_context(cs_action.beacon),
            "target": cs_beacon_to_context(None, cs_action.beacon),
            "operator": operator,
            "mitre_attack_tactic": tactic,
            "mitre_attack_technique": technique,
            "mitre_attack_subtechnique": subtechnique,
            "description": cs_action.description,
            "raw_evidence": f"{cs_action.input}{chr(13)+chr(13) + cs_action.output if cs_action.output else ''}"
        }

    def get_context_data(self, **kwargs):
        context = super(EventCreateView, self).get_context_data(**kwargs)

        cs_action = get_object_or_404(CSAction, pk=self.kwargs.get('pk'))

        if cs_action.indicators.exists() and not self.request.POST:
            file, location = cs_indicator_archive_to_file(cs_action.indicators.first().data)
            initial = [{"location": location,
                        "file": file,
                        "removed": False
                        }]

            context["file_distributions_formset"] = FileDistributionFormSet(initial=initial)
            context["file_distributions_formset"].extra += len(initial)
        else:
            # This should come from the super call - confused...
            if self.request.POST:
                context["file_distributions_formset"] = FileDistributionFormSet(self.request.POST)
            else:
                context["file_distributions_formset"] = FileDistributionFormSet()

        context['action'] = "Create"

        return context

    def form_valid(self, form):
        response = super(CSLogToEventView, self).form_valid(form)

        cs_action = get_object_or_404(CSAction, pk=self.kwargs.get('pk'))

        mapping = EventMapping(source_object=cs_action, event=self.object)
        mapping.save()

        return response

class CSDownloadToEventView(EventCreateView):
    def get_initial(self):
        task = Task.objects.order_by("-id").first()
        download = get_object_or_404(Download, pk=self.kwargs.get('pk'))

        # Collection
        tactic = AttackTactic.objects.get(mitre_id="TA0009")

        if download.path.startswith("\\\\"):
            # Data from network shared drive
            technique = AttackTechnique.objects.get(mitre_id="T1039")
        else:
            # Data from local system
            technique = AttackTechnique.objects.get(mitre_id="T1005")

        return {
            "task": task,
            "timestamp": timezone.localtime(download.date).strftime("%Y-%m-%dT%H:%M"),
            "source": cs_beacon_to_context(None, download.beacon),
            "target": previous_hop_to_context(download.beacon),
            "operator": None,
            "mitre_attack_tactic": tactic,
            "mitre_attack_technique": technique,
            "mitre_attack_subtechnique": None,

            "description": f"Downloaded \"{download.name}\" ({download.size:,} bytes) from {download.path}"
        }

    def form_valid(self, form):
        response = super(CSDownloadToEventView, self).form_valid(form)

        download = get_object_or_404(Download, pk=self.kwargs.get('pk'))

        mapping = EventMapping(source_object=download, event=self.object)
        mapping.save()

        return response


class CSBeaconToEventView(EventCreateView):
    def get_initial(self):
        task = Task.objects.order_by("-id").first()
        beacon = get_object_or_404(Beacon, pk=self.kwargs.get('pk'))

        tactic = AttackTactic.objects.get(mitre_id="TA0011")

        # Defaults:
        technique = AttackTechnique.objects.get(mitre_id="T1095")  # Non-application layer protocol
        subtechnique = None
        protocol = ""

        if beacon.listener:
            # Assuming HTTP listener
            if beacon.listener.payload == "windows/beacon_https/reverse_https":
                if beacon.listener.host != beacon.listener.althost:
                    # Assume domain fronting
                    technique = AttackTechnique.objects.get(mitre_id="T1090")
                    subtechnique = AttackSubTechnique.objects.get(mitre_id="T1090.004")
                    protocol = "domain-fronted HTTPS"
                else:
                    # Assume direct connection
                    technique = AttackTechnique.objects.get(mitre_id="T1071")
                    subtechnique = AttackSubTechnique.objects.get(mitre_id="T1071.001")
                    protocol = "direct HTTPS"
            elif beacon.listener.payload == "windows/beacon_bind_pipe":
                technique = AttackTechnique.objects.get(mitre_id="T1090")
                subtechnique = AttackSubTechnique.objects.get(mitre_id="T1090.001")
                protocol = "SMB Named Pipe"

        return {
            "task": task,
            "timestamp": timezone.localtime(beacon.opened).strftime("%Y-%m-%dT%H:%M"),
            "source": cs_beacon_to_context(None, beacon),
            "target": previous_hop_to_context(beacon),
            "operator": None,
            "mitre_attack_tactic": tactic,
            "mitre_attack_technique": technique,
            "mitre_attack_subtechnique": subtechnique,

            "description": f"New {protocol} command and control connection from Cobalt Strike beacon on {beacon.os_human}".replace("  ", " "),
            "outcome": f"Remote {'administrative' if beacon.user.endswith(' *') else ''} control of device".replace("  ", " ")
        }

    def form_valid(self, form):
        response = super(CSBeaconToEventView, self).form_valid(form)

        beacon = get_object_or_404(Beacon, pk=self.kwargs.get('pk'))

        mapping = EventMapping(source_object=beacon, event=self.object)
        mapping.save()

        return response


class BeaconExclusionForm(forms.Form):
    exclusion_type = forms.ChoiceField(choices=[("id", "id"),
                                                ("user", "user"),
                                                ("computer", "computer"),
                                                ("process", "process"),
                                                ("internal", "internal"),
                                                ("external", "external")])
    beacon_id = forms.IntegerField()


@permission_required('cobalt_strike_monitor.add_beaconexclusion')
def create_beacon_exclusion(request):
    form = BeaconExclusionForm(request.POST)
    if form.is_valid():
        original_beacon = Beacon.objects.get(id=form.cleaned_data['beacon_id'])

        if form.cleaned_data['exclusion_type'] == "id":
            obj, _ = BeaconExclusion.objects.get_or_create(**{"beacon_id":
                                                         original_beacon.__getattribute__(form.cleaned_data['exclusion_type'])})
        else:
            obj, _ = BeaconExclusion.objects.get_or_create(**{form.cleaned_data['exclusion_type']:
                                                         original_beacon.__getattribute__(form.cleaned_data['exclusion_type'])})
    else:
        print("invalid beacon exclusion form")

    return redirect('event_tracker:cs-beacons-list')


class BeaconExclusionList(PermissionRequiredMixin, ListView):
    permission_required = 'cobalt_strike_monitor.view_beaconexclusion'
    model = BeaconExclusion


class BeaconExclusionDeleteView(PermissionRequiredMixin, DeleteView):
    permission_required = 'cobalt_strike_monitor.delete_beaconexclusion'
    model = BeaconExclusion

    def get_success_url(self):
        return reverse_lazy('event_tracker:cs-beacon-exclusion-list')


class WebhookListView(PermissionRequiredMixin, ListView):
    permission_required = 'event_tracker.view_webhook'
    model = Webhook


class WebhookCreateView(PermissionRequiredMixin, CreateView):
    permission_required = 'event_tracker.add_webhook'
    model = Webhook
    fields = "__all__"

    def get_success_url(self):
        return reverse_lazy('event_tracker:webhook-list')

    def get_context_data(self, **kwargs):
        context = super(WebhookCreateView, self).get_context_data(**kwargs)
        context["action"] = "Create"
        return context


class WebhookUpdateView(PermissionRequiredMixin, UpdateView):
    permission_required = 'event_tracker.change_webhook'
    model = Webhook
    fields = "__all__"

    def get_context_data(self, **kwargs):
        context = super(WebhookUpdateView, self).get_context_data(**kwargs)
        context["action"] = "Update"
        return context


class WebhookDeleteView(PermissionRequiredMixin, DeleteView):
    permission_required = 'event_tracker.delete_webhook'
    model = Webhook

    def get_success_url(self):
        return reverse_lazy('event_tracker:webhook-list')


@permission_required(('event_tracker.add_webhook','event_tracker.change_webhook'))
def trigger_dummy_webhook(request, webhook_id):
    webhook = get_object_or_404(Webhook, pk=webhook_id)
    dummy_ts = TeamServer(description="Dummy Team Server")
    dummy_beacon = Beacon(user="user", computer="computer", process="process.exe", team_server=dummy_ts)
    notify_webhook_new_beacon(webhook, dummy_beacon)

    return redirect(reverse_lazy('event_tracker:webhook-list'))


class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ['code', 'name', 'start_date', 'end_date']

    start_date = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "date"}))
    end_date = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "date"}))


class InitialConfigTask(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = Task
    form_class = TaskForm
    template_name = "initial-config/initial-config-task.html"

    def test_func(self):
        # Test if an operation is active and if it has any tasks.
        # Allow access only if an operation is active AND it has no tasks yet.
        if hasattr(self.request, 'current_operation') and self.request.current_operation:
            # Ensure we are querying the active_op_db for tasks related to this operation
            # Since tasks are solely in active_op_db, a simple check is enough.
            return not Task.objects.using('active_op_db').exists()
        return False # No active operation, or other issue.

    def get_permission_denied_message(self):
        if not (hasattr(self.request, 'current_operation') and self.request.current_operation):
            return "No operation is currently active. Please select or create an operation first."
        if Task.objects.using('active_op_db').exists():
            return "The current operation already has tasks. You can create new tasks from the operation dashboard or event list."
        return super().get_permission_denied_message()

    def handle_no_permission(self):
        # If an operation is active but tasks exist, redirect to event list of first task.
        if hasattr(self.request, 'current_operation') and self.request.current_operation and Task.objects.using('active_op_db').exists():
            first_task_pk = Task.objects.using('active_op_db').first().pk
            messages.info(self.request, "This operation already has tasks. Redirecting to the event list.")
            return redirect(reverse_lazy('event_tracker:event-list', kwargs={"task_id": first_task_pk}))
        # If no operation is active, redirect to select operation.
        if not (hasattr(self.request, 'current_operation') and self.request.current_operation):
            messages.warning(self.request, "Please select an active operation before creating a task.")
            return redirect(reverse_lazy('event_tracker:select_operation'))
        return super().handle_no_permission()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if hasattr(self.request, 'current_operation') and self.request.current_operation:
            context['current_operation_display_name'] = self.request.current_operation.display_name
        else:
            # This case should ideally be prevented by test_func and handle_no_permission
            context['current_operation_display_name'] = "None (Error: No active operation!)"
        context['page_title'] = "Create Initial Task for Operation"
        return context

    def form_valid(self, form):
        # Task will be saved to active_op_db due to router configuration
        self.object = form.save() # Router handles saving to active_op_db
        messages.success(self.request, f"Initial task '{self.object.name}' created successfully for operation '{self.request.current_operation.display_name}'.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        # Redirect to the event list for the newly created task
        return reverse_lazy('event_tracker:event-list', kwargs={"task_id": self.object.pk})


class UserPreferencesForm(forms.ModelForm):
    class Meta:
        model = UserPreferences
        fields = ['timezone']


class InitialConfigAdmin(CreateView): # Temporarily remove UserPassesTestMixin
    model = User
    form_class = UserCreationForm
    template_name = "initial-config/initial-config-admin.html"

    def dispatch(self, request, *args, **kwargs):
        print(f"[DEBUG] InitialConfigAdmin.dispatch: Path: {request.path}")
        try:
            user_count = User.objects.using(DEFAULT_DB_ALIAS).count()
            print(f"[DEBUG] InitialConfigAdmin.dispatch: User.objects.count() result: {user_count}")
            if user_count > 0: # If users exist
                 print(f"[DEBUG] InitialConfigAdmin.dispatch: Users exist ({user_count}), redirecting to login.")
                 # Simulate UserPassesTestMixin's redirect to login if test_func fails (users exist)
                 return redirect_to_login(request.get_full_path(), reverse_lazy('login'), self.get_redirect_field_name())
        except Exception as e:
            print(f"[DEBUG] InitialConfigAdmin.dispatch: EXCEPTION checking user count: {e}")
            # If DB error, don't try to redirect, let it proceed to super().dispatch which might show an error page.
            pass 
        
        print(f"[DEBUG] InitialConfigAdmin.dispatch: Proceeding to super().dispatch (user_count should be 0 or exception occurred).")
        return super().dispatch(request, *args, **kwargs)

    def get_success_url(self):
        messages.success(self.request, "Initial admin user created successfully. Please create or select an operation.")
        return reverse_lazy('event_tracker:select_operation')

    def get_context_data(self, **kwargs):
        print("[DEBUG] InitialConfigAdmin.get_context_data: START")
        context = super().get_context_data(**kwargs)
        print(f"[DEBUG] InitialConfigAdmin.get_context_data: Context keys from super: {list(context.keys())}")
        if 'form' in context:
            print("[DEBUG] InitialConfigAdmin.get_context_data: 'form' IS in context.")
            form_instance = context['form']
            print(f"[DEBUG] InitialConfigAdmin.get_context_data: type(context['form']) = {type(form_instance)}")
            if hasattr(form_instance, 'is_bound'):
                print(f"[DEBUG] InitialConfigAdmin.get_context_data: form_instance.is_bound = {form_instance.is_bound}")
            else:
                print("[DEBUG] InitialConfigAdmin.get_context_data: form_instance does NOT have 'is_bound' attribute.")

            if hasattr(form_instance, 'fields'):
                print(f"[DEBUG] InitialConfigAdmin.get_context_data: form_instance.fields keys: {list(form_instance.fields.keys())}")
                if 'username' in form_instance.fields:
                    print(f"[DEBUG] InitialConfigAdmin.get_context_data: type(form_instance.fields['username']) = {type(form_instance.fields['username'])}")
                    # Try rendering the field widget to string
                    try:
                        username_field = form_instance['username'] # BoundField
                        rendered_widget = username_field.as_widget()
                        print(f"[DEBUG] InitialConfigAdmin.get_context_data: Successfully rendered form_instance['username'].as_widget() to string: {len(rendered_widget)} chars")
                    except Exception as e:
                        print(f"[DEBUG] InitialConfigAdmin.get_context_data: EXCEPTION rendering form_instance['username'].as_widget(): {e}")
                else:
                    print("[DEBUG] InitialConfigAdmin.get_context_data: 'username' field NOT in form_instance.fields.")
            else:
                print("[DEBUG] InitialConfigAdmin.get_context_data: form_instance does NOT have 'fields' attribute.")
        else:
            print("[DEBUG] InitialConfigAdmin.get_context_data: 'form' IS NOT in context after super() call.")
        
        # The original commented-out lines for UserPreferencesForm
        # from .forms import UserPreferencesForm # Moved to top if needed, or handle missing form
        # context['preferences_form'] = UserPreferencesForm()
        print("[DEBUG] InitialConfigAdmin.get_context_data: END (original print was: InitialConfigAdmin.get_context_data called.)")
        return context

    def form_valid(self, form):
        print("[DEBUG] InitialConfigAdmin.form_valid called.")
        result = super().form_valid(form)
        self.object.is_staff = True
        self.object.is_superuser = True
        self.object.save(using=DEFAULT_DB_ALIAS)
        
        # Assuming UserPreferencesForm is available
        # from .forms import UserPreferencesForm 
        # preferences_form = UserPreferencesForm(self.request.POST)
        # if preferences_form.is_valid():
        #     preferences = preferences_form.save(commit=False)
        #     preferences.user = self.object
        #     preferences.save(using=DEFAULT_DB_ALIAS)
        return result

    # test_func is removed as UserPassesTestMixin is removed for this debug step


class UserPreferencesView(LoginRequiredMixin, FormView):
    template_name = 'registration/user-preferences.html'
    form_class = UserPreferencesForm
    success_url = "/event-tracker/1"

    def get_initial(self):
        obj = UserPreferences.objects.filter(user=self.request.user).first()
        if not obj:
            return {}
        else:
            return {"timezone": obj.timezone}

    def form_valid(self, form):
        form_obj = form.save(commit=False)

        db_obj = UserPreferences.objects.filter(user=self.request.user).first()
        if not db_obj:
            # Create a new user preference, via the form's save()
            form_obj.user = self.request.user
            form_obj.save()
        else:
            # Update the existing user preference, copying info from the form
            db_obj.timezone = form_obj.timezone
            db_obj.save()

        return super().form_valid(form)


@permission_required('event_tracker.change_event')
def toggle_event_star(request, task_id, pk):
    event = get_object_or_404(Event, pk=pk)
    event.starred = not event.starred
    event.save()
    return HttpResponse(json.dumps({'starred': event.starred}), 'application/json')

@permission_required('event_tracker.change_event')
def toggle_qs_stars(request, task_id):
    eventfilter = EventFilterForm(request.session.get('eventfilter'), task_id=task_id)

    qs = Event.objects.all()

    if eventfilter.is_valid():
        qs = eventfilter.apply_to_queryset(qs)

    if qs.filter(starred=False).exists():
        qs.update(starred=True)
    else:
        qs.update(starred=False)

    return redirect(reverse_lazy("event_tracker:event-list", kwargs={"task_id": task_id}))


class EventFieldSuggestions(View):
    def post(self, request, *args, **kwargs):
        event_form = EventForm(request.POST)

        context = {}
        context["description_suggestions"], context["mitre_suggestions"] = generate_suggestions(event_form)

        return render(request, "suggestions/event_field_suggestions.html", context)

# Operation Management Views

class SelectOperationView(LoginRequiredMixin, ListView):
    model = Operation
    template_name = 'event_tracker/operation_select.html' # Create this template
    context_object_name = 'operations'

    def get_queryset(self):
        # Operations are stored in the default database
        return Operation.objects.using(DEFAULT_DB_ALIAS).order_by('display_name')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['current_op_name'] = self.request.session.get('active_operation_name')
        return context

class CreateOperationView(LoginRequiredMixin, CreateView):
    model = Operation
    form_class = OperationForm
    template_name = 'event_tracker/operation_form.html' # Create this template
    # success_url = reverse_lazy('event_tracker:select_operation') # Or activate directly

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        # Ensure form saves to the default database
        kwargs['instance'] = Operation(pk=None) # Pass an unsaved instance for ModelForm to know it's a create
        return kwargs

    def form_valid(self, form):
        # Ensure the operation is saved to the default database explicitly
        # The router should handle this, but being explicit can be safer with ModelForms.
        form.instance.save(using=DEFAULT_DB_ALIAS)
        messages.success(self.request, f"Operation '{form.instance.display_name}' created successfully. Please create an initial task.")
        
        # Instead of just activating in session here, redirect to the activate_operation view,
        # which handles full activation, DB initialization, and then redirects to initial-config-task.
        return redirect(reverse('event_tracker:activate_operation', kwargs={'operation_name': form.instance.name}))

from .middleware import _initialize_operation_db # <<< ENSURE THIS IMPORT IS HERE

@transaction.non_atomic_requests
@login_required
def activate_operation(request, operation_name):
    logger.info(f"[ACTIVATE_OP] Entered activate_operation for '{operation_name}'")
    
    # First, ensure we're not in any atomic transactions
    try:
        # Close any existing connections to ensure clean state
        if 'active_op_db' in connections:
            connections['active_op_db'].close()
            connections['active_op_db'].connection = None
            logger.info("[ACTIVATE_OP] Closed existing active_op_db connection")
        
        # Ensure atomic transactions are disabled for both databases
        connections.databases['default']['ATOMIC_REQUESTS'] = False
        connections.databases['active_op_db'] = {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(settings.OPS_DATA_DIR / f"{operation_name}.sqlite3"),
            'OPTIONS': {'timeout': 20},
            'ATOMIC_REQUESTS': False,
            'CONN_MAX_AGE': 0,
            'AUTOCOMMIT': True,
        }
        logger.info("[ACTIVATE_OP] Disabled atomic transactions for both databases")
    except Exception as e:
        logger.error(f"[ACTIVATE_OP] Error setting up database connections: {e}")
        messages.error(request, f"Error setting up database connections: {e}")
        return redirect('event_tracker:select_operation')

    try:
        operation = Operation.objects.using(DEFAULT_DB_ALIAS).get(name=operation_name)
        logger.info(f"[ACTIVATE_OP] Found operation '{operation.name}' in default database")
    except Operation.DoesNotExist:
        logger.error(f"[ACTIVATE_OP] Operation '{operation_name}' not found in default DB")
        messages.error(request, f"Operation '{operation_name}' not found.")
        return redirect('event_tracker:select_operation')

    try:
        CurrentOperation.objects.using('default').all().delete()
        logger.info("[ACTIVATE_OP] Cleared existing CurrentOperation entries")
    except Exception as e:
        logger.error(f"[ACTIVATE_OP] Error clearing CurrentOperation entries: {e}")

    # Initialize database with polling
    max_attempts = 5
    attempt = 0
    initialized = False
    last_error = None

    while attempt < max_attempts and not initialized:
        try:
            logger.info(f"[ACTIVATE_OP] Attempt {attempt + 1}/{max_attempts} to initialize database for '{operation.name}'")
            
            # Ensure we're not in an atomic transaction before each attempt
            if 'active_op_db' in connections:
                connections['active_op_db'].close()
                connections['active_op_db'].connection = None
            
            initialized, message = _initialize_operation_db(operation.name, settings.OPS_DATA_DIR)
            logger.info(f"[ACTIVATE_OP] _initialize_operation_db returned: initialized={initialized}, message={message}")
            
            if initialized:
                logger.info(f"[ACTIVATE_OP] Database initialized successfully for operation '{operation.name}'")
                break
            else:
                last_error = message
                logger.warning(f"[ACTIVATE_OP] Database initialization attempt {attempt + 1} failed: {message}")
                time.sleep(2)  # Wait 2 seconds before next attempt
                
        except Exception as e:
            last_error = str(e)
            logger.error(f"[ACTIVATE_OP] Error during initialization attempt {attempt + 1}: {e}")
            time.sleep(2)  # Wait 2 seconds before next attempt
            
        attempt += 1

    if not initialized:
        error_msg = f"Could not initialize database after {max_attempts} attempts. Last error: {last_error}"
        logger.error(f"[ACTIVATE_OP] {error_msg}")
        messages.error(request, f"Could not initialize database for operation '{operation.display_name}'. {error_msg}")
        request.session.pop('active_operation_name', None)
        request.session.pop('active_operation_display_name', None)
        try:
            CurrentOperation.objects.using('default').all().delete()
        except Exception as delete_error:
            logger.error(f"[ACTIVATE_OP] Error clearing CurrentOperation model: {delete_error}")
        return redirect('event_tracker:select_operation')

    try:
        # Create CurrentOperation entry
        CurrentOperation.objects.using('default').create(operation=operation)
        logger.info(f"[ACTIVATE_OP] Created new CurrentOperation entry for '{operation.name}'")

        # Set session variables
        request.session['active_operation_name'] = operation.name
        request.session['active_operation_display_name'] = operation.display_name
        logger.info(f"[ACTIVATE_OP] User '{request.user}' activated operation '{operation.name}' ('{operation.display_name}')")

        # Start team server pollers if needed
        from cobalt_strike_monitor.models import TeamServer
        from cobalt_strike_monitor.poll_team_server import TeamServerPoller
        enabled_servers = list(TeamServer.objects.using('active_op_db').filter(active=True))
        logger.info(f"[ACTIVATE_OP] Found {len(enabled_servers)} enabled teamservers in operation '{operation.name}'")
        if enabled_servers:
            logger.info(f"[ACTIVATE_OP] Starting pollers for {len(enabled_servers)} teamservers")
            poller = TeamServerPoller()
            for server in enabled_servers:
                poller.add(server.pk)
                logger.info(f"[ACTIVATE_OP] Started poller for teamserver {server.pk}")
        else:
            logger.info(f"[ACTIVATE_OP] No enabled teamservers found for operation '{operation.name}'")

        # Re-enable atomic requests for active_op_db
        connections.databases['active_op_db']['ATOMIC_REQUESTS'] = True
        if 'active_op_db' in connections:
            connections['active_op_db'].close()
            logger.info(f"[ACTIVATE_OP] Closed active_op_db connection after re-enabling ATOMIC_REQUESTS")

    except Exception as e:
        logger.error(f"[ACTIVATE_OP] Unexpected error after database initialization for operation '{operation.name}': {e}", exc_info=True)
        messages.error(request, f"Error activating operation '{operation.display_name}'. Error: {e}")
        request.session.pop('active_operation_name', None)
        request.session.pop('active_operation_display_name', None)
        try:
            CurrentOperation.objects.using('default').all().delete()
        except Exception as delete_error:
            logger.error(f"[ACTIVATE_OP] Error clearing CurrentOperation model: {delete_error}")
        return redirect('event_tracker:select_operation')

    messages.success(request, f"Activated operation: {operation.display_name}")

    next_url = request.GET.get('next')
    if next_url and url_has_allowed_host_and_scheme(url=next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        logger.debug(f"[ACTIVATE_OP] Operation '{operation.name}' activated. Redirecting to safe next_url: {next_url}")
        return redirect(next_url)
    else:
        if next_url:
            logger.warning(f"[ACTIVATE_OP] Operation '{operation.name}' activated. Unsafe next_url provided: '{next_url}'. Falling back to default redirect.")
        if not Task.objects.using('active_op_db').exists():
            logger.debug(f"[ACTIVATE_OP] Operation '{operation.name}' activated. No tasks found. Redirecting to initial task config.")
            return redirect('event_tracker:initial-config-task')
        else:
            first_task = Task.objects.using('active_op_db').order_by('id').first()
            logger.debug(f"[ACTIVATE_OP] Operation '{operation.name}' activated. Tasks exist. Redirecting to first task events: {first_task.id}")
            return redirect(reverse('event_tracker:event-list', kwargs={'task_id': first_task.id}))


class InitialConfigTask(LoginRequiredMixin, UserPassesTestMixin, CreateView):
    model = Task
    form_class = TaskForm
    template_name = "initial-config/initial-config-task.html"

    def test_func(self):
        # Test if an operation is active and if it has any tasks.
        # Allow access only if an operation is active AND it has no tasks yet.
        if hasattr(self.request, 'current_operation') and self.request.current_operation:
            # Ensure we are querying the active_op_db for tasks related to this operation
            # Since tasks are solely in active_op_db, a simple check is enough.
            return not Task.objects.using('active_op_db').exists()
        return False # No active operation, or other issue.

    def get_permission_denied_message(self):
        if not (hasattr(self.request, 'current_operation') and self.request.current_operation):
            return "No operation is currently active. Please select or create an operation first."
        if Task.objects.using('active_op_db').exists():
            return "The current operation already has tasks. You can create new tasks from the operation dashboard or event list."
        return super().get_permission_denied_message()

    def handle_no_permission(self):
        # If an operation is active but tasks exist, redirect to event list of first task.
        if hasattr(self.request, 'current_operation') and self.request.current_operation and Task.objects.using('active_op_db').exists():
            first_task_pk = Task.objects.using('active_op_db').first().pk
            messages.info(self.request, "This operation already has tasks. Redirecting to the event list.")
            return redirect(reverse_lazy('event_tracker:event-list', kwargs={"task_id": first_task_pk}))
        # If no operation is active, redirect to select operation.
        if not (hasattr(self.request, 'current_operation') and self.request.current_operation):
            messages.warning(self.request, "Please select an active operation before creating a task.")
            return redirect(reverse_lazy('event_tracker:select_operation'))
        return super().handle_no_permission()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if hasattr(self.request, 'current_operation') and self.request.current_operation:
            context['current_operation_display_name'] = self.request.current_operation.display_name
        else:
            # This case should ideally be prevented by test_func and handle_no_permission
            context['current_operation_display_name'] = "None (Error: No active operation!)"
        context['page_title'] = "Create Initial Task for Operation"
        return context

    def form_valid(self, form):
        # Task will be saved to active_op_db due to router configuration
        self.object = form.save() # Router handles saving to active_op_db
        messages.success(self.request, f"Initial task '{self.object.name}' created successfully for operation '{self.request.current_operation.display_name}'.")
        return redirect(self.get_success_url())

    def get_success_url(self):
        # Redirect to the event list for the newly created task
        return reverse_lazy('event_tracker:event-list', kwargs={"task_id": self.object.pk})

class ImportOperationView(LoginRequiredMixin, CreateView):
    form_class = ImportOperationForm
    template_name = 'event_tracker/operation_import.html'
    success_url = reverse_lazy('event_tracker:select_operation')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        # Remove instance from kwargs since ImportOperationForm is not a ModelForm
        kwargs.pop('instance', None)
        return kwargs

    def form_valid(self, form):
        try:
            # Create the operation in the default database
            operation = Operation.objects.create(
                name=form.cleaned_data['name'],
                display_name=form.cleaned_data['display_name']
            )

            # Get the uploaded file
            db_file = form.cleaned_data['db_file']
            
            # Save the database file to the ops-data directory
            import shutil
            import os
            from django.conf import settings
            from django.core.management import call_command
            from io import StringIO
            
            # Ensure the ops-data directory exists
            os.makedirs(settings.OPS_DATA_DIR, exist_ok=True)
            
            # Save the file
            destination_path = settings.OPS_DATA_DIR / f"{operation.name}.sqlite3"
            with open(destination_path, 'wb+') as destination:
                for chunk in db_file.chunks():
                    destination.write(chunk)

            # Update the active_op_db settings to point to the new database
            connections['active_op_db'].close()
            connections.databases['active_op_db']['NAME'] = str(destination_path)
            connections['active_op_db'].connection = None

            # Run migrations on the imported database
            core_apps = [
                'contenttypes',
                'auth',
                'admin',
                'sessions',
            ]

            our_apps = [
                'event_tracker',
                'cobalt_strike_monitor',
                'taggit',
                'djangoplugins',
                'reversion',
                'background_task'
            ]

            # Run migrations for core apps first
            for app_name in core_apps:
                migration_output_buffer = StringIO()
                try:
                    call_command(
                        'migrate',
                        app_name,
                        database='active_op_db',
                        verbosity=1,
                        stdout=migration_output_buffer,
                        stderr=migration_output_buffer
                    )
                except Exception as e:
                    messages.error(self.request, f"Error running migrations for {app_name}: {str(e)}")
                    return self.form_invalid(form)

            # Then run migrations for our apps
            for app_name in our_apps:
                migration_output_buffer = StringIO()
                try:
                    call_command(
                        'migrate',
                        app_name,
                        database='active_op_db',
                        verbosity=1,
                        stdout=migration_output_buffer,
                        stderr=migration_output_buffer
                    )
                except Exception as e:
                    messages.error(self.request, f"Error running migrations for {app_name}: {str(e)}")
                    return self.form_invalid(form)

            # Sync users from default database to operation database
            from django.contrib.auth import get_user_model
            User = get_user_model()
            
            # Get all users from default database
            default_users = User.objects.using('default').all()
            
            # Copy each user to the operation database
            for user in default_users:
                new_user = User(
                    id=user.id,
                    username=user.username,
                    password=user.password,
                    is_superuser=user.is_superuser,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    email=user.email,
                    is_staff=user.is_staff,
                    is_active=user.is_active,
                    date_joined=user.date_joined
                )
                new_user.save(using='active_op_db')

            messages.success(self.request, f"Operation '{operation.display_name}' imported successfully.")
            return super().form_valid(form)
        except Exception as e:
            messages.error(self.request, f"Error importing operation: {str(e)}")
            return self.form_invalid(form)

class UpdateOperationView(LoginRequiredMixin, UpdateView):
    model = Operation
    form_class = OperationForm
    template_name = 'event_tracker/operation_form.html'
    slug_field = 'name'
    slug_url_kwarg = 'operation_name'

    def get_queryset(self):
        return Operation.objects.using(DEFAULT_DB_ALIAS)

    def form_valid(self, form):
        old_name = self.get_object().name
        new_name = form.cleaned_data['name']
        form.instance.save(using=DEFAULT_DB_ALIAS)
        if old_name != new_name:
            old_db_path = settings.OPS_DATA_DIR / f"{old_name}.sqlite3"
            new_db_path = settings.OPS_DATA_DIR / f"{new_name}.sqlite3"
            if old_db_path.exists():
                try:
                    old_db_path.rename(new_db_path)
                except Exception as e:
                    messages.warning(self.request, f"Could not rename database file: {e}")
            else:
                messages.warning(self.request, f"Old database file {old_db_path} not found for renaming.")
        messages.success(self.request, f"Operation '{form.instance.display_name}' updated successfully.")
        return redirect('event_tracker:select_operation')

class DeleteOperationView(LoginRequiredMixin, DeleteView):
    model = Operation
    template_name = 'event_tracker/operation_confirm_delete.html'
    slug_field = 'name'
    slug_url_kwarg = 'operation_name'

    def get_queryset(self):
        return Operation.objects.using(DEFAULT_DB_ALIAS)

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        db_path = settings.OPS_DATA_DIR / f"{self.object.name}.sqlite3"
        # Capture db_path before deletion
        response = super().delete(request, *args, **kwargs)
        if db_path.exists():
            try:
                db_path.unlink()
            except Exception as e:
                messages.warning(request, f"Could not delete database file: {e}")
        else:
            messages.warning(request, f"Database file {db_path} not found for deletion.")
        messages.success(request, f"Operation '{self.object.display_name}' and its database file have been deleted.")
        return response

    def get_success_url(self):
        return reverse_lazy('event_tracker:select_operation')
