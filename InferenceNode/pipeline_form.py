"""Validate builder writes and resolve credential references without exposing secrets."""
from copy import deepcopy

from . import pipeline_store, publisher_store


def prepare_pipeline_form(data, user, source_types):
    if not isinstance(data, dict):
        raise ValueError('Pipeline configuration must be an object')
    data = deepcopy(data)
    if 'name' in data and (not isinstance(data['name'], str) or not data['name'].strip()):
        raise ValueError('Pipeline name is required')

    source_id = data.pop('source_pipeline_id', None)
    if source_id:
        stored = pipeline_store.get_pipeline_for_user(source_id, user, 'edit')['config']
        incoming = data.get('frame_source', {})
        original = stored.get('frame_source', {})
        if incoming.get('capture_type', incoming.get('type')) != original.get('capture_type', original.get('type')):
            raise ValueError('The saved camera source type has changed; reload it before testing')
        data['frame_source'] = pipeline_store.unredact_into(original, incoming)

    if 'frame_source' in data:
        source = data['frame_source']
        if not isinstance(source, dict) or not isinstance(source.get('config'), dict):
            raise ValueError('Frame source configuration is required')
        source_type = source.get('capture_type', source.get('type'))
        schema = next((s.get('config_schema') for s in source_types if s.get('type') == source_type), None)
        if not schema or not isinstance(schema.get('fields'), list):
            raise ValueError('Source type is unavailable or its configuration schema could not be loaded')
        cfg = source['config']
        for field in schema['fields']:
            if field.get('type') == 'file' or not field.get('required'):
                continue
            key = field['name']
            value = cfg.get(key, field.get('default'))
            if key == 'source' and source_type in ('video_file', 'image_folder', 'folder'):
                value = cfg.get('relative_source') or value
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValueError('Required source field: ' + str(field.get('label') or key))

    if 'destinations' in data:
        if not isinstance(data['destinations'], list):
            raise ValueError('Destinations must be a list')
        for dest in data['destinations']:
            if not isinstance(dest, dict) or not isinstance(dest.get('config'), dict):
                raise ValueError('Destination configuration must be an object')
            favorite_id = dest.pop('favorite_id', None)
            if favorite_id:
                favorite = publisher_store.get_publisher(favorite_id, runtime=True)
                if not favorite or favorite['kind'] != 'favorite' or favorite['type'] != dest.get('type'):
                    raise ValueError('Selected publisher favorite is unavailable or has changed type')
                if not favorite['secrets_ok']:
                    raise ValueError('Selected publisher favorite credentials are unavailable')
                dest['config'] = pipeline_store.unredact_into(favorite['config'], dest['config'])
    return data
