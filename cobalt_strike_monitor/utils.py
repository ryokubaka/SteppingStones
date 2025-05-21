from django.conf import settings
from django.db import connections
from event_tracker.models import Operation, CurrentOperation
import logging

logger = logging.getLogger(__name__)

def get_current_active_operation():
    """
    Get the current active operation and return its database configuration.
    Returns the Operation object if an operation is active,
    or None if no operation is active.
    """
    # Get the active operation from the CurrentOperation model
    current_op = CurrentOperation.get_current()
    if not current_op:
        logger.warning("No active operation found in CurrentOperation model.")
        return None

    try:
        # Return the operation object directly
        return current_op
    except Exception as e:
        logger.error(f"Error getting operation object: {e}")
        return None 