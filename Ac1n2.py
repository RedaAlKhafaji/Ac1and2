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

# Suppress SSL warnings in the Render logs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# ==================== CONFIGURATION ====================
APP_ID = "wx6e1af3fa84fbe523"
SSO_TOKEN = r"EyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJzc29JZCI6IjIxMjQ1ODI0NyIsImFwcElkIjoid3g2ZTFhZjNmYTg0ZmJlNTIzIiwibWFjIjoiZGVmYXVsdCIsImV4cGlyZWREYXRlIjoiMTc4MTMxOTU4MCJ9.XSnjK7sgKUxdRHpQBFOP-UPHP5sAOyEi7HF8Oq3C-2S5jocXjLSQOUhcVyDk4tfgZU-LEJw9Xv-sTxgwxVvz-w"

AC1_DEVICE_ID = "C-0JABFAAAI"
AC2_DEVICE_ID = "DfaxahFAAAE"

LOAD_BALANCE_URL = "https://eu-api-prod.aws.tcljd.com/v1/auth/service/loadBalance"

# Define the GMT+3 Timezone for accurate scheduling
BAGHDAD_TZ = timezone(timedelta(hours=3))

# Exact exam calendar dates where the internet blackout occurs (YYYY-MM-DD)
OFFLINE_DATES = [
    "2026-06-13",  # Saturday
    "2026-06-15",  # Monday
    "2026-06-17",  # Wednesday
    "2026-06-20",  # Saturday
    "2026-06-22",  # Monday
    "2026-06-24",  # Wednesday
    "2026-06-27",  # Saturday
    "2026-06-29",  # Monday
    "2026-07-01",  # Wednesday
    "2026-07-04"   # Saturday
]
# =======================================================


class RenderHealthCheckServer(BaseHTTPRequestHandler):
    """Answers Render's port ping requests to keep the free Web Service alive."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"TCL AC Automation Web Service is active.")
        
    def do_HEAD(self):
        """Answers UptimeRobot's invisible pings to prevent 501 errors."""
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        
    def log_message(self, format, *args):
        return

def run_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), RenderHealthCheckServer)
    logging.info(f"Render health-check web server started on port {port}")
    server.serve_forever()


class TCLCloud:
    def __init__(self):
        self.iot_client = None
        self.credentials_expiry = 0

    def get_cognito_tokens(self):
        auth_headers = {
            "appid": APP_ID,
            "ssotoken": SSO_TOKEN,
            "user-agent": "Dart/3.4 (dart:io)",
            "accept-encoding": "gzip"
        }
        response = requests.get(LOAD_BALANCE_URL, headers=auth_headers, timeout=10, verify=False)
        response.raise_for_status()
        data = response.json()
        if "data" not in data or "cognitoId" not in data["data"]:
            raise ValueError("Failed to fetch Cognito Tokens. Check if SSO_TOKEN is correct and not expired.")
        return data["data"]["cognitoId"], data["data"]["cognitoToken"]

    def refresh_client(self):
        if self.iot_client and time.time() < self.credentials_expiry - 300:
            return self.iot_client
        
        logging.info("Authenticating with TCL and AWS Cognito...")
        cognito_id, cognito_token = self.get_cognito_tokens()
        
        cognito_client = boto3.client(
            'cognito-identity', 
            region_name='eu-central-1', 
            verify=False,
            config=Config(signature_version=UNSIGNED)
        )
        
        creds_resp = cognito_client.get_credentials_for_identity(
            IdentityId=cognito_id,
            Logins={'cognito-identity.amazonaws.com': cognito_token}
        )
        aws_creds = creds_resp['Credentials']
        
        self.iot_client = boto3.client(
            'iot-data', 
            region_name='eu-central-1',
            endpoint_url='https://data.iot.eu-central-1.amazonaws.com',
            verify=False,
            aws_access_key_id=aws_creds['AccessKeyId'],
            aws_secret_access_key=aws_creds['SecretKey'],
            aws_session_token=aws_creds['SessionToken']
        )
        
        self.credentials_expiry = time.time() + 3600
        logging.info("AWS IoT Data Plane connection established.")
        return self.iot_client

    def get_ac_state(self, device_id):
        try:
            client = self.refresh_client()
            response = client.get_thing_shadow(thingName=device_id)
            payload_dict = json.loads(response['payload'].read().decode('utf-8'))
            return payload_dict.get("state", {}).get("reported", {})
        except Exception as e:
            logging.error(f"Error reading shadow for {device_id}: {e}")
            if "Forbidden" in str(e) or "Expired" in str(e) or "Signature" in str(e):
                self.iot_client = None
            return None

    def set_ac_generator_mode(self, device_id, target_mode):
        try:
            client = self.refresh_client()
            
            desired_state = {
                "generatorMode": target_mode
            }
            
            if target_mode == 0:
                desired_state["turbo"] = 1
                desired_state["windSpeed"] = 6
                logging.info("Target Mode 0 detected. Adding Turbo and Max WindSpeed to payload.")
            else:
                desired_state["turbo"] = 0
            
            payload = {
                "state": {
                    "desired": desired_state
                },
                "clientToken": f"mobile_{int(time.time() * 1000)}"
            }
            
            client.publish(
                topic=f"$aws/things/{device_id}/shadow/update",
                qos=1,
                payload=json.dumps(payload).encode('utf-8')
            )
            return True
        except Exception as e:
            logging.error(f"Error sending command to {device_id}: {e}")
            if "Forbidden" in str(e) or "Expired" in str(e) or "Signature" in str(e):
                self.iot_client = None
            return False


