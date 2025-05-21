import os
import sys
from multiprocessing import Manager
import logging

from django.apps import AppConfig


class CobaltStrikeMonitorConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'cobalt_strike_monitor'

    def ready(self):
        logger = logging.getLogger(__name__)
        logger.info("Entering the ready method of CobaltStrikeMonitorConfig.")

        from . import signals

        logger.info("CobaltStrikeMonitorConfig ready method called.")

        if 'runserver' not in sys.argv:
            logger.info("Exiting ready method as 'runserver' is not in sys.argv.")
            return True

        if os.environ.get('RUN_MAIN'):
            logger.info("RUN_MAIN is set. Checking for active operation before initializing TeamServerPoller.")
            
            # Check for an active operation using the global CurrentOperation model
            from event_tracker.background_tasks import get_current_active_operation
            
            try:
                current_op = get_current_active_operation()
                if current_op:
                    logger.info(f"Found active operation '{current_op.name}'. Initializing TeamServerPoller.")
                    from .poll_team_server import TeamServerPoller
                    TeamServerPoller().initialise()
                else:
                    logger.info("No active operation found. TeamServerPoller will be initialized when an operation is activated.")
            except Exception as e:
                logger.error(f"Error checking for active operation: {e}", exc_info=True)
        else:
            logger.info("RUN_MAIN is not set. Skipping TeamServerPoller initialization.")
