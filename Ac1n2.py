import os, json, time, logging, threading, requests, boto3, urllib3
from botocore.config import Config
from botocore import UNSIGNED
from http.server import BaseHTTPRequestHandler, HTTPServer
from tuya_connector import TuyaOpenAPI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- TUYA PLUG SETTINGS (YOUR GRID SENSOR) ---
TUYA_ACCESS_ID = "qs4anwqyckn79fdfj95f"
TUYA_ACCESS_SECRET = "bf921a8f1b9d40428977bb43e886fd1b"
TUYA_DEVICE_ID = "ebd5b133c4d8aa2376su12"
TUYA_ENDPOINT = "https://openapi.tuyaus.com" # Western America Data Center


# --- TCL AC SETTINGS ---
SSO = "eyJhbGciOiJSUzI1NiJ9.eyJvZmZsaW5lIjpmYWxzZSwicmVnaW9uIjoiU0ciLCJleHAiOjE3ODM5MzkxMzQsImlhdCI6MTc4MTM0NzEzNCwic2NhbkNvZGUiOm51bGwsInVzZXJuYW1lIjoiMjEyNDU4MjQ3In0.DlLdnc4hF6JOk-6RXP7TIdP8OPjpIZdMcdt6qw6iqKAxxoK5tvwJTjK0X6RxOkeVNagL1sX12VsrpMEE0Da3Gr_eyEQdtnPKmvSNBqHRYh0LhcpcCC4sQ_tIIZkJV61ZMKqnGKxShyaoWvaJyRzuroBqZPuEFQua6BVEhmDuVHQ"
AT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJzc29JZCI6IjIxMjQ1ODI0NyIsImFwcElkIjoid3g2ZTFhZjNmYTg0ZmJlNTIzIiwibWFjIjoiZGVmYXVsdCIsImV4cGlyZWREYXRlIjoiMTc4MTM0ODkzOCJ9.-wmsuNpkEpj0qoAtGRR8G7zpH1YHyTaQJ63ZK0O3hpBm7JRsJxe0mzBJ3CGywLTf8TzfyG8bavac5ERjmwKC1A"

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
        # Target 0 = Grid (Turbo ON). Target 2 = Generator (Turbo OFF).
        payload = json.dumps({"state": {"desired": {"generatorMode": target, "turbo": 1 if target == 0 else 0}}}).encode('utf-8')
        self.iot.publish(topic=f"$aws/things/{AC1}/shadow/update", qos=1, payload=payload)

def get_plug_status(openapi):
    try:
        response = openapi.get(f"/v1.0/devices/{TUYA_DEVICE_ID}")
        if response.get("success"):
            return response["result"].get("online", False)
        else:
            logging.error(f"Tuya API Error: {response.get('msg')}")
            return False
    except Exception as e:
        logging.error(f"Failed to fetch Tuya status: {e}")
        return False

def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    tcl_cloud = TCLCloud()
    tuya_api = TuyaOpenAPI(TUYA_ENDPOINT, TUYA_ACCESS_ID, TUYA_ACCESS_SECRET)
    tuya_api.connect()
    
    while True:
        try:
            if tcl_cloud.iot is None:
                tcl_cloud.connect()
                
            is_grid_online = get_plug_status(tuya_api)
            
            if is_grid_online:
                target = 0
                logging.info("Grid is ON (Plug Online) -> Commanding AC 1 to Grid Mode + Turbo")
            else:
                target = 2
                logging.info("Grid is OFF (Plug Offline) -> Commanding AC 1 to Gen Mode (L2)")
            
            tcl_cloud.set_mode(target)
            
        except Exception as e:
            logging.error(f"Loop error: {e}")
            tcl_cloud.iot = None 
            try: tuya_api.connect()
            except: pass
            
        time.sleep(60)

if __name__ == "__main__":
    main()
