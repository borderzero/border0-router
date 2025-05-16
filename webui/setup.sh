#!/usr/bin/env bash
set -e

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
echo "Downloading Border0 client CSS"
# Create directory for Border0 theme
BORDER0_DIR="static/border0"
mkdir -p "$BORDER0_DIR"
# Fetch the client page to extract CSS URL and download it as default (light) theme
css_path=$(curl -s https://client.border0.com | grep -oP '(?<=href=")/assets/index-[0-9a-f]+\.css' | head -1)
if [ -n "$css_path" ]; then
  curl -sSL https://client.border0.com${css_path} -o "$BORDER0_DIR/border0-light.css"
  echo "Downloaded Border0 light theme CSS to $BORDER0_DIR/border0-light.css"
else
  echo "Failed to detect Border0 CSS URL"
fi

echo "Setup complete. Activate the virtual environment with 'source venv/bin/activate' and run './webui' to start the server."
echo " just run ./run.sh to start the server."