import os, json, time, logging, threading, requests, boto3, urllib3, hashlib
from botocore.config import Config
from botocore import UNSIGNED
from http.server import BaseHTTPRequestHandler, HTTPServer
from tuya_connector import TuyaOpenAPI

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- TUYA PLUG SETTINGS (Secrets kept hardcoded) ---
TUYA_ACCESS_ID = "ewtcedjchygrv47mpx9v"
TUYA_ACCESS_SECRET = "fe5d5d91ccd741f5b1b8b0063b7b4abd"
TUYA_DEVICE_ID = "ebb1453d5297cf2ec9naor"          # Currently targeting 'freg' as the Grid Sensor
TUYA_ENDPOINT = "https://openapi.tuyaus.com"       # Western America Data Center

# --- TCL AC SETTINGS ---
TCL_EMAIL = os.environ.get("TCL_EMAIL")
TCL_PASSWORD = os.environ.get("TCL_PASSWORD")
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

def fetch_tcl_tokens(email, password):
    pw_md5 = hashlib.md5(password.encode("utf-8")).hexdigest()
    headers = {
        "th_platform": "android",
        "th_version": "4.8.1",
        "th_appbulid": "830",
        "user-agent": "Android",
        "content-type": "application/json; charset=UTF-8",
    }
    
    login_payload = {
        "equipment": 2, "password": pw_md5, "osType": 1, "username": email,
        "clientVersion": "4.8.1", "osVersion": "6.0", "deviceModel": "Android",
        "captchaRule": 2, "channel": "app"
    }
    login_resp = requests.post("https://pa.account.tcl.com/account/login?clientId=54148614", json=login_payload, headers=headers, verify=False).json()
    if login_resp.get("status") != 1:
        raise RuntimeError(f"TCL Login Failed: {login_resp}")
        
    sso_token = login_resp.get("token")
    user_id = login_resp.get("user", {}).get("username")
    if not user_id:
        raise RuntimeError(f"Failed to extract numeric user_id: {login_resp}")
        
    urls_payload = {"ssoId": user_id, "ssoToken": sso_token}
    urls_resp = requests.post("https://prod-center.aws.tcljd.com/v3/global/cloud_url_get", json=urls_payload, headers=headers, verify=False).json()
    if "data" not in urls_resp or "cloud_url" not in urls_resp.get("data", {}):
        raise RuntimeError(f"Failed to fetch cloud_url: {urls_resp}")
    cloud_url = urls_resp["data"]["cloud_url"]
    
    ref_payload = {"userId": user_id, "ssoToken": sso_token, "appId": APP_ID}
    ref_resp = requests.post(f"{cloud_url}/v3/auth/refresh_tokens", json=ref_payload, headers=headers, verify=False).json()
    if "data" not in ref_resp or "saasToken" not in ref_resp.get("data", {}):
        raise RuntimeError(f"Failed to fetch saasToken: {ref_resp}")
        
    return sso_token, ref_resp["data"]["saasToken"]

