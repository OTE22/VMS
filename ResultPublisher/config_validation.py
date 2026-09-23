"""Pure configuration validation; never connects to a destination."""
import json

def normalize_config(kind, config):
    out = dict(config or {})
    if kind == 'webhook' and out.get('headers') is not None:
        headers = out['headers']
        if isinstance(headers, str):
            if headers.lstrip().startswith('{'):
                headers = json.loads(headers)
            else:
                pairs = [line.partition(':') for line in headers.splitlines() if line.strip()]
                if any(not sep or not key.strip() for key, sep, value in pairs):
                    raise ValueError('Headers must use Header: Value lines or a JSON object')
                headers = {key.strip(): value.strip() for key, sep, value in pairs}
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not k or any(c in k+v for c in '\r\n') for k,v in headers.items()):
            raise ValueError('Headers must be an object of single-line string values')
        out['headers'] = headers
    if kind == 'serial':
        value = out.pop('baud_rate', out.get('baud', 9600))
        out['baud'] = int(value)
        if out['baud'] <= 0:
            raise ValueError('Baud rate must be positive')
    return out


def validate_favorite_config(kind, config):
    """Validate saved forms without configuring or connecting a destination."""
    import math
    from ResultPublisher import get_available_destination_types
    if not isinstance(config, dict):
        raise ValueError('Configuration must be an object')
    schema = next((d for d in get_available_destination_types() if d['type'] == kind), None)
    if not schema or not schema.get('available'):
        raise ValueError('Select an available destination type')
    out = normalize_config(kind, config)
    for field in schema['config_schema']['fields']:
        key = 'baud' if kind == 'serial' and field['name'] == 'baud_rate' else field['name']
        value = out.get(key, field.get('default'))
        if value is None or value == '':
            if field.get('required'):
                raise ValueError('Required field: ' + field.get('label', key))
            continue
        typ = field['type']
        if typ == 'number':
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('Invalid number: ' + key)
            if value < field.get('min', -math.inf) or value > field.get('max', math.inf):
                raise ValueError('Out of range: ' + key)
        elif typ == 'checkbox' and not isinstance(value, bool):
            raise ValueError('Invalid boolean: ' + key)
        elif typ == 'select' and value not in [o['value'] for o in field.get('options', [])]:
            raise ValueError('Invalid selection: ' + key)
        elif typ in ('text', 'url', 'password', 'textarea') and not (key == 'headers' and isinstance(value, dict)):
            if not isinstance(value, str) or (field.get('required') and not value.strip()):
                raise ValueError('Invalid field: ' + key)
    return out
