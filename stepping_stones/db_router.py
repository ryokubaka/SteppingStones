class OperationRouter:
    """
    A router to control all database operations on models in the
    event_tracker application, and other core Django applications.
    """

    # Models that should go to the 'default' database (shared data)
    shared_apps = ('django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions', 'reversion',
                   'background_task',  # Added for background tasks
                   'mitre_attack'      # Added for MITRE ATT&CK data
                  )
    shared_models = {'operation', 'userpreferences', 'attacktactic', 'attacktechnique', 'attacksubtechnique',
                     'credential', 'bloodhoundserver', 'currentoperation'}

    # Apps whose models should go to the 'active_op_db' (operation-specific data)
    op_specific_apps = ('cobalt_strike_monitor', 'taggit') 
    # Add any other apps that are purely operation-specific
    
    # Models within event_tracker that are shared (use 'default' DB)
    shared_event_tracker_models = {'operation', 'userpreferences', 'bloodhoundserver', 'currentoperation', 'ldapsettings'}

    def db_for_read(self, model, **hints):
        app_label = model._meta.app_label
        model_name = model._meta.model_name

        if app_label == 'event_tracker' and model_name in self.shared_event_tracker_models:
            return 'default'
        if app_label in self.shared_apps:
            return 'default'
        # For op_specific_apps and non-shared event_tracker models, use active_op_db
        # This also implicitly handles 'taggit' correctly for op-specific tags
        if hasattr(hints.get('instance'), 'is_op_specific_instance'): # A marker you might add to instances
             return 'active_op_db'
        if app_label in self.op_specific_apps or (app_label == 'event_tracker' and model_name not in self.shared_event_tracker_models):
            return 'active_op_db'
        
        # Fallback for models not explicitly routed (e.g. third party apps not listed)
        # This could be None to let Django decide, or 'default' if that's safer.
        # If an active operation context exists, routing unknown apps to 'default' might be desired.
        # However, for safety and explicitness, unrouted usually means 'default'.
        # Consider if request.current_operation can be accessed here safely (might not be in all contexts)
        return 'default' # Default unrouted to 'default'

    def db_for_write(self, model, **hints):
        # Same logic as db_for_read for simplicity, but can be different
        return self.db_for_read(model, **hints)

    def allow_relation(self, obj1, obj2, **hints):
        # Allow relations if both objects are meant for the same database.
        db_obj1 = self.db_for_read(obj1.__class__, instance=obj1)
        db_obj2 = self.db_for_read(obj2.__class__, instance=obj2)
        
        # Special case: Allow relations between User objects and operation-specific models
        # This ensures that operation-specific models can reference users from the operation database
        if (obj1.__class__._meta.model_name == 'user' and db_obj2 == 'active_op_db') or \
           (obj2.__class__._meta.model_name == 'user' and db_obj1 == 'active_op_db'):
            return True
            
        # Special case: Allow relations between ContentType objects and operation-specific models
        # This ensures that operation-specific models can reference content types from the default database
        if (obj1.__class__._meta.model_name == 'contenttype' and db_obj2 == 'active_op_db') or \
           (obj2.__class__._meta.model_name == 'contenttype' and db_obj1 == 'active_op_db'):
            return True
        
        # Special case: Allow relations between Operation objects and operation-specific models
        # This ensures that operation-specific models (like Credential) can reference operations from the default database
        if (obj1.__class__._meta.model_name == 'operation' and db_obj2 == 'active_op_db') or \
           (obj2.__class__._meta.model_name == 'operation' and db_obj1 == 'active_op_db'):
            return True
            
        if db_obj1 and db_obj2:
            return db_obj1 == db_obj2
        # If one of the dbs couldn't be determined, Django's default is to deny relation.
        # Or, allow if one is None (e.g. a model not yet in a DB during form validation)
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == 'default': # 'default' database
            if app_label in self.shared_apps: # Includes background_task, mitre_attack
                return True 
            if app_label == 'event_tracker' and model_name in self.shared_event_tracker_models:
                return True 
            # Allow other apps not explicitly listed as op_specific or shared event_tracker models
            if app_label not in self.op_specific_apps and \
               not (app_label == 'event_tracker' and model_name not in self.shared_event_tracker_models):
                return True
            return False
        
        elif db == 'active_op_db': # Operation-specific database
            # Allow op-specific models from event_tracker
            if app_label == 'event_tracker' and model_name not in self.shared_event_tracker_models:
                return True
            # Allow models from fully op-specific apps
            if app_label in self.op_specific_apps:
                return True

            # Allow necessary tables from 'shared' Django apps in the active_op_db
            if app_label == 'contenttypes':  # Always allow contenttypes in operation database
                return True
            if app_label == 'auth':  # Allow all auth models including User
                return True
            if app_label == 'admin' and model_name == 'logentry':
                return True
            
            return False
        
        return None 