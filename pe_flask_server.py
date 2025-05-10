import argparse
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import json
import logging
import os
import uuid
import hmac
from datetime import datetime
from jac_login import JACLogin
from pe_client import PEClient
from log import get_logger

app = Flask(__name__)
logging.basicConfig(filename='pe_access.log', level=logging.INFO)

# Load credentials
with open('credentials.json', 'r') as f:
    credentials = json.load(f)

# Initialize PEClient and JACLogin
jac_login = JACLogin(credentials['username'], credentials['password'])
pe_client = PEClient(jac_login)

# Access token
ACCESS_TOKEN = credentials.get('pe_access_token', str(uuid.uuid4()))

def get_client_ip():
    """Get client IP address"""
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr

@app.route('/run', methods=['POST'])
def running_checkin():
    """Handle running request"""
    token = request.headers.get('token')
    if not token or not hmac.compare_digest(token, ACCESS_TOKEN):
        app.logger.warning(f"{datetime.now()}: Unauthorized running attempt from {get_client_ip()}")
        return jsonify({"error": "Unauthorized"}), 401

    try:
        if not pe_client.login():
            raise Exception("Login failed")
        
        # Get UID to verify session
        uid = pe_client.get_uid()
        app.logger.info(f"User {uid} starting running")

        # Generate and upload result
        result_data = pe_client.example_result_data()
        response = pe_client.upload_result(result_data)
        
        if response.status_code == 200:
            app.logger.info(f"Running successful for user {uid}")
            return jsonify({
                "status": "success",
                "data": result_data
            }), 200
        else:
            raise Exception(f"Upload failed: {response.text}")
            
    except Exception as e:
        app.logger.error(f"Running failed: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/status', methods=['GET'])
def status():
    """Check service status"""
    try:
        logged_in = pe_client.validate_session()
        return jsonify({
            "status": "running",
            "authenticated": logged_in,
            "uid": pe_client.uid if logged_in else None
        }), 200
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

def refresh_session():
    """Refresh PE system session"""
    try:
        if not pe_client.login():
            app.logger.error("Session refresh failed")
        else:
            app.logger.info("Session refreshed successfully")
    except Exception as e:
        app.logger.error(f"Session refresh error: {e}")

# Schedule session refresh every 30 minutes
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
scheduler.add_job(
    refresh_session,
    trigger=CronTrigger(
        timezone="Asia/Shanghai",
        minute='0,30'
    )
)

@app.after_request
def log_request(response):
    """Log all requests"""
    app.logger.info(
        f'{datetime.now()}: {request.method} {request.path} '
        f'{response.status_code} from {get_client_ip()}'
    )
    return response

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run PE Flask app')
    parser.add_argument('-p', '--port', type=int, default=5001, help='Port number')
    
    pe_client.login()
    scheduler.start()
    args = parser.parse_args()
    app.run(debug=False, port=args.port)
