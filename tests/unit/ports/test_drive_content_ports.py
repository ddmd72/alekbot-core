"""The drive's content reads (USER_DRIVE_INDEXING_RFC §24.3): the too-large error carries its limit."""


from src.domain.user_drive import DriveFileTooLarge


class TestUserDriveContentReads:
    def test_too_large_carries_its_limit(self):
        err = DriveFileTooLarge(10 * 1024 * 1024)
        assert err.limit_bytes == 10 * 1024 * 1024 and "10.0MB" in str(err)
