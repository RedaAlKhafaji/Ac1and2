import os, json, time, logging, threading, requests, boto3, urllib3
from datetime import datetime, timezone, timedelta
from botocore.config import Config
from botocore import UNSIGNED
from http.server import BaseHTTPRequestHandler, HTTPServer
from tuya_connector import TuyaOpenAPI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- TUYA PLUG SETTINGS ---
TUYA_ACCESS_ID = "qs4anwqyckn79fdfj95f"
TUYA_ACCESS_SECRET = "bf921a8f1b9d40428977bb43e886fd1b"
TUYA_DEVICE_ID = "ebd5b133c4d8aa2376su12"          # The Grid Sensor Plug
TUYA_DEVICE_ID_2 = "ebb1453d5297cf2ec9naor"        # The Mirror Plug
TUYA_ENDPOINT = "https://openapi.tuyaus.com"       # Western America Data Center

# --- TCL AC SETTINGS ---
SSO = "eyJhbGciOiJSUzI1NiJ9.eyJvZmZsaW5lIjpmYWxzZSwicmVnaW9uIjoiU0ciLCJleHAiOjE3ODM5MzkxMzQsImlhdCI6MTc4MTM0NzEzNCwic2NhbkNvZGUiOm51bGwsInVzZXJuYW1lIjoiMjEyNDU4MjQ3In0.DlLdnc4hF6JOk-6RXP7TIdP8OPjpIZdMcdt6qw6iqKAxxoK5tvwJTjK0X6RxOkeVNagL1sX12VsrpMEE0Da3Gr_eyEQdtnPKmvSNBqHRYh0LhcpcCC4sQ_tIIZkJV61ZMKqnGKxShyaoWvaJyRzuroBqZPuEFQua6BVEhmDuVHQ"
AT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJzc29JZCI6IjIxMjQ1ODI0NyIsImFwcElkIjoid3g2ZTFhZjNmYTg0ZmJlNTIzIiwibWFjIjoiZGVmYXVsdCIsImV4cGlyZWREYXRlIjoiMTc4MTM0ODkzOCJ9.-wmsuNpkEpj0qoAtGRR8G7zpH1YHyTaQJ63ZK0O3hpBm7JRsJxe0mzBJ3CGywLTf8TzfyG8bavac5ERjmwKC1A"

AC1 = "C-0JABFAAAI" 
LOAD_BALANCE_URL = "https://eu-api-prod.aws.tcljd.com/v1/auth/service/loadBalance"
APP_ID = "wx6e1af3fa84fbe523"

# --- INTENTIONAL SHUTOFF DATES (YYYY-MM-DD) ---
SHUTOFF_DATES = [
    "2026-06-13", # السبت
    "2026-06-15", # الاثنين
    "2026-06-18", # الخميس
    "2026-06-21", # الأحد
    "2026-06-24", # الأربعاء
    "2026-06-28", # الأحد
    "2026-07-01", # الأربعاء
    "2026-07-04"  # السبت
]

class RenderHealthCheckServer(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers()
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def log_message(self, format, *args): return

def run_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), RenderHealthCheckServer).serve_forever()

class TCLCloud:
    def __init__(self): 
        self.iot = None

    def connect(self):
        headers = {"appid": APP_ID, "ssotoken": SSO, "accesstoken": AT}
        resp = requests.get(LOAD_BALANCE_URL, headers=headers, verify=False).json()
        data = resp["data"]
        
        cognito = boto3.client('cognito-identity', region_name='eu-central-1', verify=False, config=Config(signature_version=UNSIGNED))
        creds = cognito.get_credentials_for_identity(IdentityId=data["cognitoId"], Logins={'cognito-identity.amazonaws.com': data["cognitoToken"]})['Credentials']
        
        self.iot = boto3.client('iot-data', region_name='eu-central-1', endpoint_url='https://data.iot.eu-central-1.amazonaws.com', verify=False,
                               aws_access_key_id=creds['AccessKeyId'], aws_secret_access_key=creds['SecretKey'], aws_session_token=creds['SessionToken'])

    def set_mode(self, target):
        if not self.iot: return
        # Target 0 = Grid (Turbo ON). Target 2 = Generator (Turbo OFF).
        payload = json.dumps({"state": {"desired": {"generatorMode": target, "turbo": 1 if target == 0 else 0}}}).encode('utf-8')
        self.iot.publish(topic=f"$aws/things/{AC1}/shadow/update", qos=1, payload=payload)

def get_plug_status(openapi):
    try:
        response = openapi.get(f"/v1.0/devices/{TUYA_DEVICE_ID}")
        if response.get("success"):
            return response["result"].get("online", False)
        else:
            logging.error(f"Tuya API Error (Sensor): {response.get('msg')}")
            return False
    except Exception as e:
        logging.error(f"Failed to fetch Tuya sensor status: {e}")
        return False

def set_second_plug(openapi, turn_on):
    try:
        commands = {'commands': [{'code': 'switch_1', 'value': turn_on}]}
        response = openapi.post(f'/v1.0/devices/{TUYA_DEVICE_ID_2}/commands', commands)
        if not response.get("success"):
            logging.error(f"Tuya API Error (Mirror Plug): {response.get('msg')}")
    except Exception as e:
        logging.error(f"Failed to switch second plug: {e}")

def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    tcl_cloud = TCLCloud()
    tuya_api = TuyaOpenAPI(TUYA_ENDPOINT, TUYA_ACCESS_ID, TUYA_ACCESS_SECRET)
    tuya_api.connect()
    
    while True:
        try:
            if tcl_cloud.iot is None:
                tcl_cloud.connect()
                
            # 1. Get current time in Iraq (UTC + 3 hours)
            iraq_tz = timezone(timedelta(hours=3))
            now = datetime.now(iraq_tz)
            current_date_str = now.strftime("%Y-%m-%d")
            
            # 2. Check if today is a scheduled shutoff day
            is_schedule_active = False
            if current_date_str in SHUTOFF_DATES:
                # Convert current time to "minutes past midnight"
                current_minutes = now.hour * 60 + now.minute
                
                # 5:55 AM = 355 mins. 7:30 AM = 450 mins.
                start_window = 5 * 60 + 55
                end_window = 7 * 60 + 30
                
                if start_window <= current_minutes < end_window:
                    is_schedule_active = True
                    
            # 3. Get the physical plug status
            is_grid_online = get_plug_status(tuya_api)
            
            # 4. Decide the AC Mode & Second Plug State
            if is_schedule_active:
                target = 2
                logging.info(f"Schedule Override Active for {current_date_str} at {now.strftime('%H:%M')} -> AC to Gen, Plug 2 OFF")
                set_second_plug(tuya_api, False)
                
            elif is_grid_online:
                target = 0
                logging.info("Grid is ON -> AC to Grid, Plug 2 ON")
                set_second_plug(tuya_api, True)
                
            else:
                target = 2
                logging.info("Grid is OFF -> AC to Gen, Plug 2 OFF")
                set_second_plug(tuya_api, False)
            
            tcl_cloud.set_mode(target)
            
        except Exception as e:
            logging.error(f"Loop error: {e}")
            tcl_cloud.iot = None 
            try: tuya_api.connect()
            except: pass
            
        time.sleep(60)

if __name__ == "__main__":
    main()
