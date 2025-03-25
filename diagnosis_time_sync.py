#!/usr/bin/env python3
# filepath: /home/god/Binance/diagnosis_time_sync.py

import os
import sys
import time
import json
import logging
import socket
import requests
import statistics
import subprocess
from datetime import datetime

# Add project directory to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the relevant modules from the trading bot
from grid_trading_bot.binance_api.client import BinanceClient
import config

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('time_diagnosis.log')
    ]
)

logger = logging.getLogger("time_diagnosis")

class TimeSync:
    """Class to diagnose and fix time synchronization issues with Binance API"""
    
    def __init__(self):
        """Initialize the diagnostic tool"""
        self.binance_client = None
        self.ntp_servers = [
            "time.google.com",
            "time.cloudflare.com",
            "time.windows.com",
            "pool.ntp.org",
            "ntp.ubuntu.com"
        ]
        self.binance_endpoints = [
            "api.binance.com",
            "api1.binance.com",
            "api2.binance.com",
            "api3.binance.com"
        ]
        self.time_samples = 10  # Number of time samples to collect
    
    def check_system_clock(self):
        """Check system clock synchronization status"""
        logger.info("=== SYSTEM CLOCK CHECK ===")
        
        # Check if chrony or ntpd is running
        try:
            chrony_status = subprocess.run(
                ["systemctl", "is-active", "chrony"], 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                text=True
            )
            ntp_status = subprocess.run(
                ["systemctl", "is-active", "ntp"], 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                text=True
            )
            
            if chrony_status.stdout.strip() == "active":
                logger.info("✓ Chrony service is active")
                self._check_chrony_sources()
            elif ntp_status.stdout.strip() == "active":
                logger.info("✓ NTP service is active")
                self._check_ntp_status()
            else:
                logger.warning("❌ No time synchronization service (chrony/ntp) is running")
                logger.info("Recommendation: Install and enable chrony with 'sudo apt install chrony && sudo systemctl enable --now chrony'")
        except Exception as e:
            logger.error(f"Failed to check system time services: {e}")
        
        # Check for time drift against NTP servers
        try:
            self._check_time_drift()
        except Exception as e:
            logger.error(f"Failed to check time drift: {e}")
    
    def _check_chrony_sources(self):
        """Check chrony sources and synchronization status"""
        try:
            result = subprocess.run(
                ["chronyc", "sources"], 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                text=True
            )
            logger.info(f"Chrony sources:\n{result.stdout}")
            
            tracking = subprocess.run(
                ["chronyc", "tracking"], 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                text=True
            )
            logger.info(f"Chrony tracking:\n{tracking.stdout}")
            
            # Check for system time offset
            for line in tracking.stdout.split('\n'):
                if "System time" in line:
                    offset_parts = line.split(':')
                    if len(offset_parts) > 1:
                        offset = offset_parts[1].strip()
                        logger.info(f"System time offset: {offset}")
                        if "seconds fast" in offset and float(offset.split()[0]) > 0.5:
                            logger.warning(f"❌ System clock is significantly ahead: {offset}")
                        elif "seconds slow" in offset and float(offset.split()[0]) > 0.5:
                            logger.warning(f"❌ System clock is significantly behind: {offset}")
        except Exception as e:
            logger.error(f"Failed to check chrony status: {e}")
    
    def _check_ntp_status(self):
        """Check NTP synchronization status"""
        try:
            result = subprocess.run(
                ["ntpq", "-p"], 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE,
                text=True
            )
            logger.info(f"NTP peers:\n{result.stdout}")
        except Exception as e:
            logger.error(f"Failed to check NTP status: {e}")
    
    def _check_time_drift(self):
        """Check time drift against multiple NTP servers"""
        logger.info("Checking time drift against NTP servers...")
        
        for server in self.ntp_servers:
            try:
                # Use ntpdate in query mode (-q) to check time without setting it
                result = subprocess.run(
                    ["ntpdate", "-q", server], 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE,
                    text=True
                )
                
                if result.returncode == 0:
                    for line in result.stdout.split('\n'):
                        if "offset" in line:
                            parts = line.split(',')
                            for part in parts:
                                if "offset" in part:
                                    offset = float(part.split()[1])
                                    logger.info(f"Time offset with {server}: {offset} seconds")
                                    if abs(offset) > 0.5:
                                        logger.warning(f"❌ Significant time drift detected with {server}: {offset} seconds")
                                    else:
                                        logger.info(f"✓ Time synchronized with {server} (offset: {offset} seconds)")
            except Exception as e:
                logger.error(f"Failed to check time drift with {server}: {e}")
    
    def test_binance_connectivity(self):
        """Test network connectivity to Binance servers"""
        logger.info("\n=== BINANCE CONNECTIVITY TEST ===")
        
        for endpoint in self.binance_endpoints:
            try:
                # Measure ping RTT
                start_time = time.time()
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect((endpoint, 443))
                sock.close()
                rtt = (time.time() - start_time) * 1000
                
                logger.info(f"Connected to {endpoint} - RTT: {rtt:.2f}ms")
                
                # Measure HTTP response time
                start_time = time.time()
                response = requests.get(f"https://{endpoint}/api/v3/time", timeout=5)
                http_rtt = (time.time() - start_time) * 1000
                
                if response.status_code == 200:
                    server_time = response.json()['serverTime']
                    local_time = int(time.time() * 1000)
                    time_diff = local_time - server_time
                    
                    logger.info(f"HTTP response from {endpoint} - RTT: {http_rtt:.2f}ms")
                    logger.info(f"Server time: {server_time} ({datetime.fromtimestamp(server_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')})")
                    logger.info(f"Local time:  {local_time} ({datetime.fromtimestamp(local_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')})")
                    logger.info(f"Time difference: {time_diff} ms")
                    
                    if abs(time_diff) > 1000:
                        logger.warning(f"❌ Time difference with {endpoint} exceeds Binance's 1000ms threshold: {time_diff}ms")
                    else:
                        logger.info(f"✓ Time difference with {endpoint} is within acceptable limits: {time_diff}ms")
                else:
                    logger.error(f"HTTP request to {endpoint} failed with status code: {response.status_code}")
            except Exception as e:
                logger.error(f"Connection to {endpoint} failed: {e}")
    
    def analyze_time_offset_calculation(self):
        """Analyze the current time offset calculation logic in the client code"""
        logger.info("\n=== TIME OFFSET CALCULATION ANALYSIS ===")
        
        try:
            # Initialize Binance client
            self.binance_client = BinanceClient()
            
            # Check existing time offset
            current_offset = self.binance_client.time_offset
            logger.info(f"Current time offset in client: {current_offset}ms")
            
            # Collect multiple time samples
            logger.info(f"Collecting {self.time_samples} time samples...")
            samples = []
            
            for i in range(self.time_samples):
                try:
                    local_time_start = int(time.time() * 1000)
                    response = requests.get("https://api.binance.com/api/v3/time", timeout=5)
                    local_time_end = int(time.time() * 1000)
                    
                    if response.status_code == 200:
                        server_time = response.json()['serverTime']
                        rtt = local_time_end - local_time_start
                        one_way_delay = rtt / 2
                        time_offset = server_time - (local_time_start + one_way_delay)
                        
                        samples.append({
                            'server_time': server_time,
                            'local_time_start': local_time_start,
                            'local_time_end': local_time_end,
                            'rtt': rtt,
                            'offset': time_offset
                        })
                        
                        logger.info(f"Sample {i+1}: offset={time_offset:.2f}ms, RTT={rtt}ms")
                    
                    time.sleep(0.5)  # Brief pause between samples
                except Exception as e:
                    logger.error(f"Failed to collect sample {i+1}: {e}")
            
            # Analyze samples
            if samples:
                # Sort by RTT for best sample
                samples_by_rtt = sorted(samples, key=lambda x: x['rtt'])
                best_sample = samples_by_rtt[0]
                
                # Calculate statistics
                offsets = [s['offset'] for s in samples]
                rtts = [s['rtt'] for s in samples]
                
                logger.info("\nTime sample statistics:")
                logger.info(f"Mean offset: {statistics.mean(offsets):.2f}ms")
                logger.info(f"Median offset: {statistics.median(offsets):.2f}ms")
                logger.info(f"Min offset: {min(offsets):.2f}ms")
                logger.info(f"Max offset: {max(offsets):.2f}ms")
                logger.info(f"Std dev: {statistics.stdev(offsets):.2f}ms")
                logger.info(f"Sample variance: {statistics.variance(offsets):.2f}")
                
                logger.info("\nRTT statistics:")
                logger.info(f"Mean RTT: {statistics.mean(rtts):.2f}ms")
                logger.info(f"Min RTT: {min(rtts):.2f}ms")
                logger.info(f"Max RTT: {max(rtts):.2f}ms")
                
                # Best sample analysis
                logger.info("\nBest sample (lowest RTT):")
                logger.info(f"Offset: {best_sample['offset']:.2f}ms")
                logger.info(f"RTT: {best_sample['rtt']}ms")
                
                # Safety offset analysis
                if best_sample['offset'] < 0:
                    # Current calculation: min(-500, self.time_offset - 500)
                    current_safety = min(-500, best_sample['offset'] - 500)
                    
                    # Alternative: self.time_offset - 500 (without min)
                    alternative_safety = best_sample['offset'] - 500
                    
                    logger.info("\nSafety offset analysis:")
                    logger.info(f"Current formula (min(-500, offset - 500)): {current_safety}ms")
                    logger.info(f"Alternative formula (offset - 500): {alternative_safety}ms")
                    
                    if current_safety != alternative_safety:
                        logger.warning("❌ Current safety offset calculation is limiting the compensation!")
                        logger.warning(f"The min() function restricts offset to -500ms when it should be {alternative_safety}ms")
                    else:
                        logger.info("✓ Current safety offset calculation is appropriate")
                
                # Recommendations
                logger.info("\nRecommendations:")
                if abs(best_sample['offset']) > 1000:
                    logger.warning("❌ Time difference is significant! Apply these fixes:")
                    logger.warning("1. Remove the min() function from safety_offset calculation")
                    logger.warning("2. Use full offset compensation: safety_offset = self.time_offset - 500")
                else:
                    logger.info("✓ Time difference is within acceptable limits")
            else:
                logger.error("No valid time samples collected!")
            
            # Force a time sync and check the result
            logger.info("\nForcing time sync to test client implementation...")
            sync_result = self.binance_client.manual_time_sync()
            logger.info(f"Manual time sync result: {'Success' if sync_result else 'Failed'}")
            logger.info(f"Updated time offset: {self.binance_client.time_offset}ms")
            
            # Test timestamp generation with applied offset
            logger.info("\nTesting timestamp generation...")
            self._test_timestamp_generation()
            
        except Exception as e:
            logger.error(f"Failed to analyze time offset calculation: {e}")
    
    def _test_timestamp_generation(self):
        """Test timestamp generation with different offset calculations"""
        try:
            # Get current offset from client
            current_offset = self.binance_client.time_offset
            
            # Generate adjusted timestamp using client's method
            client_timestamp = self.binance_client._get_timestamp()
            
            # Calculate what timestamp would be using alternative safety offsets
            base_ts = int(time.time() * 1000)
            
            # Method 1: Client's current method
            if current_offset < 0:
                safety_offset_current = min(-500, current_offset - 500)
            else:
                safety_offset_current = -500
            
            # Method 2: Fixed method (always use full offset)
            safety_offset_fixed = current_offset - 500 if current_offset < 0 else -500
            
            # Method 3: Very conservative (always subtract 1000ms)
            safety_offset_conservative = current_offset - 1000 if current_offset < 0 else -1000
            
            ts_current = base_ts + safety_offset_current
            ts_fixed = base_ts + safety_offset_fixed
            ts_conservative = base_ts + safety_offset_conservative
            
            logger.info(f"Base timestamp (no offset): {base_ts}")
            logger.info(f"Client's adjusted timestamp: {client_timestamp}")
            logger.info(f"Method 1 (current): {ts_current} (offset = {safety_offset_current}ms)")
            logger.info(f"Method 2 (fixed): {ts_fixed} (offset = {safety_offset_fixed}ms)")
            logger.info(f"Method 3 (conservative): {ts_conservative} (offset = {safety_offset_conservative}ms)")
            
            # Check which would pass Binance's 1000ms threshold
            logger.info("\nValidation against Binance server time:")
            
            # Get current server time from Binance
            response = requests.get("https://api.binance.com/api/v3/time", timeout=5)
            if response.status_code == 200:
                server_time = response.json()['serverTime']
                logger.info(f"Current Binance server time: {server_time}")
                
                # Check each timestamp against server time
                diff_current = ts_current - server_time
                diff_fixed = ts_fixed - server_time
                diff_conservative = ts_conservative - server_time
                
                logger.info(f"Method 1 (current) diff: {diff_current}ms {'✓ PASS' if abs(diff_current) < 1000 else '❌ FAIL'}")
                logger.info(f"Method 2 (fixed) diff: {diff_fixed}ms {'✓ PASS' if abs(diff_fixed) < 1000 else '❌ FAIL'}")
                logger.info(f"Method 3 (conservative) diff: {diff_conservative}ms {'✓ PASS' if abs(diff_conservative) < 1000 else '❌ FAIL'}")
                
                # Recommendation
                if abs(diff_current) >= 1000 and abs(diff_fixed) < 1000:
                    logger.warning("\n❌ Current method would fail Binance timestamp validation!")
                    logger.warning("Solution: Remove min() function from safety offset calculation")
                elif abs(diff_current) >= 1000 and abs(diff_conservative) < 1000:
                    logger.warning("\n❌ Current method would fail Binance timestamp validation!")
                    logger.warning("Solution: Use more conservative offset (current_offset - 1000)")
        except Exception as e:
            logger.error(f"Failed to test timestamp generation: {e}")
    
    def simulate_api_requests(self):
        """Simulate API requests with different timestamp calculations"""
        logger.info("\n=== API REQUEST SIMULATION ===")
        
        try:
            # Only proceed if we have a client
            if not self.binance_client:
                self.binance_client = BinanceClient()
            
            # Get current time offset
            current_offset = self.binance_client.time_offset
            
            # Define test cases with different offset calculations
            test_cases = [
                {
                    "name": "Current implementation",
                    "offset_func": lambda offset: min(-500, offset - 500) if offset < 0 else -500
                },
                {
                    "name": "Fixed implementation (full offset)",
                    "offset_func": lambda offset: offset - 500 if offset < 0 else -500
                },
                {
                    "name": "Conservative implementation",
                    "offset_func": lambda offset: offset - 1000 if offset < 0 else -1000
                }
            ]
            
            # Test each case
            for case in test_cases:
                safety_offset = case["offset_func"](current_offset)
                adjusted_timestamp = int(time.time() * 1000) + safety_offset
                
                logger.info(f"\nTest: {case['name']}")
                logger.info(f"Safety offset: {safety_offset}ms")
                logger.info(f"Adjusted timestamp: {adjusted_timestamp}")
                
                # Make a simple no-risk API call that requires timestamp
                try:
                    # Use direct request to avoid client's internal timestamp adjustment
                    api_url = "https://api.binance.com/api/v3/account"
                    headers = {
                        "X-MBX-APIKEY": self.binance_client.api_key
                    }
                    
                    # Generate signature
                    import hmac
                    import hashlib
                    
                    params = {
                        "timestamp": adjusted_timestamp
                    }
                    
                    query_string = f"timestamp={adjusted_timestamp}"
                    signature = hmac.new(
                        bytes(self.binance_client.api_secret, "utf-8"),
                        msg=bytes(query_string, "utf-8"),
                        digestmod=hashlib.sha256
                    ).hexdigest()
                    
                    params["signature"] = signature
                    
                    response = requests.get(api_url, params=params, headers=headers, timeout=10)
                    
                    if response.status_code == 200:
                        logger.info(f"✓ API request successful with {case['name']}")
                    else:
                        error_data = response.json()
                        logger.warning(f"❌ API request failed with {case['name']}: {error_data}")
                        
                        # Check for timestamp error
                        if 'code' in error_data and error_data['code'] == -1021:
                            logger.error(f"Timestamp error detected with {case['name']}!")
                except Exception as e:
                    logger.error(f"Simulation failed for {case['name']}: {e}")
            
            logger.info("\nRecommendation based on all tests:")
            if abs(current_offset) > 500:
                logger.warning("Remove the min() function from safety offset calculation")
                logger.warning("Replace with: safety_offset = self.time_offset - 500")
            
        except Exception as e:
            logger.error(f"Failed to simulate API requests: {e}")
    
    def run_diagnosis(self):
        """Run the full diagnostic routine"""
        logger.info("Starting Binance API time synchronization diagnosis")
        logger.info(f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        self.check_system_clock()
        self.test_binance_connectivity()
        self.analyze_time_offset_calculation()
        self.simulate_api_requests()
        
        logger.info("\n=== DIAGNOSIS SUMMARY ===")
        logger.info("1. Check if system clock is synchronized with NTP (install chrony if not)")
        logger.info("2. The key issue is likely in the safety_offset calculation:")
        logger.info("   - Current code uses min(-500, offset - 500) which limits the compensation")
        logger.info("   - When time offset is large (e.g. -1241ms), this only adjusts by -500ms")
        logger.info("   - Should use full offset: safety_offset = offset - 500")
        logger.info("3. Update the code in client.py:")
        logger.info("   if self.time_offset < 0:")
        logger.info("       # Remove the min() function to use the full offset compensation")
        logger.info("       safety_offset = self.time_offset - 500  # Full offset plus safety margin")
        logger.info("   else:")
        logger.info("       safety_offset = -500")
        logger.info("\nDiagnosis complete. See time_diagnosis.log for full details.")

if __name__ == "__main__":
    diagnosis = TimeSync()
    diagnosis.run_diagnosis()