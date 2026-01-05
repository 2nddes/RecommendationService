from __future__ import annotations

from app import create_app

app = create_app()


if __name__ == "__main__":
    # 本地开发启动：生产环境建议用 gunicorn/uwsgi 等
    app.run(host="0.0.0.0", port=5000, debug=True)
