"""Common Flask server utilities for SJTU Suite."""
from flask import Flask, request
import logging
from datetime import datetime


def get_client_ip():
    """Get client IP address from request headers."""
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr


def create_app_with_logging(name: str, log_file: str = None):
    """
    Create a Flask app with basic logging configured.
    
    Args:
        name: Flask app name
        log_file: Optional log file path
        
    Returns:
        Flask: Configured Flask application
    """
    app = Flask(name)
    
    if log_file:
        logging.basicConfig(filename=log_file, level=logging.INFO)
    
    return app
