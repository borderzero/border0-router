from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required
import os
import psutil
import time
import json
from ...config import Config

stats_bp = Blueprint('stats', __name__, url_prefix='/stats')
 
# Store last network counters for throughput calculation
last_net = {}

@stats_bp.route('/')
@login_required
def index():
    return render_template('stats/index.html')
 
@stats_bp.route('/data')
@login_required
def data():
    """Return system metrics as JSON for Chart.js polling."""
    now = time.time()
    # Network throughput calculation
    net = psutil.net_io_counters()
    if not last_net:
        sent_rate = 0.0
        recv_rate = 0.0
    else:
        delta = now - last_net.get('time', now)
        dt = delta if delta > 0 else 1
        sent_rate = (net.bytes_sent - last_net['bytes_sent']) / dt
        recv_rate = (net.bytes_recv - last_net['bytes_recv']) / dt
    # Update last counters
    last_net['bytes_sent'] = net.bytes_sent
    last_net['bytes_recv'] = net.bytes_recv
    last_net['time'] = now

    # CPU, memory, disk usage
    cpu = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage('/').percent

    # Prepare record for response and logging
    record = {
        'time': int(now * 1000),
        'cpu': cpu,
        'memory': memory,
        'disk': disk,
        'net_sent': sent_rate,
        'net_recv': recv_rate
    }
    # Append to metrics log for historical data
    try:
        log_path = Config.METRICS_LOG_PATH
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass
    return jsonify(record)
    
@stats_bp.route('/history')
@login_required
def history():
    """Return historical metrics for the past X hours."""
    # hours parameter (in hours), default to 24
    hours = request.args.get('hours', type=float, default=24.0)
    cutoff = int((time.time() - hours * 3600) * 1000)
    data = []
    try:
        with open(Config.METRICS_LOG_PATH, 'r') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get('time', 0) >= cutoff:
                    data.append(rec)
    except FileNotFoundError:
        pass
    return jsonify(data)
