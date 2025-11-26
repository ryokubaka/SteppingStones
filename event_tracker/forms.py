from django import forms
from django.utils.text import slugify
from .models import Operation, Task, Event, FileDistribution, UserPreferences, AttackTactic, AttackTechnique, AttackSubTechnique, Context, File # Added Operation and other models
from dal import autocomplete
from django_tomselect.forms import TomSelectMultipleChoiceField, TomSelectConfig
from taggit.models import Tag
from django.contrib.auth.models import User
from django.urls import reverse_lazy # Moved this up as it's used in EventForm


class OperationForm(forms.ModelForm):
    class Meta:
        model = Operation
        fields = ['name', 'display_name']
        help_texts = {
            'name': "A unique name for the operation, used for the database filename. Should contain only letters, numbers, hyphens, or underscores (e.g., 'op_alpha_2024').",
            'display_name': "A user-friendly display name for the operation (e.g., 'Operation Alpha - Q1 2024')."
        }

    def clean_name(self):
        name = self.cleaned_data.get('name')
        # Slugify ensures it's filename-safe, but we also want to replace hyphens with underscores
        # if your preference is for underscores in filenames, or just use slugify as is.
        slugged_name = slugify(name)
        # Example: if you prefer underscores instead of hyphens from slugify:
        # slugged_name = slugify(name).replace('-', '_') 
        if not name == slugged_name:
            raise forms.ValidationError(
                f"Operation name contains invalid characters. A suggested safe name is '{slugged_name}'. Please use only letters, numbers, hyphens, or underscores."
            )
        if Operation.objects.filter(name=name).exists():
             # This check is for create view. For update, we'd need to exclude the current instance.
             if not (self.instance and self.instance.name == name):
                raise forms.ValidationError("An operation with this name already exists.")
        return name

class TaskForm(forms.ModelForm):
    class Meta:
        model = Task
        fields = ['code', 'name', 'start_date', 'end_date']

    start_date = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "date"}))
    end_date = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "date"}))


class UserPreferencesForm(forms.ModelForm):
    class Meta:
        model = UserPreferences
        fields = ['timezone']


blank_choice = [ (None, '-----') ]

class EventBulkEditForm(forms.Form):
    tags = TomSelectMultipleChoiceField(
        label="Tag(s) to add",
        config=TomSelectConfig(
            url='event_tracker:tag-autocomplete',
            value_field='name',
            label_field='name',
            create_field='name',
            create=True,
            max_items=None,
        ),
        required=False
    )
    detected = forms.ChoiceField(label="Set all Detected to", choices=blank_choice + Event.DetectedChoices.choices, initial=None, required=False)
    prevented = forms.ChoiceField(label="Set all Prevented to", choices=blank_choice + Event.PreventedChoices.choices, initial=None, required=False)


