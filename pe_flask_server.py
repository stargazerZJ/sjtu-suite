import argparse
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import json
import logging
import os
import uuid
import hmac
from datetime import datetime, timedelta
from jac_login import JACLogin
from pe_client import PEClient, LocationType
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
def running():
    """Handle running request with customizable parameters"""
    token = request.headers.get('token')
    if not token or not hmac.compare_digest(token, ACCESS_TOKEN):
        app.logger.warning(f"{datetime.now()}: Unauthorized running attempt from {get_client_ip()}")
        return jsonify({"error": "Unauthorized"}), 401

    try:
        if not pe_client.login():
            raise Exception("Login failed")
        
        uid = pe_client.get_uid()
        app.logger.info(f"User {uid} starting running")
        
        data = request.json or {}
        
        run_time = None
        if 'run_time' in data:
            try:
                run_time = datetime.fromisoformat(data['run_time'].replace('Z', '+00:00'))
            except ValueError:
                app.logger.warning(f"Invalid run_time format: {data['run_time']}")
        
        points_num = 15000
        if 'distance' in data:
            points_num = int(data['distance'] * 7.5)
        
        response = pe_client.simulate_running(run_time=run_time, n=points_num)
        
        if response and response.status_code == 200:
            app.logger.info(f"Running simulation successful for user {uid}")
            return jsonify({
                "status": "success",
                "response": response.json() if hasattr(response, 'json') else response.text
            }), 200
        else:
            error_msg = response.text if response else "No response"
            raise Exception(f"Running simulation failed: {error_msg}")
            
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