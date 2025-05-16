import os
from flask import Flask
from .config import Config
from .extensions import login_manager

# Import blueprints
from .modules.auth.routes import auth_bp
from .modules.home.routes import home_bp
from .modules.wifi.routes import wifi_bp
from .modules.wan.routes import wan_bp
from .modules.lan.routes import lan_bp
from .modules.vpn.routes import vpn_bp
from .modules.stats.routes import stats_bp

def create_app():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    static_dir = os.path.join(base_dir, 'static')
    template_dir = os.path.join(base_dir, 'templates')

    app = Flask(__name__, static_folder=static_dir, template_folder=template_dir)
    app.config.from_object(Config)

    login_manager.init_app(app)

    # Register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(home_bp)
    app.register_blueprint(wifi_bp)
    app.register_blueprint(wan_bp)
    app.register_blueprint(lan_bp)
    app.register_blueprint(vpn_bp)
    app.register_blueprint(stats_bp)

    return app
