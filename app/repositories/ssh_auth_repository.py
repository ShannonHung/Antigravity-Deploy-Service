from abc import ABC, abstractmethod
from typing import Dict, Any
from app.domain.command import SSHConnectionConfig


class SSHAuthenticator(ABC):
    @abstractmethod
    def get_connect_kwargs(self) -> Dict[str, Any]:
        """Return kwargs to pass to asyncssh.connect()"""


def create_authenticator(config: SSHConnectionConfig) -> SSHAuthenticator:
    from app.repositories.ssh_cert_auth_repository import SSHCertificateAuthenticator
    from app.repositories.ssh_key_auth_repository import SSHKeyAuthenticator

    if config.auth_method == "ca" and config.cert_base64:
        return SSHCertificateAuthenticator(config.key_base64, config.cert_base64)
    return SSHKeyAuthenticator(config.key_base64)
