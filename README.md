# Border0 Pi

Border0 Pi transforms a Raspberry Pi into a secure Wi-Fi gateway with a captive-portal setup and Border0 VPN integration.

## Gateway Admin Panel (Web UI)

The `webui` directory contains a Flask-based admin panel for configuring networking, managing Border0 VPN, and viewing system statistics.

### Prerequisites
- Python 3.8 or newer
- Git (to clone this repository)
- No manual download needed: `setup.sh` will fetch Bootstrap and Volt assets locally.

### Setup
1. Navigate to the `webui` directory:
   ```bash
   cd webui
   ```
2. Run the setup script to create a Python virtual environment, install dependencies, and fetch static assets locally:
   ```bash
   ./setup.sh
   ```
3. Activate the virtual environment:
   ```bash
   source venv/bin/activate
   ```

### Running the Web UI
With the virtual environment active, start the server:
```bash
./webui
```
The app will be available at `http://0.0.0.0:5000`.

After logging in, navigate to **Border0 VPN Config** in the sidebar. Follow these steps to set up VPN without shell access:
1. Enter your **Organization Name** (the org you use with Border0) and click **Save Organization**.
2. Click **Login to Border0**. The server will run the `border0 client login --org <org>` command in the background (it waits up to ~5 minutes). A new browser window/tab should open pointing to the Border0 authentication URL. If it doesn’t, click the provided link.
3. Complete authentication in the new window. The CLI process on the Pi will detect the completion and write the client token to `~/.border0/client_token`.
4. Back in the VPN Config page, refresh if needed. You should now see the **Install & Start VPN** button—click it to install the Border0 node and start the VPN tunnel.
5. Once complete, your Pi is connected to Border0 over VPN.
  
## Historical Metrics Collection

A background metrics collector service can record system metrics (CPU, memory, disk, network throughput) every minute and make 24h of history available in the Web UI.

### Setup
1. Copy the systemd unit into `/etc/systemd/system/`:
   ```bash
   sudo cp build/templates/border0-metrics.service /etc/systemd/system/
   ```
2. Install the collector script to the Web UI directory and make it executable:
   ```bash
   sudo cp webui/metrics_collector.py /opt/border0/webui/
   sudo chmod +x /opt/border0/webui/metrics_collector.py
   ```
3. Reload systemd, enable and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now border0-metrics.service
   ```
4. In the Gateway Admin Panel, go to **Statistics** to view live and historical data for the past 24 hours.
