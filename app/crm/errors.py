"""Safe CRM failures independent of HTTP response construction."""


class CRMError(Exception):
    status_code = 400
    detail = "CRM operation could not be completed."


class CRMNotFound(CRMError):
    status_code = 404
    detail = "CRM resource not found."


class CRMPermissionDenied(CRMError):
    status_code = 403
    detail = "CRM permission denied."


class CRMConflict(CRMError):
    status_code = 409
    detail = "CRM operation conflicts with existing data."


class CRMInvalidReference(CRMError):
    status_code = 422
    detail = "CRM reference is invalid."
