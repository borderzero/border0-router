from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user, UserMixin
from ...config import Config
import subprocess
import datetime
from ...extensions import login_manager

auth_bp = Blueprint('auth', __name__)

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    if user_id == Config.ADMIN_USERNAME:
        return User(user_id)
    return None

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('home.index'))
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == Config.ADMIN_USERNAME and password == Config.ADMIN_PASSWORD:
            user = User(username)
            login_user(user)
            return redirect(request.args.get('next') or url_for('home.index'))
        flash('Invalid credentials', 'danger')
    return render_template('auth/login.html')

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('auth.login'))

@auth_bp.route('/reboot', methods=['GET', 'POST'])
@login_required
def reboot():
    """Show reboot page on GET; reboot the system on POST."""
    if request.method == 'POST':
        try:
            subprocess.Popen(['systemctl', 'reboot'])
            flash('Rebooting system...', 'info')
        except Exception as e:
            flash(f'Failed to reboot system: {e}', 'danger')
        return redirect(url_for('auth.reboot'))
    # GET: display uptime and reboot confirmation
    uptime_str = ''
    try:
        with open('/proc/uptime', 'r') as f:
            total_seconds = int(float(f.read().split()[0]))
        uptime_td = datetime.timedelta(seconds=total_seconds)
        uptime_str = str(uptime_td)
    except Exception:
        uptime_str = 'Unavailable'
    return render_template('auth/reboot.html', uptime=uptime_str)
