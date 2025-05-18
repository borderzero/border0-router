#!/usr/bin/env bash
set -e

echo "Setting up webui... $(pwd) $0"

# Create Python virtual environment in webui/venv
if [ ! -d "venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Upgrade pip and install requirements
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r ../requirements.txt

echo "Downloading Bootstrap and Volt static assets..."
STATIC_DIR="static/volt"
mkdir -p "$STATIC_DIR/css" "$STATIC_DIR/js"
# Bootstrap CSS and JS
curl -sSL https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/css/bootstrap.min.css \
     -o "$STATIC_DIR/css/bootstrap.min.css"
curl -sSL https://cdn.jsdelivr.net/npm/bootstrap@5.2.3/dist/js/bootstrap.bundle.min.js \
     -o "$STATIC_DIR/js/bootstrap.bundle.min.js"
# Volt theme CSS and JS
curl -sSL https://raw.githubusercontent.com/themesberg/volt-bootstrap-5-dashboard/main/dist/css/volt.css \
     -o "$STATIC_DIR/css/volt.css"
curl -sSL https://raw.githubusercontent.com/themesberg/volt-bootstrap-5-dashboard/main/dist/js/volt.js \
     -o "$STATIC_DIR/js/volt.js"

echo "Setup complete. Activate the virtual environment with 'source venv/bin/activate' and run './run.sh' to start the server."