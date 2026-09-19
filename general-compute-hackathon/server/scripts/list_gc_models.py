"""Print org-enabled General Compute model IDs only."""

import os
import sys

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(override=True)

api_key = os.getenv("GENERAL_COMPUTE_API_KEY") or os.getenv("GENERALCOMPUTE_API_KEY")
base_url = os.getenv("GENERAL_COMPUTE_BASE_URL", "https://api.generalcompute.com/v1")
if not api_key:
    print("MISSING_KEY")
    sys.exit(1)

client = OpenAI(api_key=api_key, base_url=base_url)
try:
    models = client.models.list()
    ids = sorted({m.id for m in models.data})
    print("SOURCE=openai_models_list")
    print("COUNT=" + str(len(ids)))
    for mid in ids:
        print(mid)
except Exception as exc:
    print("OPENAI_LIST_FAIL=" + type(exc).__name__)

import httpx

try:
    r = httpx.post(
        "https://api.generalcompute.com/v1/models/list",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    print("POST_MODELS_LIST_HTTP=" + str(r.status_code))
    data = r.json()
    items = data.get("data") if isinstance(data, dict) else data
    if isinstance(items, list):
        ids = sorted({(i.get("id") or i.get("modelId") or "") for i in items if isinstance(i, dict)})
        print("POST_COUNT=" + str(len([x for x in ids if x])))
        for mid in ids:
            if mid:
                print(mid)
except Exception as exc:
    print("POST_FAIL=" + type(exc).__name__)
