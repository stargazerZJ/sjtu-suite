from flask import Flask, request, jsonify
from log import get_logger
from library_seat_advanced_client import LibrarySeatAdvancedClient
import argparse
import logging
from datetime import datetime,timedelta
import hmac
import json
import os
import uuid
from jac_login import JACLogin

app = Flask(__name__)
logging.basicConfig(filename='access.log', level=logging.INFO)

# Load credentials
with open('credentials.json', 'r') as f:
    credentials = json.load(f)

# Initialize DoorClient and JACLogin
jac_login = JACLogin(credentials['username'], credentials['password'])
library_seat_client = LibrarySeatAdvancedClient(jac_login)

# Your predefined token (ideally should be in Env Variable)
ACCESS_TOKEN = os.getenv('LIB_ACCESS_TOKEN') or str(uuid.uuid4())
# ACCESS_TOKEN = credentials['lib_access_token']


@app.route("/")
def hello_world():
    return "<p>Hello world</p>"

def get_client_ip():
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr

# Convert an ISOString to a Datetime object.
# Warning: Only support convertion to UTC+8, do not support other time zones.
def toDate(res:str)->datetime:
    format = "%Y-%m-%dT%H:%M:%SZ"
    date = datetime.strptime(res, format)
    # convert UTC time to UTC+8
    date += timedelta(hours=8)
    # print(date)
    return date

@app.route("/reserve",methods=['POST'])
def reserve_seat():
    token = request.headers.get('token')
    if not token or not hmac.compare_digest(token, ACCESS_TOKEN):
        app.logger.warning(f"{datetime.now()}: Unauthorized access attempt from IP {get_client_ip()}")
        return jsonify({"error": "Unauthorized"}), 401
    
    # parse request body
    request_body = json.loads(request.data)
    seat_id = request_body['seat_id']
    start_str = request_body['start_time']
    end_str = request_body['end_time']
    start_time = toDate(start_str)
    end_time = toDate(end_str)

    # do reservation
    status, reservation_id, message = library_seat_client.reserve_seat(seat_id,start_time,end_time)

    if status: # success
        app.logger.info(f"{datetime.now()}: Reserved successfully by IP {get_client_ip()}")
        return jsonify({"status":"Success","message":message}), 200
    else:
        app.logger.error(f"{datetime.now()}: Failed to reserve the seat")
        return jsonify({"status":"Failed","message":message}), 500


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run Flask app with specified port.')
    parser.add_argument('-p', '--port', type=int, default=5000, help='Port to listen on (default: 5000)')

    library_seat_client.login()
    # scheduler.start()
    args = parser.parse_args()
    app.run(debug=True, port=args.port)