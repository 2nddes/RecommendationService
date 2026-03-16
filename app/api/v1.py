from __future__ import annotations

from flask import Blueprint

from app.api.v1_admin import admin_bp
from app.api.v1_rag import rag_bp
from app.api.v1_recommend import recommend_bp
from app.api.v1_search import search_bp

v1_bp = Blueprint("v1", __name__)

v1_bp.register_blueprint(recommend_bp)
v1_bp.register_blueprint(search_bp)
v1_bp.register_blueprint(admin_bp)
v1_bp.register_blueprint(rag_bp)
