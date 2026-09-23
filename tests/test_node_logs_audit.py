import logging
from unittest.mock import Mock
import pytest
from flask import Flask
from test_settings_save_regressions import body, node, env
from InferenceNode import node_settings_store as nss
from InferenceNode.log_manager import LogManager

def send(n, route, payload):
    app=Flask(__name__);app.add_url_rule('/save',view_func=body(route,n),methods=['POST'])
    return app.test_client().post('/save',json=payload)

@pytest.mark.parametrize('payload',[{'node_name':[]},{'node_name':' '},{'node_name':'x'*121},{'log_level':'UNKNOWN'},{'other':1}])
def test_node_rejects_invalid_fields_before_mutation(payload):
    n=node();r=send(n,'update_node_config',payload)
    assert r.status_code==400
    assert n.node_name=='new-name'
    n.log_manager.update_settings.assert_not_called()

@pytest.mark.parametrize('payload',[[],{}, {'retention_days':31},{'max_log_size_mb':True},{'enable_file_logging':'false'}])
def test_log_invalid_settings_are_client_errors(payload):
    n=node();r=send(n,'update_log_settings',payload)
    assert r.status_code==400
    n.log_manager.update_settings.assert_not_called()

def test_node_save_only_persists_owned_settings(env):
    n=node();n._save_settings=Mock(side_effect=AssertionError('must not save unrelated runtime snapshots'))
    nss.set_setting(nss.KEY_TELEMETRY,{'enabled':False,'publish_interval':17})
    r=send(n,'update_node_config',{'node_name':'  New node  '})
    assert r.status_code==200
    assert nss.get_setting(nss.KEY_NODE_IDENTITY)['node_name']=='New node'
    assert nss.get_setting(nss.KEY_TELEMETRY)['publish_interval']==17
    assert nss.get_setting(nss.KEY_PREFERENCES) is None

def test_failed_runtime_apply_restores_previous_logging():
    n=node();n.log_manager.update_settings.side_effect=[False,True]
    r=send(n,'update_log_settings',{'log_level':'DEBUG'})
    assert r.status_code==500 and not r.json.get('success')
    assert n.log_manager.update_settings.call_count==2
    assert n.log_manager.update_settings.call_args.args[0]['log_level']=='WARNING'

def test_console_log_level_changes_with_settings():
    manager=LogManager();manager.stream_handler=Mock()
    previous=logging.getLogger().level
    try:
        assert manager.update_settings({'log_level':'DEBUG'})
        manager.stream_handler.setLevel.assert_called_once_with(logging.DEBUG)
    finally: logging.getLogger().setLevel(previous)

def test_file_enable_failure_is_not_success():
    manager=LogManager();manager._setup_file_logging=Mock()
    assert not manager.update_settings({'enable_file_logging':True})

def test_retention_applied_before_new_handler():
    manager=LogManager();seen=[]
    def setup():
        seen.append(manager.retention_days);manager.file_handler=Mock()
    manager._setup_file_logging=setup
    assert manager.update_settings({'enable_file_logging':True,'retention_days':30})
    assert seen==[30]

def test_log_timestamp_includes_timezone():
    from InferenceNode.log_manager import MemoryLogHandler
    handler=MemoryLogHandler()
    handler.emit(logging.LogRecord('test',logging.INFO,'',1,'message',(),None))
    assert handler.get_logs()[0]['timestamp'].endswith('+00:00')
