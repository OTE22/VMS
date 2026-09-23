"""Coordinate media deletion with creation of pipeline media references."""
from sqlalchemy import text, select


def lock(session):
    if session.get_bind().dialect.name == 'postgresql':
        session.execute(text('SELECT pg_advisory_xact_lock(728194622)'))


def guard_reference(session, config, previous=None):
    lock(session)
    source = ((config or {}).get('frame_source') or {}).get('config') or {}
    old = (((previous or {}).get('frame_source') or {}).get('config') or {}).get('relative_source')
    relative = source.get('relative_source')
    if not relative or relative == old:
        return
    from .data_models import MediaAsset
    from . import artifact_paths
    import os
    row = session.execute(select(MediaAsset).where(MediaAsset.relative_path == relative)).scalar_one_or_none()
    if row is None or row.status != 'AVAILABLE' or row.validation_status != 'PASSED' or not os.path.isfile(artifact_paths.resolve('media', relative)):
        raise ValueError('Selected media is unavailable; refresh sources before saving')
