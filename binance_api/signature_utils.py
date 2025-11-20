"""
Signature generation utilities for Binance API authentication.

Supports all three key types as specified in binance-spot-api-docs/web-socket-api.md:
- HMAC SHA-256 (API secret)
- RSA PKCS#1 v1.5 SHA-256 (RSA private key)
- Ed25519 (Ed25519 private key)

Reference: binance-connector-python/common/src/binance_common/signature.py
"""

import os
import hmac
import base64
from hashlib import sha256
from typing import Optional, Tuple, Union
from enum import Enum

try:
    from cryptography.hazmat.primitives.asymmetric import rsa, ed25519
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False


class KeyType(Enum):
    """Supported private key types"""
    HMAC = "hmac"
    RSA = "rsa"
    ED25519 = "ed25519"


class SignatureGenerator:
    """
    Generates signatures for Binance API requests.
    Automatically detects key type and applies correct signature algorithm.
    """
    
    def __init__(
        self, 
        api_secret: Optional[str] = None,
        private_key_path: Optional[str] = None,
        private_key_pass: Optional[str] = None
    ):
        """
        Initialize signature generator with authentication credentials.
        
        Args:
            api_secret: HMAC API secret string
            private_key_path: Path to RSA or Ed25519 private key file
            private_key_pass: Password for encrypted private key (optional)
        
        Raises:
            ValueError: If no valid authentication method is provided
            ImportError: If cryptography library is needed but not installed
        """
        self.api_secret = api_secret
        self.private_key_path = private_key_path
        self.private_key_pass = private_key_pass
        
        # Private key objects (loaded lazily)
        self._rsa_key: Optional[rsa.RSAPrivateKey] = None
        self._ed25519_key: Optional[ed25519.Ed25519PrivateKey] = None
        self._key_type: Optional[KeyType] = None
        
        # Determine and load the appropriate key type
        if private_key_path and os.path.isfile(private_key_path):
            if not CRYPTOGRAPHY_AVAILABLE:
                raise ImportError(
                    "cryptography library is required for RSA/Ed25519 keys. "
                    "Install with: pip install cryptography"
                )
            self._load_private_key()
        elif api_secret:
            self._key_type = KeyType.HMAC
        else:
            raise ValueError(
                "No authentication method provided. "
                "Provide either api_secret (HMAC) or private_key_path (RSA/Ed25519)"
            )
    
    def _load_private_key(self) -> None:
        """
        Load private key from file and detect its type (RSA or Ed25519).
        
        Follows the pattern from binance-connector-python:
        1. Try loading as RSA
        2. If that fails, try Ed25519
        3. If both fail, raise error
        """
        with open(self.private_key_path, 'rb') as key_file:
            key_data = key_file.read()
        
        # Prepare password for key decryption
        password = None
        if self.private_key_pass:
            password = self.private_key_pass.encode('utf-8')
        
        # Try RSA first
        try:
            self._rsa_key = serialization.load_pem_private_key(
                key_data,
                password=password,
                backend=default_backend()
            )
            # Verify it's actually an RSA key
            if isinstance(self._rsa_key, rsa.RSAPrivateKey):
                self._key_type = KeyType.RSA
                return
        except (ValueError, TypeError):
            pass
        
        # Try Ed25519
        try:
            self._ed25519_key = serialization.load_pem_private_key(
                key_data,
                password=password,
                backend=default_backend()
            )
            # Verify it's actually an Ed25519 key
            if isinstance(self._ed25519_key, ed25519.Ed25519PrivateKey):
                self._key_type = KeyType.ED25519
                return
        except (ValueError, TypeError):
            pass
        
        raise ValueError(
            f"Unsupported or invalid private key format in {self.private_key_path}. "
            "Private key must be either RSA or Ed25519 in PEM format."
        )
    
    def generate_signature(self, query_string: str) -> Tuple[str, KeyType]:
        """
        Generate signature for the given query string.
        
        Args:
            query_string: URL-encoded parameter string (sorted alphabetically)
                         Example: "apiKey=xxx&symbol=BTCUSDT&timestamp=1234567890"
        
        Returns:
            Tuple of (signature_string, key_type)
            - For RSA: base64-encoded signature
            - For Ed25519: base64-encoded signature
            - For HMAC: hex-encoded signature
        
        Reference: binance-spot-api-docs/web-socket-api.md
            - HMAC example: lines 667-757
            - RSA example: lines 759-849
            - Ed25519 example: lines 851-912
        """
        if self._key_type == KeyType.RSA:
            return self._generate_rsa_signature(query_string), KeyType.RSA
        elif self._key_type == KeyType.ED25519:
            return self._generate_ed25519_signature(query_string), KeyType.ED25519
        elif self._key_type == KeyType.HMAC:
            return self._generate_hmac_signature(query_string), KeyType.HMAC
        else:
            raise ValueError("No valid key type configured")
    
    def _generate_rsa_signature(self, query_string: str) -> str:
        """
        Generate RSA PKCS#1 v1.5 SHA-256 signature.
        
        Per web-socket-api.md line 812:
        - Use RSA private key to sign the query string
        - Hash with SHA-256
        - Use PKCS#1 v1.5 padding
        - Base64-encode the signature
        
        Args:
            query_string: Parameter string to sign
        
        Returns:
            Base64-encoded signature string
        """
        if not self._rsa_key:
            raise ValueError("RSA key not loaded")
        
        # Sign with RSA-SHA256
        signature_bytes = self._rsa_key.sign(
            query_string.encode('utf-8'),
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        
        # Base64 encode per specification
        return base64.b64encode(signature_bytes).decode('utf-8')
    
    def _generate_ed25519_signature(self, query_string: str) -> str:
        """
        Generate Ed25519 signature.
        
        Per web-socket-api.md line 909:
        - Use Ed25519 private key to sign the query string
        - Base64-encode the signature
        
        Args:
            query_string: Parameter string to sign
        
        Returns:
            Base64-encoded signature string
        """
        if not self._ed25519_key:
            raise ValueError("Ed25519 key not loaded")
        
        # Sign with Ed25519
        signature_bytes = self._ed25519_key.sign(query_string.encode('utf-8'))
        
        # Base64 encode per specification
        return base64.b64encode(signature_bytes).decode('utf-8')
    
    def _generate_hmac_signature(self, query_string: str) -> str:
        """
        Generate HMAC SHA-256 signature.
        
        Per web-socket-api.md line 724:
        - Use API secret to generate HMAC
        - Hash with SHA-256
        - Return hex-encoded digest
        
        Args:
            query_string: Parameter string to sign
        
        Returns:
            Hex-encoded signature string
        """
        if not self.api_secret:
            raise ValueError("API secret not configured for HMAC")
        
        # Generate HMAC-SHA256
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            query_string.encode('utf-8'),
            sha256
        ).hexdigest()
        
        return signature
    
    @property
    def key_type(self) -> Optional[KeyType]:
        """Get the detected/configured key type"""
        return self._key_type
    
    def verify_key(self) -> dict:
        """
        Verify that the loaded key is valid.
        
        Returns:
            Dictionary with verification status:
            {
                "valid": bool,
                "key_type": str,
                "error": str (if invalid)
            }
        """
        try:
            if self._key_type == KeyType.RSA:
                # Verify RSA key by getting public key
                public_key = self._rsa_key.public_key()
                return {
                    "valid": True,
                    "key_type": "RSA",
                    "key_size": self._rsa_key.key_size
                }
            elif self._key_type == KeyType.ED25519:
                # Verify Ed25519 key by getting public key
                public_key = self._ed25519_key.public_key()
                return {
                    "valid": True,
                    "key_type": "Ed25519"
                }
            elif self._key_type == KeyType.HMAC:
                return {
                    "valid": True,
                    "key_type": "HMAC",
                    "secret_length": len(self.api_secret) if self.api_secret else 0
                }
            else:
                return {
                    "valid": False,
                    "error": "No key type configured"
                }
        except Exception as e:
            return {
                "valid": False,
                "error": f"Key validation failed: {str(e)}"
            }
