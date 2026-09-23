from test_form_roundtrips_pg import live_forms, test_node_and_log_forms_database_and_get
import pytest

def test_both_templates_render(live_forms):
    for path in ('/node-info','/logs'):
        r=live_forms['client'].get(path)
        assert r.status_code==200
        assert b'operations-workspace.css' in r.data
        assert b'data-can-manage="true"' in r.data

@pytest.mark.parametrize('path,payload',[
    ('/api/node/config',{'node_name':[]} ),
    ('/api/logs/settings',{'retention_days':31}),
    ('/api/logs/settings',{'enable_file_logging':'false'}),
])
def test_invalid_form_returns_400(live_forms,path,payload):
    assert live_forms['send'](path,payload).status_code==400

def test_clear_only_clears_memory_and_is_csrf_protected(live_forms):
    c=live_forms['client']
    assert c.post('/api/logs/clear').status_code==400
    assert live_forms['send']('/api/logs/clear',{}).status_code==200
    assert c.get('/api/logs').json['success']

def test_node_reports_labeled_metrics(live_forms):
    r=live_forms['client'].get('/api/node/info')
    assert r.status_code==200
    status=r.json['data']['status']
    assert 0<=status['cpu_percent']<=100
    assert status['active_pipelines']==len(live_forms['node'].pipeline_manager.active_pipelines)
    assert isinstance(status['buffered_errors'],int)

def test_log_limit_invalid(live_forms):
    assert live_forms['client'].get('/api/logs?limit=-1').status_code==400
