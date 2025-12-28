"""Secure credential encryption using Fernet."""
from cryptography.fernet import Fernet
import os
import base64

class CredentialManager:
    def __init__(self):
        # Load or generate encryption key
        key = os.getenv('ENCRYPTION_KEY')
        if not key:
            # Generate and save to .env for first run
            key = Fernet.generate_key().decode()
            print(f"⚠️ Generated new encryption key. Add to .env: ENCRYPTION_KEY={key}")
        self.cipher = Fernet(key.encode() if isinstance(key, str) else key)
    
    def encrypt(self, plaintext: str) -> str:
        """Encrypt credentials."""
        return self.cipher.encrypt(plaintext.encode()).decode()
    
    def decrypt(self, ciphertext: str) -> str:
        """Decrypt credentials."""
        return self.cipher.decrypt(ciphertext.encode()).decode()

# Global instance
credential_manager = CredentialManager()
