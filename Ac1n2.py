import os, json, time, logging, threading, requests, boto3, urllib3
from datetime import datetime, time as dt_time, timezone, timedelta
from botocore.config import Config
from botocore import UNSIGNED
from http.server import BaseHTTPRequestHandler, HTTPServer

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')

APP_ID = "wx6e1af3fa84fbe523"
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN") # Stored in Render Environment
AC1, AC2 = "C-0JABFAAAI", "DfaxahFAAAE"
LOAD_BALANCE_URL = "https://eu-api-prod.aws.tcljd.com/v1/auth/service/loadBalance"

class TCLCloud:
    def __init__(self):
        self.sso_token = None
        self.iot_client = None

    def refresh_auth(self):
        logging.info("Auto-refreshing session...")
        # Exchange Refresh Token for new SSO_TOKEN via API
        headers = {"appid": APP_ID, "refreshtoken": REFRESH_TOKEN}
        resp = requests.post("https://eu-api-prod.aws.tcljd.com/v1/auth/login", json=headers, verify=False).json()
        self.sso_token = resp["data"]["ssoToken"]
        
        # Get IoT Credentials
        data = requests.get(LOAD_BALANCE_URL, headers={"appid": APP_ID, "ssotoken": self.sso_token}, verify=False).json()["data"]
        cognito = boto3.client('cognito-identity', region_name='eu-central-1', verify=False, config=Config(signature_version=UNSIGNED))
        creds = cognito.get_credentials_for_identity(IdentityId=data["cognitoId"], Logins={'cognito-identity.amazonaws.com': data["cognitoToken"]})['Credentials']
        
        self.iot_client = boto3.client('iot-data', region_name='eu-central-1', endpoint_url='https://data.iot.eu-central-1.amazonaws.com', verify=False,
                                       aws_access_key_id=creds['AccessKeyId'], aws_secret_access_key=creds['SecretKey'], aws_session_token=creds['SessionToken'])

    def get_state(self, device_id):
        try: return json.loads(self.iot_client.get_thing_shadow(thingName=device_id)['payload'].read().decode('utf-8'))["state"]["reported"]
        except: return {}

    def set_mode(self, target):
        self.iot_client.publish(topic=f"$aws/things/{AC1}/shadow/update", qos=1, 
                                payload=json.dumps({"state": {"desired": {"generatorMode": target, "turbo": 1 if target == 0 else 0}}}).encode('utf-8'))

def main():
    cloud = TCLCloud()
    cloud.refresh_auth()
    while True:
        try:
            ac2 = cloud.get_state(AC2)
            mode, auto = int(ac2.get("generatorMode", 6)), int(ac2.get("autoGeneratorMode", 0))
            
            # STRICT LOGIC: Only target 2 if it's explicitly a gen-level
            # 6 and 0 are considered "Grid". 1, 2, 3 are "Gen".
            target = 2 if (mode in [1, 2, 3] or auto in [1, 2, 3]) else 0
            
            ac1 = cloud.get_state(AC1)
            if int(ac1.get("generatorMode", 6)) != target:
                logging.info(f"Syncing: Target {target}")
                cloud.set_mode(target)
        except Exception as e: 
            logging.error(f"Error: {e}")
            cloud.refresh_auth() # Retry auth on failure
        time.sleep(60)

if __name__ == "__main__": main()
