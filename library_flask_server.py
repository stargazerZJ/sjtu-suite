from flask import Flask, request, jsonify
from log import get_logger
from library_seat_advanced_client import LibrarySeatAdvancedClient
from user_data_client import UserDataClient
import argparse
import logging
from datetime import datetime,timedelta
import hmac
import json
import os
import uuid
from jac_login import JACLogin
from flask_cors import CORS

app = Flask(__name__)
logging.basicConfig(filename='access.log', level=logging.DEBUG)

CORS(app)

# Load credentials
with open('credentials.json', 'r') as f:
    credentials = json.load(f)

# Initialize DoorClient and JACLogin
jac_login = JACLogin(credentials['username'], credentials['password'])
library_seat_client = LibrarySeatAdvancedClient(jac_login)

# Initialize UserDataClient
user_data_client = UserDataClient("userdata.json")

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
    try:
        start_time = toDate(start_str)
        end_time = toDate(end_str)
    except:
        app.logger.error(f"Invalid Time Format:start_str={start_str},end_str={end_str}")

    # do reservation
    status, reservation_id, message = library_seat_client.reserve_seat(seat_id,start_time,end_time)

    if status: # success
        app.logger.info(f"{datetime.now()}: Reserved successfully by IP {get_client_ip()}")
        return jsonify({"status":"Success","message":message}), 200
    else:
        app.logger.error(f"{datetime.now()}: Failed to reserve the seat")
        return jsonify({"status":"Failed","message":message}), 500

@app.route("/addtoplan",methods=['POST'])
def addToPlan():
    user_data_client.add_reservation(json.loads(request.data))
    return jsonify({"status":"Success","message":"已添加至抢座计划表"}),200

@app.route("/showplan",methods=['GET'])
def showPlan():
    tasks_with_id = user_data_client.getTasksWithId()
    return jsonify(tasks_with_id), 200


@app.route("/getzoneinfo",methods=['GET'])
def getZoneInfo():
    zone_info = library_seat_client.get_zone_info_with_parent()
    info_json = json.dumps(zone_info)
    return jsonify(info_json), 200

@app.route("/getseatpic",methods=['GET'])
def getSeatPic():
    """get the seat zone's url of the specific seat area"""
    zone = request.args.get('zone')
    try:
        response = library_seat_client.call_API(f"/ic-web/sysInfo?sysType=2&sysValue={zone}&sysKind=16")
        return ("https://libseat.sjtu.edu.cn/ic-web/" + response.data['content']),200
    except:
        return ("Error"),500
    
@app.route("/getseatmap",methods=['GET'])
def getSeatMap():
    zone = request.args.get('zone')
    nextday = request.args.get('nextday')
    try:
        date = (datetime.now()+timedelta(days=1)).strftime('%Y%m%d') if nextday and nextday!="false" else datetime.now().strftime('%Y%m%d') 
        response = library_seat_client.call_API(f"/ic-web/reserve?roomIds={zone}&resvDates={date}&sysKind=8")
        return ({"data":response.data}),200
    except:
        return ("error"),500


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run Flask app with specified port.')
    parser.add_argument('-p', '--port', type=int, default=5000, help='Port to listen on (default: 5000)')

    library_seat_client.login()
    # scheduler.start()
    args = parser.parse_args()
    app.run(debug=True, port=args.port)