"""Server feature switches; retired lab is disabled unless explicitly enabled."""
import os


def price_action_lab_enabled():
    value = os.environ.get('PRICE_ACTION_LAB_ENABLED', 'false').strip().lower()
    if value in ('', '0', 'false', 'no', 'off'):
        return False
    if value in ('1', 'true', 'yes', 'on'):
        return True
    raise RuntimeError('INVALID_PRICE_ACTION_LAB_ENABLED')
