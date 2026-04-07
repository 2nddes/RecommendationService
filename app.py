from __future__ import annotations

from app import create_app
from app.reco.online.runtime import get_settings

app = create_app(get_settings())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
