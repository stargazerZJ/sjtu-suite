import json
import os
from log import get_logger
from typing import List

class reservationJson:
    seat_id:int
    start_str:str
    end_str:str



class UserDataClient:
    file_name:str
    full_data:json
    preferred_seats:List[int]
    reservation_tasks:List[reservationJson]

    def __init__(self,file_name_res:str):
        self.clearValue()
        self.file_name = file_name_res
        self.logger = get_logger("UserDataClient")
        self.load()

    # Only Used to Test.
    def setValue(self):
        self.preferred_seats = [12,13,14,15]
        self.reservation_tasks = [
        {
            "seat_id": 114514,
            "start_str": "startTime1",
            "end_str": "endTime1"
        },
        {
            "seat_id": 114515,
            "start_str": "startTime2",
            "end_str": "endTime2"
        }
        ]
        self.logger.debug(self.reservation_tasks)

    def clearValue(self):
        self.preferred_seats = []
        self.reservation_tasks = []

    def clearData(self):
        self.clearValue()
        self.save()

    def splitData(self):
        self.preferred_seats = self.full_data['preferred_seats']
        self.reservation_tasks = self.full_data['reservation_tasks']
        self.logger.debug(self.preferred_seats)
        self.logger.debug(self.reservation_tasks)

    def createFile(self):
    # Try to create a file if {file_name} doesn't exist. 
        try: 
            with open(self.file_name, 'w', encoding='utf-8') as f:
                f.write('{"preferred_seats":[],"reservation_tasks":[]}') 
            self.logger.info(f"File {self.file_name} created as an empty JSON file.")
        except:
            self.logger.error(f"File {self.file_name} does not exist. Creating file also failed.")
        return 

    def load(self):
        if not os.path.exists(self.file_name):
            self.createFile()
        else:
            try:
                with open(self.file_name, 'r', encoding='utf-8') as f:
                    self.full_data = json.load(f)
                    self.splitData()
            except json.JSONDecodeError:
                self.logger.error(f"Can't parse data from {self.file_name}.")
                return 
            except:
                self.logger.error(f"An Error just occured.")
                self.clearValue()
                return 
    
    def save(self):
        if not os.path.exists(self.file_name):
            self.createFile()
        with open(self.file_name, 'w', encoding='utf-8') as f:
            self.full_data = {"preferred_seats":self.preferred_seats,"reservation_tasks":self.reservation_tasks}
            json.dump(self.full_data, f, ensure_ascii=False, indent=4)  # 写入 JSON 数据，格式化输出
            print(f"Data written to {self.file_name} successfully.")

    def add_reservation(self,new_reservation:reservationJson):
        self.reservation_tasks.append(new_reservation)
        self.save()

    def getTasksWithId(self):
        new_json_array = [{"id": idx, **item} for idx, item in enumerate(self.reservation_tasks)]
        return new_json_array
    
    def deleteTask(self,id):
        if id >= len(self.reservation_tasks):
            return
        self.reservation_tasks.pop(id)
        self.save()


if __name__ == '__main__':

    file_name = "userdata.json"
    data_client = UserDataClient(file_name) 
    data_client.logger.setLevel("DEBUG")

    # Only For test.Please delete it after debugging is done.
    test_reservation:reservationJson = {
            "seat_id": 114518,
            "start_str": "startTime3",
            "end_str": "endTime3"
        }
    
    test_getTasksWithId = False
    if(test_getTasksWithId):
        tasks_with_id = data_client.getTasksWithId()
        data_client.logger.debug(tasks_with_id)
    
    test_insert = False
    if(test_insert):
        data_client.add_reservation(test_reservation)
        tasks_with_id = data_client.getTasksWithId()
        data_client.logger.debug(tasks_with_id)
    
    test_delete = False
    if(test_delete):
        data_client.deleteTask(2)
        tasks_with_id = data_client.getTasksWithId()
        data_client.logger.debug(tasks_with_id)


    