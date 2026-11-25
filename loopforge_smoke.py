"""Paste this file into a LoopForge Python script node (no bound Git repo)."""
import json
import os
import time
import urllib.request
import urllib.error
from uuid import uuid4

base = os.environ['UNITY_WORKER_URL'].rstrip('/')
token = os.environ['UNITY_WORKER_TOKEN']
job_id = str(uuid4())
max_wait = int(os.environ.get('UNITY_WORKER_WAIT_SECONDS', '1900'))


def request(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(base + path, data=data,
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    # No automatic proxy or redirects: keep the credential on the intended local service.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(req, timeout=15) as response:
        return json.load(response)


print('Unity job ID:', job_id, flush=True)
# Retrying the same submission ID prevents duplicate jobs after a lost response.
for attempt in range(3):
    try:
        request('/jobs', {'job_id': job_id})
        break
    except urllib.error.HTTPError:
        raise
    except (OSError, urllib.error.URLError):
        if attempt == 2:
            raise
        time.sleep(2)

deadline = time.monotonic() + max_wait
while time.monotonic() < deadline:
    try:
        result = request('/jobs/' + job_id)
    except urllib.error.HTTPError:
        raise
    except (OSError, urllib.error.URLError):
        time.sleep(5)
        continue
    if result['status'] != 'running':
        # Keep the short log tail in SOP output; the full log stays on Windows.
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        with open('unity-result.json', 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        raise SystemExit(0 if result['status'] == 'succeeded' else 1)
    print('Unity is running:', job_id, flush=True)
    time.sleep(5)
raise SystemExit('Polling timed out; inspect Windows job before retrying: ' + job_id)
