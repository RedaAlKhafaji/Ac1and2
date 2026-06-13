import os
import sys
import json
import time
import logging
import threading
import requests
import urllib3
import boto3
from datetime import datetime, time as dt_time, timezone, timedelta
from botocore.config import Config
from botocore import UNSIGNED
from http.server import BaseHTTPRequestHandler, HTTPServer

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.StreamHandler(sys.stdout)])

# ==================== CONFIGURATION ====================
APP_ID = "wx6e1af3fa84fbe523"
# Tokens extracted from your latest HAR capture
SSO_TOKEN = ".QQuxbnzJoZ_s4ncpSHX6jPA9y4_3V06EjfftN4ErWY0pnk0YPhM2a9Ow7Ix4-bEjlJyqUx9leU-OFI0Xbilt30gqlNiGBNFH2L-bGjBStNPotNh9YA0-aPMm5_9f5ZCxcKX6mHjfzG60FotBrmDZjIW7oqWb5OwoY__myvj_XZE"
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.-pCWb4Zo7A"

AC1_DEVICE_ID = "C-0JABFAAAI"
AC2_DEVICE_ID = "DfaxahFAAAE"

LOAD_BALANCE_URL = "https://eu-api-prod.aws.tcljd.com/v1/auth/service/loadBalance"
BAGHDAD_TZ = timezone(timedelta(hours=3))

OFFLINE_DATES = ["2026-06-13", "2026-06-15", "2026-06-17", "2026-06-20", "2026-06-22", "2026-06-24", "2026-06-27", "2026-06-29", "2026-07-01", "2026-07-04"]
# =======================================================

class RenderHealthCheckServer(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers()
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def log_message(self, format, *args): return

def run_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), RenderHealthCheckServer).serve_forever()

class TCLCloud:
    def __init__(self):
        self.iot_client = None
        self.credentials_expiry = 0

    def refresh_client(self):
        if self.iot_client and time.time() < self.credentials_expiry - 300:
            return self.iot_client
        
        logging.info("Authenticating with TCL...")
        headers = {"appid": APP_ID, "ssotoken": SSO_TOKEN, "accesstoken": ACCESS_TOKEN, "user-agent": "Dart/3.4 (dart:io)"}
        response = requests.get(LOAD_BALANCE_URL, headers=headers, timeout=15, verify=False)
        data = response.json().get("data", {})
        
        cognito_client = boto3.client('cognito-identity', region_name='eu-central-1', verify=False, config=Config(signature_version=UNSIGNED))
        creds = cognito_client.get_credentials_for_identity(IdentityId=data["cognitoId"], Logins={'cognito-identity.amazonaws.com': data["cognitoToken"]})['Credentials']
        
        self.iot_client = boto3.client('iot-data', region_name='eu-central-1', endpoint_url='https://data.iot.eu-central-1.amazonaws.com', verify=False,
                                       aws_access_key_id=creds['AccessKeyId'], aws_secret_access_key=creds['SecretKey'], aws_session_token=creds['SessionToken'])
        
        self.credentials_expiry = time.time() + 3600
        return self.iot_client

    def get_ac_state(self, device_id):
        try:
            return json.loads(self.refresh_client().get_thing_shadow(thingName=device_id)['payload'].read().decode('utf-8')).get("state", {}).get("reported", {})
        except Exception as e:
            logging.error(f"Shadow error: {e}"); return None

    def set_ac_generator_mode(self, device_id, target_mode):
        try:
            desired = {"generatorMode": target_mode, "turbo": 1 if target_mode == 0 else 0}
            if target_mode == 0: desired["windSpeed"] = 6
            self.refresh_client().publish(topic=f"$aws/things/{device_id}/shadow/update", qos=1, payload=json.dumps({"state": {"desired": desired}}).encode('utf-8'))
            return True
        except: return False

def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()
    cloud = TCLCloud()
    while True:
        try:
            now = datetime.now(BAGHDAD_TZ)
            is_blackout = (now.strftime("%Y-%m-%d") in OFFLINE_DATES) and (dt_time(5, 55) <= now.time() < dt_time(7, 30))
            
            ac1_state = cloud.get_ac_state(AC1_DEVICE_ID)
            if ac1_state:
                target = 2 if is_blackout else 0
                ac2 = cloud.get_ac_state(AC2_DEVICE_ID)
                if not is_blackout and ac2:
                    mode = int(ac2.get("generatorMode", 0))
                    auto = int(ac2.get("autoGeneratorMode", 0))
                    target = 2 if (mode in [1,2,3] or auto in [1,2,3]) else 0
                
                if int(ac1_state.get("generatorMode", 0)) != target:
                    cloud.set_ac_generator_mode(AC1_DEVICE_ID, target)
        except Exception as e: logging.error(f"Loop error: {e}")
        time.sleep(30)

if __name__ == "__main__": main()
