from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]


class DriveUploader:
    """Optional Google Drive uploader for files exceeding Telegram limits or cloud backup."""

    def __init__(self, root_folder_id: str, service_account_file: Path) -> None:
        self.root_folder_id = root_folder_id
        self.service_account_file = service_account_file
        self._svc = None

    @property
    def enabled(self) -> bool:
        return self.service_account_file.exists()

    def _client(self):
        if self._svc is None:
            creds = service_account.Credentials.from_service_account_file(str(self.service_account_file), scopes=SCOPES)
            self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._svc

    def _ensure_folder(self, parent_id: str, name: str) -> str:
        svc = self._client()
        safe = name.replace("'", "\\'")
        q = f"'{parent_id}' in parents and name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        files = svc.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
        if files:
            return files[0]["id"]
        created = svc.files().create(
            body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
            fields="id",
        ).execute()
        return created["id"]

    def upload(self, local_path: Path, title_slug: str) -> dict[str, Any]:
        svc = self._client()
        proj_id = self._ensure_folder(self.root_folder_id, title_slug)
        orig_id = self._ensure_folder(proj_id, "originals")
        mime, _ = mimetypes.guess_type(local_path.name)
        media = MediaFileUpload(str(local_path), mimetype=mime or "application/octet-stream", resumable=True)
        created = svc.files().create(
            body={"name": local_path.name, "parents": [orig_id]},
            media_body=media,
            fields="id, name, webViewLink",
        ).execute()
        return created
