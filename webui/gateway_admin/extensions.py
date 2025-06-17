from flask_login import LoginManager
from flask_wtf import CSRFProtect

login_manager = LoginManager()
login_manager.login_view = 'auth.login'

csrf = CSRFProtect()
