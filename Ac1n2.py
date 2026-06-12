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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# ==================== CONFIGURATION ====================
APP_ID = "wx6e1af3fa84fbe523"
SSO_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJvZmZsaW5lIjpmYWxzZSwicmVnaW9uIjoiU0ciLCJleHAiOjE3ODM3MjI1NDYsImlhdCI6MTc4MTEzMDU0Niwic2NhbkNvZGUiOm51bGwsInVzZXJuYW1lIjoiMjEyNDU4MjQ3In0.QQuxbnzJoZ_s4ncpSHX6jPA9y4_3V06EjfftN4ErWY0pnk0YPhM2a9Ow7Ix4-bEjlJyqUx9leU-OFI0Xbilt30gqlNiGBNFH2L-bGjBStNPotNh9YA0-aPMm5_9f5ZCxcKX6mHjfzG60FotBrmDZjIW7oqWb5OwoY__myvj_XZE"

AC1_DEVICE_ID = "C-0JABFAAAI"
AC2_DEVICE_ID = "DfaxahFAAAE"

LOAD_BALANCE_URL = "https://eu-api-prod.aws.tcljd.com/v1/auth/service/loadBalance"
# =======================================================


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
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), RenderHealthCheckServer)
    logging.info(f"Render health-check web server started on port {port}")
    server.serve_forever()


class TCLCloud:
    def __init__(self):
        self.iot_client = None
        self.credentials_expiry = 0

    def get_cognito_tokens(self):
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

    def refresh_client(self):
        """Ensures the AWS IoT Data Plane connection is active and valid."""
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
        """Pulls the exact, live hardware state directly from Amazon."""
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
        """Sends the command payload securely to the physical unit."""
        try:
            client = self.refresh_client()
            
            desired_state = {
                "generatorMode": target_mode
            }
            
            # If switching to National Grid (Mode 0), activate Turbo and max fan speed
            if target_mode == 0:
                desired_state["turbo"] = 1
                desired_state["windSpeed"] = 6
                logging.info(f"Target Mode 0 detected. Adding Turbo and Max WindSpeed to payload.")
            else:
                # If switching to Generator (Mode 2), ensure Turbo is disabled to save power
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
    """Calculates what AC 1 should be set to strictly based on AC 2's live power telemetry."""
    try:
        gen_mode = int(ac2_state.get("generatorMode", 0))
    except (ValueError, TypeError):
        gen_mode = 0
    
    # If AC 2 is completely off Gen Mode (Grid power) or in Mode 6
    if gen_mode == 0 or gen_mode == 6:
        return 0
        
    # If AC 2 is actively running on the generator (Level 1, 2, or 3)
    elif gen_mode in [1, 2, 3]:
        return 2
        
    return 0


def main():
    logging.info("Starting TCL AC Automation Script (Full AWS Sync)...")
    threading.Thread(target=run_health_check_server, daemon=True).start()
    
    cloud = TCLCloud()
    logging.info(f"Monitoring AC 2 ({AC2_DEVICE_ID}) to synchronize AC 1 ({AC1_DEVICE_ID})...")

    while True:
        try:
            ac2_state = cloud.get_ac_state(AC2_DEVICE_ID)
            ac1_state = cloud.get_ac_state(AC1_DEVICE_ID)
            
            if ac2_state is not None and ac1_state is not None:
                target_mode = get_target_mode(ac2_state)
                
                try:
                    current_mode = int(ac1_state.get("generatorMode", 0))
                except (ValueError, TypeError):
                    current_mode = 0
                
                if current_mode != target_mode:
                    logging.info("-" * 40)
                    logging.info(f"DESYNC DETECTED: AC 2 wants Mode {target_mode}, but AC 1 is in Mode {current_mode}.")
                    logging.info(f"Diagnostic - AC 2 Live GenMode: {ac2_state.get('generatorMode', 'None')}")
                    logging.info(f"Applying Mode {target_mode} to AC 1...")
                    
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
