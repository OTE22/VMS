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
