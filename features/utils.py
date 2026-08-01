"""
Shared Utilities

Common helper functions used across multiple feature modules.
"""

import os
import time
import re
from datetime import datetime, timezone


# ============================================================================
# CACHE UTILITIES
# ============================================================================

def get_cache_key(user_id, *args):
    """
    Generate a cache key for user-specific data.
    
    Args:
        user_id: User's unique identifier
        *args: Additional components for the cache key
        
    Returns:
        str: Cache key string
    """
    components = [str(user_id)] + [str(arg) for arg in args]
    return '_'.join(components)


def is_cache_valid(cached_time, duration_seconds):
    """
    Check if cached data is still valid.
    
    Args:
        cached_time: Timestamp when data was cached
        duration_seconds: Cache validity duration in seconds
        
    Returns:
        bool: True if cache is valid, False otherwise
    """
    return (time.time() - cached_time) < duration_seconds


# ============================================================================
# DATA VALIDATION
# ============================================================================

def is_hex_identifier(value):
    """
    Check if a value is a hex identifier (but NOT a GUID).
    
    Args:
        value: String to check
        
    Returns:
        bool: True if hex identifier (excluding GUIDs), False otherwise
    """
    if not value or not isinstance(value, str):
        return False
    
    # Reject GUIDs (format: 8-4-4-4-12)
    if re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', value):
        return True  # It's a GUID, so it's a hex identifier we want to filter out
    
    # Check if it's a pure hex string (8+ chars, no dashes)
    if re.match(r'^[0-9a-fA-F]{8,}$', value):
        return True
    
    return False


def is_valid_guid(value):
    """
    Check if a value is a valid GUID.
    
    Args:
        value: String to check
        
    Returns:
        bool: True if valid GUID, False otherwise
    """
    if not value or not isinstance(value, str):
        return False
    
    guid_pattern = r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
    return bool(re.match(guid_pattern, value))


# ============================================================================
# ENVIRONMENT UTILITIES
# ============================================================================

def get_env_var(key, default=None):
    """
    Get environment variable with optional default.
    
    Args:
        key: Environment variable name
        default: Default value if not found
        
    Returns:
        str: Environment variable value or default
    """
    return os.getenv(key, default)


def is_production():
    """
    Check if running in production environment.
    
    Returns:
        bool: True if production, False otherwise
    """
    return bool(os.getenv('WEBSITE_HOSTNAME'))


# ============================================================================
# DATE/TIME UTILITIES
# ============================================================================

def get_current_timestamp():
    """
    Get current UTC timestamp in ISO format.
    
    Returns:
        str: ISO formatted timestamp
    """
    return datetime.now(timezone.utc).isoformat()


def format_date(date_str, format='%Y-%m-%d'):
    """
    Format a date string.
    
    Args:
        date_str: Date string to format
        format: Target format string
        
    Returns:
        str: Formatted date string
    """
    if not date_str:
        return None
    
    try:
        dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
        return dt.strftime(format)
    except:
        return date_str


# ============================================================================
# ERROR HANDLING
# ============================================================================

def create_error_response(error_message, status_code=500):
    """
    Create a standardized error response.
    
    Args:
        error_message: Error message string
        status_code: HTTP status code
        
    Returns:
        tuple: (dict, int) - JSON response and status code
    """
    return {
        'success': False,
        'error': str(error_message)
    }, status_code


def create_success_response(data):
    """
    Create a standardized success response.
    
    Args:
        data: Response data
        
    Returns:
        dict: JSON response
    """
    return {
        'success': True,
        **data
    }