class TCLCloud:
    def __init__(self): 
        self.iot = None
        self.creds_time = 0
        self.sso_token = None
        self.at_token = None

    def connect(self):
        logging.info("Connecting to TCL AWS IoT...")
        if not TCL_EMAIL or not TCL_PASSWORD:
            raise RuntimeError("Missing TCL_EMAIL or TCL_PASSWORD in Render Environment Variables.")
            
        if not self.sso_token or not self.at_token:
            self.sso_token, self.at_token = fetch_tcl_tokens(TCL_EMAIL, TCL_PASSWORD)
            
        headers = {"appid": APP_ID, "ssotoken": self.sso_token, "accesstoken": self.at_token}
        resp = requests.get(LOAD_BALANCE_URL, headers=headers, verify=False).json()
        
        # Invalidate tokens and retry once if rejected
        if "data" not in resp:
            logging.warning("TCL tokens expired during load balancing. Re-authenticating...")
            self.sso_token, self.at_token = fetch_tcl_tokens(TCL_EMAIL, TCL_PASSWORD)
            headers.update({"ssotoken": self.sso_token, "accesstoken": self.at_token})
            resp = requests.get(LOAD_BALANCE_URL, headers=headers, verify=False).json()
            
        data = resp["data"]
        cognito = boto3.client('cognito-identity', region_name='eu-central-1', verify=False, config=Config(signature_version=UNSIGNED))
        creds = cognito.get_credentials_for_identity(
            IdentityId=data["cognitoId"], 
            Logins={'cognito-identity.amazonaws.com': data["cognitoToken"]}
        )['Credentials']
        
        self.iot = boto3.client(
            'iot-data', region_name='eu-central-1', 
            endpoint_url='https://data.iot.eu-central-1.amazonaws.com', verify=False,
            aws_access_key_id=creds['AccessKeyId'], 
            aws_secret_access_key=creds['SecretKey'], 
            aws_session_token=creds['SessionToken']
        )
        self.creds_time = time.time()

    def set_mode(self, target):
        # Refresh AWS credentials proactively if older than 50 minutes
        if not self.iot or (time.time() - self.creds_time > 3000):
            self.connect()
            
        turbo_state = 1 if target == 0 else 0
        
        # Base JSON payload structure
        desired_state = {
            "generatorMode": target, 
            "turbo": turbo_state
        }
        
        # Inject Fan Speed parameter when targeting Generator Mode
        if target == 2:
            desired_state["fanSpeed"] = "auto"
            
        payload = json.dumps({"state": {"desired": desired_state}}).encode('utf-8')
        self.iot.publish(topic=f"$aws/things/{AC1}/shadow/update", qos=1, payload=payload)

def get_plug_status(openapi):
    try:
        response = openapi.get(f"/v1.0/devices/{TUYA_DEVICE_ID}")
        if response.get("success"):
            result = response["result"]
            is_online = result.get("online", False)
            
            # Active Cache-Buster
            if is_online:
                ping_cmd = {'commands': [{'code': 'switch_1', 'value': True}]}
                ping_resp = openapi.post(f'/v1.0/devices/{TUYA_DEVICE_ID}/commands', ping_cmd)
                
                if not ping_resp.get("success"):
                    err_code = ping_resp.get("code")
                    err_msg = str(ping_resp.get("msg", "")).lower()
                    if err_code in [2001, 1106] or "offline" in err_msg:
                        is_online = False
                        logging.info(f"Cache-Buster: Confirmed physically OFFLINE ({err_msg})")
                    else:
                        logging.warning(f"Tuya ping error ({err_code}: {err_msg}) — ignoring to prevent false switch.")
                        return None

            logging.info(f"RAW TUYA DATA -> Name: '{result.get('name')}' | Online Status: {is_online}")
            return is_online
        else:
            logging.error(f"Tuya API Error (Sensor): {response.get('msg')}")
            return None 
    except Exception as e:
        logging.error(f"Failed to fetch Tuya sensor status: {e}")
        return None 

def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    tcl_cloud = TCLCloud()
    tuya_api = TuyaOpenAPI(TUYA_ENDPOINT, TUYA_ACCESS_ID, TUYA_ACCESS_SECRET)
    tuya_api.connect()
    
    last_grid_state = None 
    
    while True:
        try:
            if tcl_cloud.iot is None:
                tcl_cloud.connect()
                
            is_grid_online = get_plug_status(tuya_api)
            
            if is_grid_online is None:
                logging.warning("Grid status uncertain — holding current state.")
            else:
                if is_grid_online != last_grid_state:
                    if is_grid_online:
                        target = 0
                        logging.info("Grid is ON -> AC to Grid (Turbo Enabled)")
                    else:
                        target = 2
                        logging.info("Grid is OFF -> AC to Gen L2 (Turbo Disabled, Auto Fan)")
                    
                    tcl_cloud.set_mode(target)
                    last_grid_state = is_grid_online 
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
