#!/bin/bash
# GCP VM Setup Script for Gemini Studio API
# Run this ONCE after SSH-ing into your new VM

set -e

echo "=== Gemini Studio API - GCP Setup ==="

# Update system
echo "[1/6] Updating system..."
sudo apt update && sudo apt upgrade -y

# Install Python 3.11 and pip
echo "[2/6] Installing Python..."
sudo apt install -y python3.11 python3.11-venv python3-pip git

# Install Playwright dependencies (for headless Chromium)
echo "[3/6] Installing browser dependencies..."
sudo apt install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0

# Create app directory
echo "[4/6] Setting up app directory..."
mkdir -p ~/gemini-studio-api
cd ~/gemini-studio-api

# Create virtual environment
echo "[5/6] Creating Python virtual environment..."
python3.11 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install --upgrade pip
pip install fastapi uvicorn playwright python-dotenv pydantic

# Install Playwright browsers
echo "[6/6] Installing Chromium browser..."
playwright install chromium

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "Next steps:"
echo "1. Upload your project files to ~/gemini-studio-api/"
echo "2. Create .env file with your settings"
echo "3. Run: cd ~/gemini-studio-api && source venv/bin/activate && python main.py"
echo ""
