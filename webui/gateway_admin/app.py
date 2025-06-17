import os
from flask import Flask
from .config import Config
from .extensions import login_manager, csrf
from jinja2 import ChoiceLoader, FileSystemLoader

# Import blueprints
from .modules.auth.routes import auth_bp
from .modules.home.routes import home_bp
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
    # include admin config templates in Jinja search path
    admin_template_dir = os.path.join(os.path.dirname(__file__), 'templates')
    app.jinja_loader = ChoiceLoader([
        app.jinja_loader,
        FileSystemLoader(admin_template_dir),
    ])

    login_manager.init_app(app)
    csrf.init_app(app)
    # Serve Border0 client assets (fonts, icons)
    from flask import send_from_directory
    assets_folder = os.path.join(static_dir, 'border0', 'assets')
    @app.route('/assets/<path:filename>')
    def border0_asset(filename):
        return send_from_directory(assets_folder, filename)
    # Serve Border0 favicon
    @app.route('/favicon.ico')
    def favicon():
        return send_from_directory(os.path.join(static_dir, 'border0'), 'favicon.ico')

    # Register blueprints
    app.register_blueprint(auth_bp)
    app.register_blueprint(home_bp)
    # Wi-Fi config moved under LAN; disable standalone Wi-Fi blueprint
    # app.register_blueprint(wifi_bp)
    app.register_blueprint(wan_bp)
    app.register_blueprint(lan_bp)
    app.register_blueprint(vpn_bp)
    app.register_blueprint(stats_bp)

    return app
