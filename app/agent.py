from __future__ import annotations

from pathlib import Path

from shadow_sdk.agent import AgentAuthenticator, AgentAuthError, AgentIdentity


class MachineAuthError(ValueError):
    pass


class MachineAuthUnavailable(RuntimeError):
    pass


class MachineScopeError(PermissionError):
    pass


class AgentAccess:
    """Authenticate the Ledger-only Agent credential from restricted files."""

    def __init__(self, *, registry_path: Path | None, secrets_dir: Path | None) -> None:
        self._registry_path = registry_path
        self._secrets_dir = secrets_dir
        self._authenticator: AgentAuthenticator | None = None

    def authenticate(self, authorization: str, *, scope: str) -> AgentIdentity:
        try:
            identity = self._get_authenticator().authenticate(authorization)
        except AgentAuthError as exc:
            raise MachineAuthError("invalid Ledger Agent credential") from exc
        try:
            identity.require_scope(scope)
        except AgentAuthError as exc:
            raise MachineScopeError("Ledger Agent scope is not granted") from exc
        return identity

    def _get_authenticator(self) -> AgentAuthenticator:
        if self._authenticator is not None:
            return self._authenticator
        if self._registry_path is None or self._secrets_dir is None:
            raise MachineAuthUnavailable("Ledger Agent authentication is not configured")
        self._authenticator = AgentAuthenticator(
            self._registry_path,
            secrets_dir=self._secrets_dir,
            audience="ledger",
        )
        return self._authenticator
