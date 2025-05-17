#!/usr/bin/env python3
"""
Simple metrics collector for Border0 Pi.
Runs as a systemd service to record system metrics every minute to a log file.
Each line in the log file is a JSON object with timestamp (ms) and metrics:
  cpu, memory, disk, net_sent, net_recv
"""
import time
import psutil
import json
import os

# Log file path; matches Config.METRICS_LOG_PATH in the web UI
LOG_FILE = '/var/lib/border0/metrics.log'
# Sampling interval in seconds (reduced to collect every 15 seconds)
INTERVAL = 15

def ensure_log_dir(path):
    dirpath = os.path.dirname(path)
    try:
        os.makedirs(dirpath, exist_ok=True)
    except Exception:
        pass

def main():
    # Prepare
    ensure_log_dir(LOG_FILE)
    prev_net = psutil.net_io_counters()
    # Warm up CPU percent
    psutil.cpu_percent(interval=None)

    while True:
        time.sleep(INTERVAL)
        now = int(time.time() * 1000)

        # Metrics
        cpu = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory().percent
        disk = psutil.disk_usage('/').percent
        net = psutil.net_io_counters()
        # Compute throughput
        sent_rate = (net.bytes_sent - prev_net.bytes_sent) / INTERVAL
        recv_rate = (net.bytes_recv - prev_net.bytes_recv) / INTERVAL
        prev_net = net

        record = {
            'time': now,
            'cpu': cpu,
            'memory': memory,
            'disk': disk,
            'net_sent': sent_rate,
            'net_recv': recv_rate
        }
        # Append to log
        try:
            with open(LOG_FILE, 'a') as f:
                f.write(json.dumps(record) + '\n')
        except Exception:
            pass

if __name__ == '__main__':
    main()