class EventForm(forms.ModelForm):
    class Meta:
        model = Event
        exclude = ('starred',)

    task = forms.ModelChoiceField(Task.objects)
    timestamp = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    timestamp_end = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}), required=False)
    operator = forms.ModelChoiceField(User.objects)
    mitre_attack_tactic = forms.ModelChoiceField(AttackTactic.objects.all(), required=False, label="Tactic", widget=forms.Select(attrs={"hx-post": reverse_lazy("event_tracker:event-field-suggestions"), "hx-target": "#mitre-attack-suggestions", "hx-trigger": "input delay:200ms"}))
    mitre_attack_technique = forms.ModelChoiceField(AttackTechnique.objects.all(), required=False, label="Technique", widget=forms.Select(attrs={"hx-post": reverse_lazy("event_tracker:event-field-suggestions"), "hx-target": "#mitre-attack-suggestions", "hx-trigger": "input delay:200ms"}))
    mitre_attack_subtechnique = forms.ModelChoiceField(AttackSubTechnique.objects.all(), required=False, label="Subtechnique", widget=forms.Select(attrs={"hx-post": reverse_lazy("event_tracker:event-field-suggestions"), "hx-target": "#mitre-attack-suggestions", "hx-trigger": "input delay:200ms"}))
    
    # Define source and target without dynamic queryset initially
    source = forms.ModelChoiceField(queryset=Context.objects.none(), required=False, empty_label="New Source...", widget=autocomplete.ModelSelect2(url='event_tracker:context-autocomplete', attrs={"data-placeholder": "New Source...", "data-html": True, "data-theme":"bootstrap-5", "class": "clonable-dropdown"}))
    target = forms.ModelChoiceField(queryset=Context.objects.none(), required=False, empty_label="New Target...", widget=autocomplete.ModelSelect2(url='event_tracker:context-autocomplete', attrs={"data-placeholder": "New Target...", "data-html": True, "data-theme":"bootstrap-5", "class": "clonable-dropdown"}))
    
    description = forms.CharField(widget=forms.Textarea(attrs={"hx-post": reverse_lazy("event_tracker:event-field-suggestions"), "hx-target": "#mitre-attack-suggestions", "hx-trigger": "input delay:200ms"}))
    raw_evidence = forms.CharField(label="Raw Evidence", required=False, widget=forms.Textarea(attrs={'class': 'font-monospace', "spellcheck": "false", "hx-post": reverse_lazy("event_tracker:event-field-suggestions"), "hx-target": "#mitre-attack-suggestions", "hx-trigger": "input delay:500ms, load"}))
    source_user = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:user-list-autocomplete', attrs={'class': 'context-field user-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null", "data-language": "ss"}))
    source_host = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:host-list-autocomplete', attrs={'class': 'context-field host-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null", "data-language": "ss"}))
    source_process = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:process-list-autocomplete', attrs={'class': 'context-field process-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null", "data-language": "ss"}))
    target_user = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:user-list-autocomplete', attrs={'class': 'context-field user-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null", "data-language": "ss"}))
    target_host = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:host-list-autocomplete', attrs={'class': 'context-field host-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null", "data-language": "ss"}))
    target_process = forms.CharField(required=False, widget=autocomplete.ListSelect2(url='event_tracker:process-list-autocomplete', attrs={'class': 'context-field process-field', "data-theme": "bootstrap-5", "data-tags": "true", "data-token-separators": "null"}))

    tags = TomSelectMultipleChoiceField(
        label="Tags (Space, comma, or tab separated)",
        config=TomSelectConfig(
            url='event_tracker:tag-autocomplete',
            value_field='name',
            label_field='name',
            create_field='name',
            create=True,
            max_items=None,
        ),
        required=False
    )
    note = forms.CharField(label="Note (optional)", required=False, widget=forms.Textarea(attrs={'rows':1, 'cols':40}))

    def __init__(self, *args, **kwargs):
        # The request object might be passed in kwargs by the view if needed for context
        # For now, we assume get_context_queryset() can determine the active DB
        super().__init__(*args, **kwargs)
        active_op_queryset = get_context_queryset()
        self.fields['source'].queryset = active_op_queryset
        self.fields['target'].queryset = active_op_queryset
        # Also, ensure MITRE fields use .all() if not already
        self.fields['mitre_attack_tactic'].queryset = AttackTactic.objects.all()
        self.fields['mitre_attack_technique'].queryset = AttackTechnique.objects.all()
        self.fields['mitre_attack_subtechnique'].queryset = AttackSubTechnique.objects.all()
        # Task queryset should also be dynamic if tasks are per-DB, or ensure it uses active_op_db
        # If Task model is routed to active_op_db, Task.objects will use it.
        # If it's more complex, might need: self.fields['task'].queryset = Task.objects.using('active_op_db').all()
        # For now, assuming Task.objects correctly uses the active_op_db due to routing.

class LimitedEventForm(EventForm):
    def __init__(self, *args, **kwargs):
        super(LimitedEventForm, self).__init__(*args, **kwargs)
        self.fields['description'].disabled = True
        self.fields['source_user'].disabled = True
        # Add other fields from EventForm that need to be disabled
        self.fields['task'].disabled = True
        self.fields['timestamp'].disabled = True
        self.fields['timestamp_end'].disabled = True
        self.fields['operator'].disabled = True
        self.fields['mitre_attack_tactic'].disabled = True
        self.fields['mitre_attack_technique'].disabled = True
        self.fields['mitre_attack_subtechnique'].disabled = True
        self.fields['source'].disabled = True
        self.fields['target'].disabled = True
        self.fields['raw_evidence'].disabled = True
        self.fields['source_host'].disabled = True
        self.fields['source_process'].disabled = True
        self.fields['target_user'].disabled = True
        self.fields['target_host'].disabled = True
        self.fields['target_process'].disabled = True
        self.fields['tags'].disabled = True
        self.fields['note'].disabled = True

