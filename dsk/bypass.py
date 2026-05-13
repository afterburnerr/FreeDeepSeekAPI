# dsk/bypass.py — spawn the Chrome-based bypass helper (dsk/server.py) and
# persist the resulting cookies to dsk/cookies.json. Called by DeepSeekAPI
# when it detects a CloudFlare / AWS WAF challenge page, and also by the
# repo's refresh_cookies.sh convenience script.
import subprocess
import os
import sys
import time
import requests
import json

def validate_cookies(cookies_data):
    """Validate that a clearance cookie is present and not empty.

    DeepSeek used Cloudflare (cf_clearance) in the past and has since moved
    to AWS WAF (aws-waf-token). Either is sufficient proof that we cleared
    the edge challenge.
    """
    cookies = cookies_data.get('cookies', {})
    for key in ('aws-waf-token', 'cf_clearance'):
        if key in cookies and cookies[key].strip() != '':
            return True
    return False

def get_and_save_cookies(server_url, cookie_file_path, max_retries=3):
    for attempt in range(max_retries):
        try:
            response = requests.get(server_url)
            response.raise_for_status()
            cookies_data = response.json()

            if not validate_cookies(cookies_data):
                print(f"Attempt {attempt + 1}: cf_clearance cookie not found, retrying...")
                time.sleep(5)
                continue

            cookies_to_save = {
                'cookies': cookies_data.get('cookies', {}),
                'user_agent': cookies_data.get('user_agent', '')
            }

            os.makedirs(os.path.dirname(cookie_file_path), exist_ok=True)
            with open(cookie_file_path, 'w', encoding='utf-8') as f:
                json.dump(cookies_to_save, f, indent=4, ensure_ascii=False)
            print("Successfully obtained and saved cookies with cf_clearance!")
            return True

        except requests.exceptions.ConnectionError as e:
            print(f"Connection error on attempt {attempt + 1}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(5)
            else:
                print("Max retries reached. Failed to get valid cookies.")
                return False

    print("Failed to obtain valid cf_clearance cookie after all attempts")
    return False

def run_server_background():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    server_script = os.path.abspath(os.path.join(script_dir, "server.py"))
    server_dir = os.path.dirname(server_script)

    os.makedirs(server_dir, exist_ok=True)

    try:
        process = subprocess.Popen(
            [sys.executable, server_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=server_dir,
            start_new_session=True
        )
        return process
    except Exception:
        return None

if __name__ == "__main__":
    print("Getting the cookies...")
    server_process = run_server_background()

    if server_process:
        # Increase initial wait time to ensure server is fully started
        time.sleep(10)
        server_url = "http://localhost:8000/cookies?url=https://chat.deepseek.com"
        cookie_file = "dsk/cookies.json"

        # Increase max retries for more reliability
        success = get_and_save_cookies(server_url, cookie_file, max_retries=5)

        if not success:
            print("Failed to obtain valid cookies.")
            server_process.terminate()
            sys.exit(1)
        server_process.terminate()
    else:
        print("Failed to start server.")
        sys.exit(1)