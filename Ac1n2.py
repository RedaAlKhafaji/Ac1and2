import os
import sys
import json
import time
import logging
import threading
import requests
import urllib3
import boto3
from botocore.config import Config
from botocore import UNSIGNED
from http.server import BaseHTTPRequestHandler, HTTPServer

# Suppress SSL warnings in the Render logs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging to output directly to standard output
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# ==================== CONFIGURATION ====================
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJzc29JZCI6IjIxMjQ1ODI0NyIsImFwcElkIjoid3g2ZTFhZjNmYTg0ZmJlNTIzIiwibWFjIjoiZGVmYXVsdCIsImV4cGlyZWREYXRlIjoiMTc4MTE3Mzc5OCJ9.8DzjfzDlH2TmIs5U4-0ucKYcu9eIWKzz27Hiujp-3O6aXUz6-QA8wWEl7OHFIpQ0KccAyxWhm4G4PP2xbyjUtg"
SSO_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJvZmZsaW5lIjpmYWxzZSwicmVnaW9uIjoiU0ciLCJleHAiOjE3ODM3MjI1NDYsImlhdCI6MTc4MTEzMDU0Niwic2NhbkNvZGUiOm51bGwsInVzZXJuYW1lIjoiMjEyNDU4MjQ3In0.QQuxbnzJoZ_s4ncpSHX6jPA9y4_3V06EjfftN4ErWY0pnk0YPhM2a9Ow7Ix4-bEjlJyqUx9leU-OFI0Xbilt30gqlNiGBNFH2L-bGjBStNPotNh9YA0-aPMm5_9f5ZCxcKX6mHjfzG60FotBrmDZjIW7oqWb5OwoY__myvj_XZE"
APP_ID = "wx6e1af3fa84fbe523"

AC1_DEVICE_ID = "C-0JABFAAAI"
AC2_DEVICE_ID = "DfaxahFAAAE"

AC2_STATUS_URL = f"https://eu-api-prod.aws.tcljd.com/v1/thing/error/{AC2_DEVICE_ID}" 
LOAD_BALANCE_URL = "https://eu-api-prod.aws.tcljd.com/v1/auth/service/loadBalance"

HEADERS = {
    "user-agent": "Dart/3.4 (dart:io)",
    "appid": APP_ID,
    "accept": "application/json; charset=utf-8",
    "accesstoken": ACCESS_TOKEN,
    "accept-language": "en",
    "content-type": "application/json"
}
# =======================================================


# ==================== RENDER HACK SERVER ====================
class RenderHealthCheckServer(BaseHTTPRequestHandler):
    """Answers Render's port ping requests to keep the free Web Service alive."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"TCL AC Automation Web Service is active.")
    
    def log_message(self, format, *args):
        return

def run_health_check_server():
    """Runs the dummy web server on the port assigned by Render."""
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), RenderHealthCheckServer)
    logging.info(f"Render health-check web server started on port {port}")
    server.serve_forever()
# ============================================================


# ==================== AUTOMATION LOGIC ====================
def get_ac1_target_mode():
    """Polls AC 2 status and determines what generatorMode AC 1 should be set to."""
    try:
        response = requests.get(AC2_STATUS_URL, headers=HEADERS, timeout=10, verify=False)
        response.raise_for_status()
        
        data = response.json()
        data_str = json.dumps(data).replace(" ", "")
        
        if '"generatorMode":6' in data_str:
            return 0
        elif '"autoGeneratorMode":1' in data_str:
            return 2
            
        return 0
        
    except Exception as e:
        logging.error(f"Error checking AC 2 status: {e}")
        return None

def get_cognito_tokens():
    """Fetches fresh AWS Cognito credentials from the TCL loadBalance API."""
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

def set_ac1_state(target_mode):
    """Sends the command payload securely to AC 1 using AWS IoT Device Shadows."""
    try:
        # 1. Grab fresh Cognito identity keys from TCL
        cognito_id, cognito_token = get_cognito_tokens()
        
        # 2. Trade the TCL tokens for temporary AWS hardware credentials
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
        
        # 3. Connect directly to the AWS IoT Data Plane
        iot_client = boto3.client(
            'iot-data', 
            region_name='eu-central-1',
            endpoint_url='https://data.iot.eu-central-1.amazonaws.com',
            verify=False,
            aws_access_key_id=aws_creds['AccessKeyId'],
            aws_secret_access_key=aws_creds['SecretKey'],
            aws_session_token=aws_creds['SessionToken']
        )
        
        # 4. Construct the physical unit's desired shadow payload
        payload = {
            "state": {
                "desired": {
                    "generatorMode": target_mode
                }
            },
            "clientToken": f"mobile_{int(time.time() * 1000)}"
        }
        
        # 5. Push the change instantly using the specific restricted MQTT topic
        iot_client.publish(
            topic=f"$aws/things/{AC1_DEVICE_ID}/shadow/update",
            qos=1,
            payload=json.dumps(payload).encode('utf-8')
        )
        
        logging.info(f"Success! AC 1 commanded to Mode {target_mode} via AWS IoT Push.")
        return True 
        
    except Exception as e:
        logging.error(f"Error sending AWS command to AC 1: {e}")
        return False

def main():
    logging.info("Starting TCL AC Automation Script (AWS IoT Mode)...")
    
    logging.info("Initializing Render free-tier environment compatibility...")
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    logging.info(f"Monitoring AC 2 ({AC2_DEVICE_ID}) for power source changes...")
    last_known_state = None

    while True:
        target_mode = get_ac1_target_mode()
        
        if target_mode is not None and target_mode != last_known_state:
            if last_known_state is not None:
                logging.info("-" * 40)
                logging.info("POWER STATE CHANGE DETECTED!")
                logging.info("-" * 40)
            
            if target_mode == 2:
                logging.info(">>> AC 2 entered Auto Gen Mode. Switching AC 1 to Mode 2.")
            elif target_mode == 0:
                logging.info(">>> AC 2 entered Mode 6 or Grid Mode. Reverting AC 1 to Mode 0.")
                
            command_success = set_ac1_state(target_mode)
                
            if command_success:
                last_known_state = target_mode
            else:
                logging.warning("Command delivery failed. Retrying in 30 seconds.")
            
        time.sleep(30)

if __name__ == "__main__":
    main()
