"""Write the API's OpenAPI spec to docs/openapi.json (e.g. to hand to a
frontend team/tool). Needs no database: the spec is generated from the code.

    python -m scripts.export_openapi
"""

import json
from pathlib import Path

from api.main import app

OUT = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"

if __name__ == "__main__":
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(app.openapi(), indent=2), encoding="utf-8")
    print(f"wrote {OUT}")
