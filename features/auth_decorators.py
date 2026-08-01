"""
Authentication Decorators

Contains the @login_required decorator and session management utilities.
"""

from flask import session, redirect, url_for, request
from functools import wraps


def login_required(f):
    """
    Decorator to require authentication for a route.
    
    Checks if user is logged in by verifying session contains user info.
    Redirects to login page if not authenticated.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            # Store the original URL they were trying to access
            session['next'] = request.url
            return redirect(url_for('auth_sso.login'))
        return f(*args, **kwargs)
    return decorated_function


def get_user_powerbi_token():
    """
    Get the user's Power BI access token from session.
    
    Returns:
        str: Access token or None if not available
    """
    return session.get('access_token')


def get_current_user():
    """
    Get current logged-in user information from session.
    
    Returns:
        dict: User info containing name, email, oid, etc.
    """
    return session.get('user', {})


def get_user_id():
    """
    Get current user's unique identifier (OID).
    
    Returns:
        str: User OID or None
    """
    user = get_current_user()
    return user.get('oid')


def is_authenticated():
    """
    Check if user is currently authenticated.
    
    Returns:
        bool: True if authenticated, False otherwise
    """
    return 'user' in session and 'access_token' in session
