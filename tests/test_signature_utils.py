"""
Tests for signature_utils module.

Tests all three signature types (HMAC, RSA, Ed25519) as required by AGENTS.md.
"""

import sys
import os
import unittest
import base64
import hmac
from hashlib import sha256
from pathlib import Path

# Ensure project root is in path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from binance_api.signature_utils import SignatureGenerator, KeyType

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend
    CRYPTOGRAPHY_AVAILABLE = True
except ImportError:
    CRYPTOGRAPHY_AVAILABLE = False


class TestSignatureGenerator(unittest.TestCase):
    """Test signature generation for all supported key types"""
    
    def setUp(self):
        """Set up test fixtures"""
        self.test_query_string = "apiKey=test&symbol=BTCUSDT&timestamp=1234567890"
        self.test_api_secret = "test_secret_key_for_hmac"
    
    def test_hmac_signature_generation(self):
        """Test HMAC SHA-256 signature generation"""
        generator = SignatureGenerator(api_secret=self.test_api_secret)
        
        # Verify key type is detected correctly
        self.assertEqual(generator.key_type, KeyType.HMAC)
        
        # Generate signature
        signature, key_type = generator.generate_signature(self.test_query_string)
        
        # Verify returned key type
        self.assertEqual(key_type, KeyType.HMAC)
        
        # Verify signature format (should be hex-encoded)
        self.assertEqual(len(signature), 64)  # SHA-256 hex digest is 64 chars
        self.assertTrue(all(c in '0123456789abcdef' for c in signature))
        
        # Verify signature value matches expected HMAC
        expected_signature = hmac.new(
            self.test_api_secret.encode('utf-8'),
            self.test_query_string.encode('utf-8'),
            sha256
        ).hexdigest()
        self.assertEqual(signature, expected_signature)
    
    @unittest.skipIf(not CRYPTOGRAPHY_AVAILABLE, "cryptography library not available")
    def test_ed25519_signature_generation(self):
        """Test Ed25519 signature generation"""
        # Generate a test Ed25519 key
        private_key = ed25519.Ed25519PrivateKey.generate()
        
        # Save to temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.pem') as f:
            key_path = f.name
            pem_data = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
            f.write(pem_data)
        
        try:
            # Create generator with Ed25519 key
            generator = SignatureGenerator(private_key_path=key_path)
            
            # Verify key type is detected correctly
            self.assertEqual(generator.key_type, KeyType.ED25519)
            
            # Generate signature
            signature, key_type = generator.generate_signature(self.test_query_string)
            
            # Verify returned key type
            self.assertEqual(key_type, KeyType.ED25519)
            
            # Verify signature format (should be base64-encoded)
            try:
                signature_bytes = base64.b64decode(signature)
                self.assertEqual(len(signature_bytes), 64)  # Ed25519 signature is 64 bytes
            except Exception:
                self.fail("Signature is not valid base64")
            
            # Verify signature can be verified with public key
            public_key = private_key.public_key()
            try:
                public_key.verify(signature_bytes, self.test_query_string.encode('utf-8'))
            except Exception as e:
                self.fail(f"Signature verification failed: {e}")
        
        finally:
            # Clean up temp file
            if os.path.exists(key_path):
                os.unlink(key_path)
    
    @unittest.skipIf(not CRYPTOGRAPHY_AVAILABLE, "cryptography library not available")
    def test_rsa_signature_generation(self):
        """Test RSA PKCS#1 v1.5 SHA-256 signature generation"""
        # Generate a test RSA key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )
        
        # Save to temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.pem') as f:
            key_path = f.name
            pem_data = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
            f.write(pem_data)
        
        try:
            # Create generator with RSA key
            generator = SignatureGenerator(private_key_path=key_path)
            
            # Verify key type is detected correctly
            self.assertEqual(generator.key_type, KeyType.RSA)
            
            # Generate signature
            signature, key_type = generator.generate_signature(self.test_query_string)
            
            # Verify returned key type
            self.assertEqual(key_type, KeyType.RSA)
            
            # Verify signature format (should be base64-encoded)
            try:
                signature_bytes = base64.b64decode(signature)
                # RSA 2048-bit signature is 256 bytes
                self.assertEqual(len(signature_bytes), 256)
            except Exception:
                self.fail("Signature is not valid base64")
            
            # Verify signature can be verified with public key
            public_key = private_key.public_key()
            try:
                public_key.verify(
                    signature_bytes,
                    self.test_query_string.encode('utf-8'),
                    padding.PKCS1v15(),
                    hashes.SHA256()
                )
            except Exception as e:
                self.fail(f"Signature verification failed: {e}")
        
        finally:
            # Clean up temp file
            if os.path.exists(key_path):
                os.unlink(key_path)
    
    def test_no_credentials_raises_error(self):
        """Test that missing credentials raises ValueError"""
        with self.assertRaises(ValueError) as context:
            SignatureGenerator()
        
        self.assertIn("No authentication method provided", str(context.exception))
    
    def test_invalid_key_file_raises_error(self):
        """Test that invalid key file raises ValueError"""
        with self.assertRaises(ValueError) as context:
            SignatureGenerator(private_key_path="/nonexistent/path/key.pem")
        
        # Should raise error about invalid path or format
        self.assertTrue(
            "No authentication method provided" in str(context.exception) or
            "Unsupported" in str(context.exception)
        )
    
    def test_verify_key_hmac(self):
        """Test key verification for HMAC"""
        generator = SignatureGenerator(api_secret=self.test_api_secret)
        
        verification = generator.verify_key()
        
        self.assertTrue(verification["valid"])
        self.assertEqual(verification["key_type"], "HMAC")
        self.assertEqual(verification["secret_length"], len(self.test_api_secret))
    
    @unittest.skipIf(not CRYPTOGRAPHY_AVAILABLE, "cryptography library not available")
    def test_verify_key_ed25519(self):
        """Test key verification for Ed25519"""
        # Generate a test Ed25519 key
        private_key = ed25519.Ed25519PrivateKey.generate()
        
        # Save to temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.pem') as f:
            key_path = f.name
            pem_data = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
            f.write(pem_data)
        
        try:
            generator = SignatureGenerator(private_key_path=key_path)
            
            verification = generator.verify_key()
            
            self.assertTrue(verification["valid"])
            self.assertEqual(verification["key_type"], "Ed25519")
        
        finally:
            if os.path.exists(key_path):
                os.unlink(key_path)
    
    @unittest.skipIf(not CRYPTOGRAPHY_AVAILABLE, "cryptography library not available")
    def test_verify_key_rsa(self):
        """Test key verification for RSA"""
        # Generate a test RSA key
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
            backend=default_backend()
        )
        
        # Save to temporary file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix='.pem') as f:
            key_path = f.name
            pem_data = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption()
            )
            f.write(pem_data)
        
        try:
            generator = SignatureGenerator(private_key_path=key_path)
            
            verification = generator.verify_key()
            
            self.assertTrue(verification["valid"])
            self.assertEqual(verification["key_type"], "RSA")
            self.assertEqual(verification["key_size"], 2048)
        
        finally:
            if os.path.exists(key_path):
                os.unlink(key_path)


if __name__ == '__main__':
    unittest.main()
