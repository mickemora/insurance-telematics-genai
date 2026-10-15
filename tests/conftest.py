import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "lambdas" / "generator"))
sys.path.insert(0, str(ROOT / "lambdas" / "scoring"))
sys.path.insert(0, str(ROOT / "lambdas" / "api"))
sys.path.insert(0, str(ROOT / "lambdas" / "claims"))
sys.path.insert(0, str(ROOT / "lambdas" / "shared" / "python"))

# Handler modules create boto3 clients at import time; give them a region.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
