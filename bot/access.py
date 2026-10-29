"""
access.py – Access control manager (admin & allowed users whitelist).

Persists admin and allowed users in a JSON file inside the sessions directory
so the configuration survives Docker container restarts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class UserEntry:
    """Represents an authorized user."""
    user_id: int | None = None
    username: str | None = None   # lowercase without '@'
    full_name: str = ""
    is_admin: bool = False


class AccessManager:
    """Manages admin and allowed users with JSON persistence."""

    def __init__(
        self,
        storage_path: Path,
        env_admins: list[str] | None = None,
    ) -> None:
        self._path = storage_path
        self._users: list[UserEntry] = []
        self._notified_pending: set[int] = set()
        self._load()
        self._sync_env_admins(env_admins or [])

    # ------------------------------------------------------------------ #
    # Queries                                                              #
    # ------------------------------------------------------------------ #

    def has_any_admin(self) -> bool:
        return any(u.is_admin for u in self._users)

    def get_admins(self) -> list[UserEntry]:
        return [u for u in self._users if u.is_admin]

    def get_all_users(self) -> list[UserEntry]:
        return list(self._users)

    def is_admin(self, user_id: int | None, username: str | None = None) -> bool:
        entry = self._find_and_bind(user_id, username)
        return bool(entry and entry.is_admin)

    def is_allowed(self, user_id: int | None, username: str | None = None) -> bool:
        entry = self._find_and_bind(user_id, username)
        return entry is not None

    # ------------------------------------------------------------------ #
    # Mutations                                                            #
    # ------------------------------------------------------------------ #

    def ensure_first_admin(
        self,
        user_id: int,
        username: str | None = None,
        full_name: str = "",
    ) -> bool:
        """If no admin is configured yet, register this user as admin.

        Returns True if the user was promoted as the initial admin.
        """
        if self.has_any_admin():
            return False
        self.add_user(
            user_id=user_id,
            username=username,
            full_name=full_name,
            is_admin=True,
        )
        logger.info("Registered initial admin: id=%s username=%s", user_id, username)
        return True

    def add_user(
        self,
        *,
        user_id: int | None = None,
        username: str | None = None,
        full_name: str = "",
        is_admin: bool = False,
    ) -> UserEntry:
        clean_username = self._clean_username(username)
        existing = self._find(user_id, clean_username)
        if existing:
            if user_id and not existing.user_id:
                existing.user_id = user_id
            if clean_username:
                existing.username = clean_username
            if full_name:
                existing.full_name = full_name
            if is_admin:
                existing.is_admin = True
            if user_id:
                self._notified_pending.discard(user_id)
            self._save()
            return existing

        entry = UserEntry(
            user_id=user_id,
            username=clean_username,
            full_name=full_name,
            is_admin=is_admin,
        )
        self._users.append(entry)
        if user_id:
            self._notified_pending.discard(user_id)
        self._save()
        return entry

    def remove_user(self, identifier: str | int) -> UserEntry | None:
        """Remove a non-admin user by user_id or @username."""
        target = self._find_by_identifier(identifier)
        if not target or target.is_admin:
            return None
        self._users.remove(target)
        self._save()
        return target

    def mark_notified(self, user_id: int) -> bool:
        """Return True if we haven't notified admin about this user yet, and mark as notified."""
        if user_id in self._notified_pending:
            return False
        self._notified_pending.add(user_id)
        return True

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _clean_username(username: str | None) -> str | None:
        if not username:
            return None
        cleaned = username.strip().lstrip("@").lower()
        return cleaned or None

    def _find(self, user_id: int | None, username: str | None) -> UserEntry | None:
        clean_uname = self._clean_username(username)
        for u in self._users:
            if user_id is not None and u.user_id == user_id:
                return u
            if clean_uname and u.username and u.username == clean_uname:
                return u
        return None

    def _find_by_identifier(self, identifier: str | int) -> UserEntry | None:
        if isinstance(identifier, int):
            return self._find(identifier, None)
        raw = str(identifier).strip()
        if raw.lstrip("-").isdigit():
            return self._find(int(raw), None)
        return self._find(None, raw)

    def _find_and_bind(
        self,
        user_id: int | None,
        username: str | None,
    ) -> UserEntry | None:
        """Find user by ID or username; if matched by username, bind their numeric user_id."""
        entry = self._find(user_id, username)
        if entry is None:
            return None

        changed = False
        clean_uname = self._clean_username(username)
        if user_id is not None and entry.user_id != user_id:
            entry.user_id = user_id
            changed = True
        if clean_uname and entry.username != clean_uname:
            entry.username = clean_uname
            changed = True
        if changed:
            self._save()
        return entry

    def _sync_env_admins(self, env_admins: list[str]) -> None:
        for raw in env_admins:
            token = raw.strip()
            if not token:
                continue
            if token.lstrip("-").isdigit():
                self.add_user(user_id=int(token), is_admin=True)
            else:
                self.add_user(username=token, is_admin=True)

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data: dict[str, Any] = json.loads(self._path.read_text(encoding="utf-8"))
            self._users = [
                UserEntry(
                    user_id=item.get("user_id"),
                    username=item.get("username"),
                    full_name=item.get("full_name", ""),
                    is_admin=bool(item.get("is_admin", False)),
                )
                for item in data.get("users", [])
            ]
        except Exception:
            logger.exception("Failed to load access file %s", self._path)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"users": [asdict(u) for u in self._users]}
            self._path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Failed to save access file %s", self._path)
