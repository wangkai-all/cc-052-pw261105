"""Errors with stable machine-readable codes shared by the API and recovery."""

from __future__ import annotations


class DeskError(Exception):
    code = "internal_error"
    status = 500

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.status = status


class InvalidBatchId(DeskError):
    code = "invalid_batch_id"
    status = 422


class BatchNotFound(DeskError):
    code = "batch_not_found"
    status = 404


class BatchFinished(DeskError):
    code = "batch_already_published"
    status = 409


class Conflict(DeskError):
    """Same batch id, different bytes."""

    code = "content_conflict"
    status = 409


class UploadRejected(DeskError):
    code = "upload_rejected"
    status = 400


class NotReadyForPublish(DeskError):
    code = "publish_not_ready"
    status = 409


class PublishInProgress(DeskError):
    code = "publish_in_progress"
    status = 409


class Cancelled(DeskError):
    code = "batch_cancelled"
    status = 409


class BatchFailed(DeskError):
    code = "batch_failed"
    status = 409


class LockBusy(DeskError):
    code = "lock_busy"
    status = 409


class SessionGone(DeskError):
    code = "upload_session_gone"
    status = 404
