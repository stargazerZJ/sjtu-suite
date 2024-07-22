import argparse
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import json
import logging
import os
import uuid
import hmac
from datetime import datetime
from jac_login import JACLogin
from door_client import DoorClient


app = Flask(__name__)
logging.basicConfig(filename='access.log', level=logging.INFO)

# Load credentials
with open('credentials.json', 'r') as f:
    credentials = json.load(f)

# Initialize DoorClient and JACLogin
jac_login = JACLogin(credentials['username'], credentials['password'])
door_client = DoorClient(credentials['room_id'])

# Your predefined token (ideally should be in Env Variable)
# ACCESS_TOKEN = os.getenv('DOOR_ACCESS_TOKEN') or str(uuid.uuid4())
ACCESS_TOKEN = credentials['door_access_token']

# Function to get client IP
def get_client_ip():
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr

@app.route('/open', methods=['POST'])
def open_door():
    token = request.headers.get('token')
    if not token or not hmac.compare_digest(token, ACCESS_TOKEN):
        app.logger.warning(f"{datetime.now()}: Unauthorized access attempt from IP {get_client_ip()}")
        return jsonify({"error": "Unauthorized"}), 401

    if door_client.open_door():
        app.logger.info(f"{datetime.now()}: Door opened successfully by IP {get_client_ip()}")
        return jsonify({"status": "Door opened successfully"}), 200
    else:
        app.logger.error(f"{datetime.now()}: Failed to open the door")
        return jsonify({"error": "Failed to open the door"}), 500


def refresh_session():
    try:
        if not door_client.login(jac_login):
            app.logger.error(f"{datetime.now()}: Scheduled session refresh failed")
        else:
            app.logger.info(f"{datetime.now()}: Scheduled session refresh successful")
    except Exception as e:
        app.logger.error(f"{datetime.now()}: Scheduled session refresh failed: {e}")


# Schedule the session refresh every hour
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
scheduler.add_job(refresh_session, 'cron', minute=0)


# # Shut down the scheduler when exiting the app
# @app.teardown_appcontext
# def shutdown_scheduler(exception=None):
#     scheduler.shutdown()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run Flask app with specified port.')
    parser.add_argument('-p', '--port', type=int, default=5000, help='Port to listen on (default: 5000)')

    door_client.login(jac_login)
    scheduler.start()
    args = parser.parse_args()
    app.run(debug=False, port=args.port)