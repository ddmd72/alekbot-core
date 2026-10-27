"""
FileStoragePort — store and retrieve user file attachments.

Implementations:
  GcsFileStorageAdapter — uploads to a GCS bucket under {user_id}/files/ prefix.

Distinction from MediaStoragePort: MediaStoragePort stores delivered content privately
under caller-made keys; FileStoragePort stores user file attachments by filename under
the user's prefix, with download and duplicate name resolution.
"""
from abc import ABC, abstractmethod


class FileStoragePort(ABC):
    """Store and retrieve user file attachments by filename."""

    @abstractmethod
    async def upload(self, data: bytes, filename: str, user_id: str, content_type: str) -> str:
        """
        Upload file to storage with Finder-style duplicate name resolution.

        Args:
            data:         Raw file bytes.
            filename:     Original filename (e.g. "report.docx"). Sanitized internally.
            user_id:      Owner. Used to build storage key: {user_id}/files/{filename}.
            content_type: MIME type.

        Returns:
            The deduplicated filename (e.g. "report (1).docx" if name was taken).

        Stores `data` as `{user_id}/files/<name>` and returns <name>: `filename` NFC-normalised,
        `#?[]*:` and newline/CR/tab replaced by "_", then made unique among this user's files
        Finder-style (`a.png`, `a (1).png`, `a (2).png`).
        Tenancy: uniqueness is per user; another user's names never count.
        Atomicity: never replaces an existing object; two concurrent uploads of one name return
        two names (P11-G4-2: not yet true, both return it and the later write replaces the earlier).
        Idempotency: uploading the same bytes again stores a new name.
        Errors: storage errors propagate; a name is returned only when the bytes are stored.
        """

    @abstractmethod
    async def download(self, filename: str, user_id: str) -> bytes:
        """
        Download file from storage.

        Args:
            filename: Deduplicated filename as returned by upload().
            user_id:  Owner.

        Returns:
            Raw file bytes.

        Raises:
            FileNotFoundError: if the file does not exist (another user's name included).
            Other storage errors propagate unchanged.

        `filename` is NFC-normalised first.
        """

    @abstractmethod
    async def delete(self, filename: str, user_id: str) -> None:
        """Remove `{user_id}/files/<filename>`.
        Errors: FileNotFoundError when absent (P11-G4-3: today the SDK's NotFound leaks); other storage
        errors propagate."""

    @abstractmethod
    async def exists(self, filename: str, user_id: str) -> bool:
        """Whether `{user_id}/files/<filename>` exists. upload's own dedup is the only caller."""

    @abstractmethod
    async def get_url(self, filename: str, user_id: str) -> str:
        """The object's storage URL. No caller in src/; the bucket is private, so the URL does not open."""
