#!/bin/bash

RED='\033[0;31m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RESET='\033[0m'

echo ""
echo -e "${RED}██████╗ ██╗██████╗ ██████╗ ██╗   ██╗      ███████╗██████╗ ██╗    ██╗ █████╗ ██████╗ ██████╗ ███████╗${RESET}"
echo -e "${RED}██╔══██╗██║██╔══██╗██╔══██╗╚██╗ ██╔╝      ██╔════╝██╔══██╗██║    ██║██╔══██╗██╔══██╗██╔══██╗██╔════╝${RESET}"
echo -e "${RED}██████╔╝██║██████╔╝██║  ██║ ╚████╔╝ █████╗█████╗  ██║  ██║██║ █╗ ██║███████║██████╔╝██║  ██║███████╗${RESET}"
echo -e "${RED}██╔══██╗██║██╔══██╗██║  ██║  ╚██╔╝  ╚════╝██╔══╝  ██║  ██║██║███╗██║██╔══██║██╔══██╗██║  ██║╚════██║${RESET}"
echo -e "${RED}██████╔╝██║██║  ██║██████╔╝   ██║         ███████╗██████╔╝╚███╔███╔╝██║  ██║██║  ██║██████╔╝███████║${RESET}"
echo -e "${RED}╚═════╝ ╚═╝╚═╝  ╚═╝╚═════╝    ╚═╝         ╚══════╝╚═════╝  ╚══╝╚══╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝${RESET}"
echo ""
echo -e "${YELLOW}                        Infiltrate & Expose — Lite Edition${RESET}"
echo -e "${CYAN}                        Developed by Jeet Ganguly${RESET}"
echo -e "${CYAN}                        No LLM · No Docker · Local-First${RESET}"
echo ""

echo "🔍 Checking Python version..."
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -lt 3 ] || { [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -lt 10 ]; }; then
    echo -e "   ${RED}Python 3.10+ required. Found: $PYTHON_VERSION${RESET}"
    exit 1
fi
echo -e "   ${GREEN}Python $PYTHON_VERSION — OK${RESET}"
echo ""


echo "Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    python3-dev \
    python3-tk \
    xvfb \
    cmake \
    build-essential \
    libopenblas-dev \
    liblapack-dev \
    libx11-dev \
    libgtk-3-dev \
    libjpeg-dev \
    libpng-dev \
    curl \
    wget \
    git \
    > /dev/null 2>&1
echo -e "   ${GREEN}System dependencies installed${RESET}"
echo ""


echo "Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel -q
echo -e "   ${GREEN}Virtual environment ready (./venv)${RESET}"
echo ""


echo "Compiling dlib (takes 5–10 minutes — please wait)..."
pip install dlib
if [ $? -eq 0 ]; then
    echo -e "   ${GREEN}dlib compiled and installed${RESET}"
else
    echo -e "   ${RED}dlib failed — check build dependencies above${RESET}"
    echo "   Tip: make sure cmake and build-essential are installed."
    exit 1
fi
echo ""


echo "Installing face_recognition..."
pip install face_recognition -q
echo -e "   ${GREEN}face_recognition installed${RESET}"

echo "Installing face_recognition_models (from GitHub)..."
pip install git+https://github.com/ageitgey/face_recognition_models
if [ $? -eq 0 ]; then
    echo -e "   ${GREEN}face_recognition_models installed${RESET}"
else
    echo -e "   ${RED}face_recognition_models failed${RESET}"
    exit 1
fi
echo ""


echo "Installing Python packages..."
pip install -q \
    flask \
    seleniumbase \
    Pillow \
    numpy
echo -e "   ${GREEN}Flask, SeleniumBase, Pillow, NumPy installed${RESET}"
echo ""


echo "Installing Chrome driver (Undetected Chrome)..."
seleniumbase install chromedriver
echo -e "   ${GREEN}Chrome driver installed${RESET}"
echo ""


echo "Creating required directories..."
mkdir -p app/face_data
mkdir -p app/post_screenshots
echo -e "   ${GREEN}app/face_data/ created${RESET}"
echo -e "   ${GREEN}app/post_screenshots/ created${RESET}"
echo ""

echo "Patching face_recognition_models for Python 3.12 compatibility..."

python3 - << 'PYEOF'
import sys, os

path = os.path.join(
    sys.prefix, 'lib',
    f'python{sys.version_info.major}.{sys.version_info.minor}',
    'site-packages'
)
init = os.path.join(path, 'face_recognition_models', '__init__.py')

if not os.path.exists(init):
    print("   face_recognition_models not found — skipping patch")
    exit(0)

content = open(init).read()
if 'pkg_resources' not in content:
    print("   Already patched — skipping")
    exit(0)

new_content = '''import os as _os
_here = _os.path.dirname(_os.path.abspath(__file__))

def pose_predictor_model_location():
    return _os.path.join(_here, "models/shape_predictor_68_face_landmarks.dat")

def pose_predictor_five_point_model_location():
    return _os.path.join(_here, "models/shape_predictor_5_face_landmarks.dat")

def face_recognition_model_location():
    return _os.path.join(_here, "models/dlib_face_recognition_resnet_model_v1.dat")

def cnn_face_detector_model_location():
    return _os.path.join(_here, "models/mmod_human_face_detector.dat")
'''

open(init, 'w').write(new_content)
print(f"   Patched: {init}")
PYEOF

echo -e "   ${GREEN}face_recognition_models patched${RESET}"
echo ""


echo "Facebook Session Setup"
echo "   Birdy-Edwards Lite uses the Cookie-Editor extension for cookie import."
echo "   You do NOT need to run a Selenium login at setup time."
echo ""
echo "   After starting the app:"
echo "   1. Install Cookie-Editor in your browser:"
echo "      Chrome  → https://chromewebstore.google.com/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmd"
echo "      Firefox → https://addons.mozilla.org/en-US/firefox/addon/cookie-editor/"
echo "   2. Open Facebook and log in normally"
echo "   3. Click the Cookie-Editor icon → Export → Export as JSON"
echo "   4. Go to http://localhost:5000"
echo "   5. Paste the copied JSON into the IMPORT COOKIES box"
echo ""


echo "     Setup Complete — Birdy-Edwards Lite is ready!         "
echo ""
echo "  Stack:"
echo "  • SeleniumBase + Undetected Chrome"
echo "  • Face model: CNN / HOG via face_recognition + dlib"
echo "  • DB        : SQLite (local, no server needed)"
echo "  • LLM deps  : NONE"
echo ""
echo "  Next steps:"
echo "  1. source venv/bin/activate"
echo "  2. cd app && python3 app.py"
echo "  3. Open http://localhost:5000"
echo "  4. Export cookies via Cookie-Editor extension and paste into the home page"
echo "  5. Enter a Facebook profile URL and hit LAUNCH PIPELINE"
echo ""
echo -e "  ${YELLOW}Always activate venv before running:${RESET}"
echo "     source venv/bin/activate"
echo ""
echo -e "  ${YELLOW}Xvfb is installed.${RESET}"
echo "     SeleniumBase will use it automatically via xvfb=True."
echo ""