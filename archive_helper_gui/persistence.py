from __future__ import annotations

import os
import pickle
from pathlib import Path
from typing import Any


class PersistenceStore:
    """Persist GUI state to a local pickle file and optionally use OS keyring.

    This is intentionally UI-agnostic: the GUI owns the schema/keys.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        keyring_available: bool,
        keyring_module: Any | None,
        keyring_service: str = "ArchiveHelperForJellyfin",
    ) -> None:
        self._state_dir = state_dir
        self._keyring_available = bool(keyring_available)
        self._keyring = keyring_module
        self._keyring_service = keyring_service

    @property
    def state_dir(self) -> Path:
        return self._state_dir

    def state_path(self) -> Path:
        return self._state_dir / "state.pkl"

    def state_file_exists(self) -> bool:
        return self.state_path().exists()

    def load_state_dict(self) -> dict[str, Any] | None:
        p = self.state_path()
        if not p.exists():
            return None
        try:
            data = pickle.loads(p.read_bytes())
            if isinstance(data, dict):
                return data
        except Exception:
            return None
        return None

    def save_state_dict(self, data: dict[str, Any]) -> None:
        sd = self._state_dir
        sd.mkdir(parents=True, exist_ok=True)
        p = self.state_path()
        p.write_bytes(pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass

    def load_password(self, key_id: str) -> str | None:
        if not self._keyring_available or self._keyring is None:
            return None
        try:
            pw = self._keyring.get_password(self._keyring_service, key_id)
            return pw or None
        except Exception:
            return None

    def save_password(self, key_id: str, password: str) -> None:
        if not self._keyring_available or self._keyring is None:
            return
        try:
            pw = (password or "").strip()
            if pw:
                self._keyring.set_password(self._keyring_service, key_id, pw)
            else:
                self._keyring.delete_password(self._keyring_service, key_id)
        except Exception:
            pass
