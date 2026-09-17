from __future__ import annotations
import json
from v11_state import summary
print(json.dumps(summary(25), ensure_ascii=False, indent=2))