def get_target_mode(ac2_state):
    try:
        gen_mode = int(ac2_state.get("generatorMode", 0))
        auto_gen_mode = int(ac2_state.get("autoGeneratorMode", 0))
    except (ValueError, TypeError):
        gen_mode = 0
        auto_gen_mode = 0
        
    # Rule 1: Manual configuration sets specific level restriction
    if gen_mode in [1, 2, 3]:
        return 2
        
    # Rule 2: Manual configuration is inactive, but smart cloud reporting flags active level restriction
    if (gen_mode == 0 or gen_mode == 6) and (auto_gen_mode in [1, 2, 3]):
        return 2
        
    # Rule 3: Clear power signature verified across both states
    return 0


def main():
    logging.info("Starting TCL AC Automation Script (Dual-Mode Cloud Evaluation)...")
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    cloud = TCLCloud()
    logging.info(f"Monitoring AC 2 ({AC2_DEVICE_ID}) to synchronize AC 1 ({AC1_DEVICE_ID})...")

    while True:
        try:
            # 1. Check local date and time (GMT+3)
            current_dt = datetime.now(BAGHDAD_TZ)
            now_time = current_dt.time()
            current_date_str = current_dt.strftime("%Y-%m-%d")
            
            # Window begins at 5:55 AM and ends at 7:30 AM
            offline_start = dt_time(5, 55)
            offline_end = dt_time(7, 30)
            
            # Trigger safety window only if the current date matches the list
            is_offline_window = (current_date_str in OFFLINE_DATES) and (offline_start <= now_time < offline_end)
            
            # 2. Fetch hardware states
            ac1_state = cloud.get_ac_state(AC1_DEVICE_ID)
            
            if ac1_state is not None:
                if is_offline_window:
                    target_mode = 2
                else:
                    ac2_state = cloud.get_ac_state(AC2_DEVICE_ID)
                    if ac2_state is not None:
                        target_mode = get_target_mode(ac2_state)
                    else:
                        target_mode = None
                
                # 3. Apply changes if there is a desync
                if target_mode is not None:
                    try:
                        current_mode = int(ac1_state.get("generatorMode", 0))
                    except (ValueError, TypeError):
                        current_mode = 0
                    
                    if current_mode != target_mode:
                        logging.info("-" * 40)
                        
                        if is_offline_window:
                            logging.info(f"EXAM BLACKOUT WINDOW DETECTED ({current_date_str}): Forcing AC 1 to Mode {target_mode} before network drops.")
                        else:
                            logging.info(f"DESYNC DETECTED: AC 2 evaluation requires Mode {target_mode}, but AC 1 is in Mode {current_mode}.")
                        
                        success = cloud.set_ac_generator_mode(AC1_DEVICE_ID, target_mode)
                        
                        if success:
                            logging.info(f"Success! AC 1 commanded to Mode {target_mode}.")
                        else:
                            logging.warning("Command delivery failed. Retrying next loop.")
                            
                        logging.info("-" * 40)
                        
        except Exception as e:
            logging.error(f"Unexpected error in main loop: {e}")
            
        time.sleep(30)

if __name__ == "__main__":
    main()
