#!/usr/bin/env python3
import subprocess
import time
import os
import signal
import sys
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - WATCHDOG - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/home/god/Binance/watchdog.log')
    ]
)
logger = logging.getLogger()

# Configuration
PROJECT_ROOT = '/home/god/Binance/grid_trading_bot'
BOT_PATH = f'{PROJECT_ROOT}/main.py'
CHECK_INTERVAL = 60  # Check every 60 seconds

# Virtual environment path - update this to your actual venv path if you're using one
VENV_PATH = '/home/god/Binance/venv'  # 假设的虚拟环境路径，请替换为实际路径
USE_VENV = True  # Set to False if not using virtual environment

def is_process_running(process):
    """Check if process is still running"""
    if process is None:
        return False
    return process.poll() is None

def start_bot():
    """Start the trading bot as a subprocess"""
    try:
        # Start the process and redirect output to a log file
        log_file = open('/home/god/Binance/bot_output.log', 'a')
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        log_file.write(f"\n\n--- Bot started at {timestamp} ---\n\n")
        log_file.flush()
        
        # Prepare command with or without virtual environment
        if USE_VENV and os.path.exists(f"{VENV_PATH}/bin/python"):
            python_executable = f"{VENV_PATH}/bin/python"
        else:
            python_executable = sys.executable
            
        logger.info(f"Using Python: {python_executable}")
        
        process = subprocess.Popen(
            [python_executable, BOT_PATH],
            stdout=log_file,
            stderr=log_file,
            cwd=PROJECT_ROOT
        )
        logger.info(f"Started bot with PID {process.pid}")
        return process
    except Exception as e:
        logger.error(f"Failed to start bot: {e}")
        return None

def handle_exit(signum, frame):
    """Clean exit handler for termination signals"""
    logger.info("Received termination signal, shutting down...")
    if bot_process and is_process_running(bot_process):
        logger.info(f"Terminating bot process {bot_process.pid}")
        bot_process.terminate()
        try:
            bot_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            logger.warning("Bot didn't terminate within timeout, forcing...")
            bot_process.kill()
    sys.exit(0)

# Register signal handlers
signal.signal(signal.SIGTERM, handle_exit)
signal.signal(signal.SIGINT, handle_exit)

# Main watchdog loop
if __name__ == "__main__":
    logger.info("Watchdog starting")
    restarts = 0
    bot_process = None
    
    while True:
        # Start the bot if it's not running
        if not is_process_running(bot_process):
            if bot_process is not None:
                logger.warning("Bot process died, restarting...")
                restarts += 1
            bot_process = start_bot()
        
        # Log periodic status
        if restarts > 0 and restarts % 5 == 0:
            logger.warning(f"Bot has restarted {restarts} times since watchdog started")
            
        # Sleep before next check
        time.sleep(CHECK_INTERVAL)