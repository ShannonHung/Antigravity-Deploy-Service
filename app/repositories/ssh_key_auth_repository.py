import base64
from typing import Dict, Any
import asyncssh
from app.repositories.ssh_auth_repository import SSHAuthenticator


class SSHKeyAuthenticator(SSHAuthenticator):
    def __init__(self, key_base64: str):
        self.key_base64 = key_base64

    def get_connect_kwargs(self) -> Dict[str, Any]:
        key_str = base64.b64decode(self.key_base64).decode("utf-8")
        key_obj = asyncssh.import_private_key(key_str)
        return {"client_keys": [key_obj], "known_hosts": None}
