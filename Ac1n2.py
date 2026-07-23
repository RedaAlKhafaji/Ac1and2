import os, json, time, logging, threading, requests, boto3, urllib3
from datetime import datetime, timezone, timedelta
from botocore.config import Config
from botocore import UNSIGNED
from http.server import BaseHTTPRequestHandler, HTTPServer
from tuya_connector import TuyaOpenAPI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- TUYA PLUG SETTINGS ---
TUYA_ACCESS_ID = "ewtcedjchygrv47mpx9v"
TUYA_ACCESS_SECRET = "fe5d5d91ccd741f5b1b8b0063b7b4abd"
TUYA_DEVICE_ID = "ebd5b133c4d8aa2376su12"          # The Grid Sensor Plug
TUYA_DEVICE_ID_2 = "ebb1453d5297cf2ec9naor"        # The Mirror Plug
TUYA_ENDPOINT = "https://openapi.tuyaus.com"       # Western America Data Center

# --- TCL AC SETTINGS ---
SSO = "eyJhbGciOiJSUzI1NiJ9.eyJvZmZsaW5lIjpmYWxzZSwicmVnaW9uIjoiU0ciLCJleHAiOjE3ODY1NTE2OTYsImlhdCI6MTc4Mzk1OTY5Niwic2NhbkNvZGUiOiJudWxsIiwidXNlcm5hbWUiOiIyMTI0NTgyNDcifQ.E-6sMzruZiBn1bK5kV87d_cu8ZtXgEzbtokh72Rp_6Ofz6SfyJ-t1JyOXTBlnYd7EMCt2ibrGGSm4Mf7gnbF9J_uZ5Rreq8S5NDUuZWs5oBSnbi7IS9C7bDgfGUP47ZNPDD9DKkSG81IU6J33xoTNylGkpcjXWyYQ17bWO1LdL4"
AT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJzc29JZCI6IjIxMjQ1ODI0NyIsImFwcElkIjoid3g2ZTFhZjNmYTg0ZmJlNTIzIiwibWFjIjoiZGVmYXVsdCIsImV4cGlyZWREYXRlIjoiMTc4Mzk2MTQ5NyJ9.U8ap2obAsedtu7IUMXO70shNAgr7y5MLgMuk1THyz-zVQ0ywBFXhEZ_hzafNvpKDsTwFE2uzgQ6Vtf6MEzcnYg"

AC1 = "C-0JABFAAAI" 
LOAD_BALANCE_URL = "https://eu-api-prod.aws.tcljd.com/v1/auth/service/loadBalance"
APP_ID = "wx6e1af3fa84fbe523"

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
        # Turbo mode forced to 1 (ON) always
        payload = json.dumps({"state": {"desired": {"generatorMode": target, "turbo": 1}}}).encode('utf-8')
        self.iot.publish(topic=f"$aws/things/{AC1}/shadow/update", qos=1, payload=payload)

def get_plug_status(openapi):
    try:
        response = openapi.get(f"/v1.0/devices/{TUYA_DEVICE_ID}")
        if response.get("success"):
            result = response["result"]
            is_online = result.get("online", False)
            logging.info(f"RAW TUYA DATA -> Name: '{result.get('name')}' | Online Status: {is_online}")
            return is_online
        else:
            logging.error(f"Tuya API Error (Sensor): {response.get('msg')}")
            return None # Changed to None to prevent false power cut reads
    except Exception as e:
        logging.error(f"Failed to fetch Tuya sensor status: {e}")
        return None # Changed to None

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
    
    last_grid_state = None # Tracks the power state to save API calls
    
    while True:
        try:
            if tcl_cloud.iot is None:
                tcl_cloud.connect()
                
            # 1. Get the physical plug status
            is_grid_online = get_plug_status(tuya_api)
            
            # 2. Decide the AC Mode & Second Plug State safely
            if is_grid_online is None:
                logging.warning("Grid status unknown this cycle — skipping action to avoid a false switch.")
            else:
                # Only send API commands if the power state has flipped
                if is_grid_online != last_grid_state:
                    if is_grid_online:
                        target = 0
                        logging.info("Grid is ON -> AC to Grid, Plug 2 ON")
                        set_second_plug(tuya_api, True)
                    else:
                        target = 3
                        logging.info("Grid is OFF -> AC to Gen (L3), Plug 2 OFF")
                        set_second_plug(tuya_api, False)
                    
                    tcl_cloud.set_mode(target)
                    last_grid_state = is_grid_online # Update the tracker
                else:
                    logging.info("Power state unchanged. Skipping redundant commands.")
            
        except Exception as e:
            logging.error(f"Loop error: {e}")
            tcl_cloud.iot = None 
            try: tuya_api.connect()
            except: pass
            
        time.sleep(120)

if __name__ == "__main__":
    main()