class EventFilterForm(forms.Form):
    tactic = forms.ModelChoiceField(AttackTactic.objects, # This will use default DB due to router
                                    required=False,
                                    empty_label="All Tactics",
                                    widget=forms.Select(attrs={'class': 'form-select form-select-sm submit-on-change'}))
    starred = forms.BooleanField(required=False, widget=forms.CheckboxInput(attrs={'class': 'submit-on-change'}))
    tag = forms.ModelChoiceField(Tag.objects.all().order_by("name"), required=False, empty_label="All Tags", # Use Tag.objects.all() for ModelChoiceField
                                 widget=forms.Select(attrs={'class': 'form-select form-select-sm submit-on-change'}))
    search = forms.CharField(required=False, widget=forms.TextInput(attrs={'placeholder': 'Filter events...', 'class': 'form-control form-control-sm'}))
    timebox = forms.IntegerField(required=False, widget=forms.NumberInput(attrs={'placeholder': 'Last X days', 'class': 'form-control form-control-sm'}))

    class Media:
        js = ["scripts/ss-forms.js"]


class FileForm(forms.ModelForm):
    class Meta:
        model = File # Ensure File is imported
        fields = "__all__"


class FileDistributionForm(forms.ModelForm):
    class Meta:
        model = FileDistribution
        fields = "__all__"


class BeaconExclusionForm(forms.Form):
    exclusion_type = forms.ChoiceField(choices=[("id", "id"),
                                                ("user", "user"),
                                                ("computer", "computer"),
                                                ("process", "process"),
                                                ("internal", "internal"),
                                                ("external", "external")])
    beacon_id = forms.IntegerField()

class EventStreamUploadForm(forms.Form):
    file = forms.FileField(help_text="A text file containing an EventStream JSON blob per line", widget=forms.FileInput(attrs={'accept':'.json,text/json'}))

def get_context_queryset():
    return Context.objects.using('active_op_db').all()

class ImportOperationForm(forms.Form):
    name = forms.CharField(
        max_length=100,
        help_text="A unique name for the operation, used for the database filename. Should contain only letters, numbers, hyphens, or underscores (e.g., 'op_alpha_2024')."
    )
    display_name = forms.CharField(
        max_length=200,
        help_text="A user-friendly display name for the operation (e.g., 'Operation Alpha - Q1 2024')."
    )
    db_file = forms.FileField(
        help_text="Select a SQLite3 database file to import as the operation database."
    )

    def clean_name(self):
        name = self.cleaned_data.get('name')
        slugged_name = slugify(name)
        if not name == slugged_name:
            raise forms.ValidationError(
                f"Operation name contains invalid characters. A suggested safe name is '{slugged_name}'. Please use only letters, numbers, hyphens, or underscores."
            )
        if Operation.objects.filter(name=name).exists():
            raise forms.ValidationError("An operation with this name already exists.")
        return name

    def clean_db_file(self):
        db_file = self.cleaned_data.get('db_file')
        if db_file:
            if not db_file.name.endswith('.sqlite3'):
                raise forms.ValidationError("The uploaded file must be a SQLite3 database file (.sqlite3)")
            try:
                # Try to open the file as a SQLite database to validate it
                import sqlite3
                import tempfile
                
                # Handle both InMemoryUploadedFile and TemporaryUploadedFile
                if hasattr(db_file, 'temporary_file_path'):
                    file_path = db_file.temporary_file_path()
                else:
                    # For InMemoryUploadedFile, create a temporary file
                    with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                        for chunk in db_file.chunks():
                            temp_file.write(chunk)
                        file_path = temp_file.name
                
                try:
                    with sqlite3.connect(file_path) as conn:
                        conn.cursor().execute("SELECT 1")
                finally:
                    # Clean up the temporary file if we created one
                    if not hasattr(db_file, 'temporary_file_path'):
                        import os
                        os.unlink(file_path)
                        
            except sqlite3.Error as e:
                raise forms.ValidationError(f"Invalid SQLite database file: {str(e)}")
        return db_file
