import logging

from app.core.exceptions import BaseAppException, ScriptVersionException


def test_script_version_exception_shape():
    exc = ScriptVersionException("too old", detail={"actual": "1.0.0"})
    assert isinstance(exc, BaseAppException)
    assert exc.http_status == 412
    assert exc.error_code == "SCRIPT_VERSION_MISMATCH"
    assert exc.log_level == logging.WARNING
    assert exc.message == "too old"
    assert exc.detail == {"actual": "1.0.0"}
