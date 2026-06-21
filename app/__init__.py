from flask import Flask
from flask_jwt_extended import JWTManager
from config import Config
from app.db import init_db, close_db
from app.utils import detect_anomalies


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    JWTManager(app)

    init_db(app)

    from app.routes.auth_routes import auth_bp
    from app.routes.admin_routes import admin_bp
    from app.routes.reviewer_routes import reviewer_bp
    from app.routes.auditor_routes import auditor_bp
    from app.routes.query_routes import query_bp
    from app.routes.alert_routes import alert_bp
    from app.routes.review_appeal_routes import appeal_bp
    from app.routes.review_return_routes import return_bp
    from app.routes.supervision_routes import supervision_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(reviewer_bp)
    app.register_blueprint(auditor_bp)
    app.register_blueprint(query_bp)
    app.register_blueprint(alert_bp)
    app.register_blueprint(appeal_bp)
    app.register_blueprint(return_bp)
    app.register_blueprint(supervision_bp)

    app.teardown_appcontext(close_db)

    @app.before_request
    def before_req():
        pass

    @app.after_request
    def after_req(response):
        try:
            from flask import request
            if request.endpoint and 'detect' in request.path:
                from flask import g
                if 'db' in g:
                    detect_anomalies(g.db)
        except Exception:
            pass
        return response

    @app.route('/')
    def index():
        return {
            "app": "语言测评管理系统 API",
            "version": "1.0.0",
            "endpoints": {
                "auth": "/api/auth/*",
                "admin": "/api/admin/*",
                "reviewer": "/api/reviewer/*",
                "auditor": "/api/auditor/*",
                "query": "/api/query/*",
                "alerts": "/api/alerts/*",
                "returns": "/api/returns/*",
                "supervisions": "/api/supervisions/*"
            }
        }

    @app.errorhandler(404)
    def not_found(e):
        return {"error": "接口不存在"}, 404

    @app.errorhandler(500)
    def server_error(e):
        return {"error": "服务器内部错误", "detail": str(e)}, 500

    return app
