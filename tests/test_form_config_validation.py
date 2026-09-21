import logging
import os
import time

import pytest
from ResultPublisher.config_validation import normalize_config
from InferenceNode.log_manager import AgeAndSizeRotatingHandler


def test_headers_and_serial_accept_legacy_forms():
    assert normalize_config('webhook', {'headers':'X-Test: value:extra'})['headers'] == {'X-Test':'value:extra'}
    assert normalize_config('serial', {'baud_rate':'115200'}) == {'baud':115200}
    with pytest.raises(ValueError):
        normalize_config('webhook', {'headers':'malformed header'})


def test_retention_only_prunes_expired_rotated_logs(tmp_path):
    active = tmp_path / 'test.log'
    old = tmp_path / 'test.log.1'; old.write_text('old')
    unrelated = tmp_path / 'test.log.keep'; unrelated.write_text('keep')
    os.utime(old, (time.time()-86400*10,)*2)
    handler = AgeAndSizeRotatingHandler(active, maxBytes=1000, backupCount=5, retention_days=2)
    try:
        assert not old.exists()
        assert active.exists() and unrelated.exists()
        handler.emit(logging.LogRecord('audit',logging.INFO,'',1,'message',(),None))
    finally:
        handler.close()


def test_serial_schema_argument_reaches_serial_library(monkeypatch):
    import sys
    from types import SimpleNamespace
    from unittest.mock import Mock
    from ResultPublisher.plugins.serial_destination import SerialDestination
    serial = Mock()
    monkeypatch.setitem(sys.modules, 'serial', SimpleNamespace(Serial=serial))
    destination = SerialDestination()
    try:
        destination.configure(com_port='/dev/audit', baud_rate='115200')
        serial.assert_called_once_with('/dev/audit', 115200, timeout=1)
    finally:
        destination.stop_queue()
        destination.close()
