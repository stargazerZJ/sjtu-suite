"""Door opening daemon - Flask server for remote door control."""
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

from sjtusuite.auth import JACLogin
from sjtusuite.clients.door import DoorClient
from sjtusuite.core.config import get_project_root
from sjtusuite.core.credentials import credentials
from sjtusuite.servers.base import get_client_ip


app = Flask(__name__)
logging.basicConfig(filename='door_access.log', level=logging.INFO)

# Initialize DoorClient and JACLogin
jac_login = JACLogin(credentials.username, credentials.password)
door_client = DoorClient(credentials.room_id, jac_login)

# Your predefined token (ideally should be in Env Variable)
ACCESS_TOKEN = credentials.door_access_token or str(uuid.uuid4())


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
        if not door_client.login():
            app.logger.error(f"{datetime.now()}: Scheduled session refresh failed")
        else:
            app.logger.info(f"{datetime.now()}: Scheduled session refresh successful")
    except Exception as e:
        app.logger.error(f"{datetime.now()}: Scheduled session refresh failed: {e}")


@app.after_request
def log_request(response):
    if response.status_code == 404:
        app.logger.info(
            f'404: "{request.user_agent}" from IP {get_client_ip()}'
        )
    return response


# Schedule the session refresh every hour
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
cron_trigger = CronTrigger(timezone="Asia/Shanghai", minute='0,30')
scheduler.add_job(refresh_session, trigger=cron_trigger)


def main():
    parser = argparse.ArgumentParser(description='Run Door Flask app with specified port.')
    parser.add_argument('-p', '--port', type=int, default=5000, help='Port to listen on (default: 5000)')

    door_client.login()
    scheduler.start()
    args = parser.parse_args()
    app.run(debug=False, port=args.port)


if __name__ == '__main__':
    main()
