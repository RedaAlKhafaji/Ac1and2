import os, json, time, logging, threading, requests, boto3, urllib3
from botocore.config import Config
from botocore import UNSIGNED
from http.server import BaseHTTPRequestHandler, HTTPServer

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- HARDCODED TOKENS ---
SSO = "eyJhbGciOiJSUzI1NiJ9.eyJvZmZsaW5lIjpmYWxzZSwicmVnaW9uIjoiU0ciLCJleHAiOjE3ODM5MzkxMzQsImlhdCI6MTc4MTM0NzEzNCwic2NhbkNvZGUiOm51bGwsInVzZXJuYW1lIjoiMjEyNDU4MjQ3In0.DlLdnc4hF6JOk-6RXP7TIdP8OPjpIZdMcdt6qw6iqKAxxoK5tvwJTjK0X6RxOkeVNagL1sX12VsrpMEE0Da3Gr_eyEQdtnPKmvSNBqHRYh0LhcpcCC4sQ_tIIZkJV61ZMKqnGKxShyaoWvaJyRzuroBqZPuEFQua6BVEhmDuVHQ"
AT = "eyJ0eXAiOiJKV1QiLCJhbGciOiJFUzI1NiJ9.eyJzc29JZCI6IjIxMjQ1ODI0NyIsImFwcElkIjoid3g2ZTFhZjNmYTg0ZmJlNTIzIiwibWFjIjoiZGVmYXVsdCIsImV4cGlyZWREYXRlIjoiMTc4MTM0ODkzOCJ9.-wmsuNpkEpj0qoAtGRR8G7zpH1YHyTaQJ63ZK0O3hpBm7JRsJxe0mzBJ3CGywLTf8TzfyG8bavac5ERjmwKC1A"

AC1, AC2 = "C-0JABFAAAI", "DfaxahFAAAE"
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
        payload = json.dumps({"state": {"desired": {"generatorMode": target, "turbo": 1 if target == 0 else 0}}}).encode('utf-8')
        self.iot.publish(topic=f"$aws/things/{AC1}/shadow/update", qos=1, payload=payload)

    def get_ac2(self):
        if not self.iot: return {}
        shadow = self.iot.get_thing_shadow(thingName=AC2)
        return json.loads(shadow['payload'].read().decode('utf-8')).get("state", {}).get("reported", {})

def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()
    cloud = TCLCloud()
    
    while True:
        try:
            if cloud.iot is None:
                cloud.connect()
                
            ac2 = cloud.get_ac2()
            if not ac2:
                continue
                
            # Extract the critical variables
            power = int(ac2.get("powerSwitch", 0))
            power_source = int(ac2.get("powerSource", 0)) # 1 = Generator Active
            auto = int(ac2.get("autoGeneratorMode", 0))   # To log the specific level
            mode = int(ac2.get("generatorMode", 6))       # Manual mode fallback
            
            # The STRICT Hardware Logic
            if power == 0:
                target = 0
                logging.info("AC 2 is OFF -> Commanding AC 1 to 0 (Off)")
                
            elif power_source == 1:
                # The hardware explicitly detects it is running on the generator!
                target = 2
                logging.info(f"AC 2 is ON GENERATOR (Auto level: {auto}) -> Commanding AC 1 to 2")
                
            elif mode in [1, 2, 3]:
                # Fallback: Just in case you manually force it into a generator mode
                target = 2
                logging.info(f"AC 2 is manually forced to Gen Mode {mode} -> Commanding AC 1 to 2")
                
            else:
                # Unit is ON and powerSource is not 1 (meaning it's on Grid)
                target = 0
                logging.info("AC 2 is on GRID -> Commanding AC 1 to 0")
            
            cloud.set_mode(target)
            
        except Exception as e:
            logging.error(f"Loop error: {e}")
            cloud.iot = None 
            
        time.sleep(60)

if __name__ == "__main__":
    main()
