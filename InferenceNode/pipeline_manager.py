
from __future__ import annotations
import sys
import os
import threading
import uuid
import json
import time
import logging
from datetime import datetime
from typing import Dict, Any, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ResultPublisher import ResultPublisher
from .pipeline import InferencePipeline

# ArmyEye's UI (and every stored pipeline config) uses these capture-type names; the
# frame-source library uses its own. Defined ONCE here and consumed by both the runtime
# and /api/frame-sources - two private copies is exactly how the builder ended up asking
# for 'ip_camera' while the API advertised 'ipcam' and reported it unavailable.
UI_TO_LIBRARY_CAPTURE_TYPE = {
    'ip_camera': 'ipcam',
    'image_folder': 'folder',
}
LIBRARY_TO_UI_CAPTURE_TYPE = {v: k for k, v in UI_TO_LIBRARY_CAPTURE_TYPE.items()}

class ControlApplyError(RuntimeError):
    """The durable setting changed, but the live runtime could not apply it."""
    saved = True


class PipelineManager:
    """Runs inference pipelines. TRANSIENT RUNTIME ONLY.

    PostgreSQL (via PipelineRepository) is the single source of truth for pipeline
    configuration. This class owns nothing persistent: threads, open frame sources,
    loaded engines/models, FPS and last-error live here and are rebuilt from the
    database whenever a pipeline is started. There is deliberately no
    pipelines_metadata.json write path - that dual store is what made persisted
    pipelines unstartable.
    """

    def __init__(self, repo_path: str, node_id: Optional[str] = None, node_name: Optional[str] = None,
                 port: Optional[int] = None, store=None):
        self.repo_path = repo_path

        # Thumbnails are managed artifacts: bytes under ARTIFACT_ROOT/thumbnails, registry
        # rows in PostgreSQL (thumbnail_registry). Pipelines write captures into the
        # STAGING area; the manager validates/hashes/promotes/registers them.
        inference_node_dir = os.path.dirname(os.path.abspath(__file__))
        self.pipelines_base_dir = os.path.join(inference_node_dir, 'pipelines')
        from . import artifact_paths as _ap
        _ap.ensure_layout()
        self.thumbnails_dir = _ap.kind_root('thumbnails')
        self.thumbnails_staging_dir = os.path.join(self.thumbnails_dir, _ap.STAGING_DIR)
        self.legacy_thumbnails_dir = os.path.join(self.pipelines_base_dir, 'thumbnails')  # migration source only
        self.pipelines_dir = self.pipelines_base_dir  # legacy alias

        # Persistence boundary. Injectable so tests can supply a fake.
        if store is None:
            from .pipeline_repository import repository as _repository
            store = _repository
        self.store = store
        self._control_lock = threading.RLock()

        # ---- transient runtime state (never persisted) ----
        self.active_pipelines = {}  # pipeline_id -> {'pipeline_instance': ..., 'started_at': ...}
        self.pipeline_threads = {}  # pipeline_id -> threading.Thread
        self.runtime_stats = {}     # pipeline_id -> last known metrics
        self.runtime_errors = {}    # pipeline_id -> last runtime error string

        # Store node info for context variables in destinations
        self.node_id = node_id
        self.node_name = node_name
        self.port = port  # Store port for dynamic webhook URLs

        self.logger = logging.getLogger(__name__)

        os.makedirs(self.pipelines_base_dir, exist_ok=True)
        os.makedirs(self.thumbnails_dir, exist_ok=True)

        # A fresh process owns no runtime. Nothing is auto-started: a stored status of
        # 'running' from a previous container is a stale value, never a resume signal.
        self.active_pipelines.clear()
        self.pipeline_threads.clear()

    # ------------------------------------------------------------------ #
    # persistence helpers (all pipeline configuration comes from Postgres)
    # ------------------------------------------------------------------ #
    def _get_config(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        """Load one pipeline definition from the database in the runtime's shape."""
        record = self.store.get(pipeline_id)
        if record is None:
            return None
        cfg = dict(record.get('config') or {})
        cfg['id'] = record['pipeline_id']
        if record.get('name') is not None:
            cfg['name'] = record['name']
        if record.get('description') is not None:
            cfg['description'] = record['description']
        cfg['status'] = record.get('status', 'stopped')
        cfg['created_date'] = cfg.get('created_date') or record.get('created_at')
        cfg['modified_date'] = cfg.get('modified_date') or record.get('updated_at')
        return cfg

    def _put_config(self, pipeline_id: str, cfg: Dict[str, Any]) -> bool:
        """Persist a definition back to Postgres. `stats` is runtime-only and dropped."""
        body = {k: v for k, v in cfg.items() if k != 'stats'}
        updated = self.store.update(pipeline_id,
                                    name=cfg.get('name'),
                                    description=cfg.get('description'),
                                    config=body,
                                    status=cfg.get('status'))
        return updated is not None

    def _exists(self, pipeline_id: str) -> bool:
        return self.store.exists(pipeline_id)

    def is_running(self, pipeline_id: str) -> bool:
        """Actual runtime truth - never derived from a stored status value."""
        entry = self.active_pipelines.get(pipeline_id)
        inst = entry.get('pipeline_instance') if entry else None
        if inst is None:
            return False
        if hasattr(inst, 'is_running'):
            try:
                return bool(inst.is_running())
            except Exception:
                return False
        return True

    def runtime_status(self, pipeline_id: str) -> str:
        """'running' | 'error' | 'stopped', from live runtime state only."""
        if self.is_running(pipeline_id):
            return 'running'
        if self.runtime_errors.get(pipeline_id):
            return 'error'
        return 'stopped'

    def _cleanup_stale_pipeline_state(self, pipeline_id: str):
        """Drop dead runtime entries and record the last-known state in the DB."""
        if pipeline_id in self.active_pipelines:
            del self.active_pipelines[pipeline_id]
        if pipeline_id in self.pipeline_threads:
            del self.pipeline_threads[pipeline_id]
        try:
            self.store.set_status(pipeline_id, 'stopped')
        except Exception as e:
            self.logger.debug(f"Could not record stopped status for {pipeline_id}: {e}")

    def get_pipeline_status(self, pipeline_id: str, record: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        """Persistent definition (Postgres) MERGED with live runtime status.

        `record` lets a caller pass a definition it already loaded, avoiding a second
        query. The reported status always comes from the runtime, never from the
        stored value.
        """
        pipeline = record if record is not None else self._get_config(pipeline_id)
        if not pipeline:
            return None

        runtime = self.active_pipelines.get(pipeline_id, {})
        pipeline_instance = runtime.get('pipeline_instance') if runtime else None

        publisher_states = self.get_pipeline_publisher_states(pipeline_id, record=pipeline)

        stats = self.runtime_stats.get(pipeline_id, {})
        if pipeline_instance and hasattr(pipeline_instance, 'get_metrics'):
            try:
                stats = pipeline_instance.get_metrics()
                self.runtime_stats[pipeline_id] = stats
            except Exception:
                pass

        return {
            'id': pipeline_id,
            'name': pipeline.get('name'),
            'status': self.runtime_status(pipeline_id),
            'stored_status': pipeline.get('status'),
            'last_error': self.runtime_errors.get(pipeline_id),
            'inference_enabled': pipeline.get('inference_enabled', True),
            'preview_enabled': False,
            'stats': stats,
            'publishers': publisher_states,
            'destinations': pipeline.get('destinations', []),
            'created_date': pipeline.get('created_date'),
            'modified_date': pipeline.get('modified_date'),
            'model': pipeline.get('model'),
            'frame_source': pipeline.get('frame_source'),
        }

    def _ensure_destination_uuid(self, dest_id: str) -> str:
        """Convert frontend destination ID to a proper UUID format"""
        if not dest_id:
            return str(uuid.uuid4())
        
        # If it's already a valid UUID format, return as-is
        try:
            uuid.UUID(dest_id)
            return dest_id
        except ValueError:
            # Convert frontend ID to a consistent UUID
            # Use a hash of the original ID to ensure consistency
            import hashlib
            hash_object = hashlib.md5(dest_id.encode())
            hex_dig = hash_object.hexdigest()
            # Convert to UUID format
            return f"{hex_dig[:8]}-{hex_dig[8:12]}-{hex_dig[12:16]}-{hex_dig[16:20]}-{hex_dig[20:32]}"

    def build_pipeline_definition(self, config: Dict[str, Any]) -> tuple:
        """Build a new pipeline definition WITHOUT persisting it.

        Persistence is the caller's job via pipeline_store.create_pipeline(), which
        enforces admin-only creation and records creator metadata. Keeping this pure
        is what removes the old JSON write path.
        """
        pipeline_id = str(uuid.uuid4())

        processed_destinations = []
        for dest in config.get('destinations', []) or []:
            processed_dest = dest.copy()
            if 'enabled' not in processed_dest:
                processed_dest['enabled'] = True
            if 'id' not in processed_dest or not processed_dest['id']:
                processed_dest['id'] = str(uuid.uuid4())
            else:
                processed_dest['id'] = self._ensure_destination_uuid(processed_dest['id'])
            processed_destinations.append(processed_dest)

        definition = {
            'id': pipeline_id,
            'name': config['name'],
            'description': config.get('description', ''),
            'frame_source': config['frame_source'],
            'model': config['model'],
            'destinations': processed_destinations,
            'created_date': datetime.now().isoformat(),
            'status': 'stopped',
            'inference_enabled': config.get('inference_enabled', True),
        }
        return pipeline_id, definition

    def build_duplicate_definition(self, source_id: str, existing_names=None) -> Optional[tuple]:
        """Clone a stored pipeline definition WITHOUT persisting it (server-authoritative
        duplicate). Reads the UNREDACTED stored config, so secrets are copied intact
        server-side; the caller must still sanitize anything it returns to a client.

          new pipeline_id      -> fresh uuid4 (never the source id, never client-supplied)
          new destination ids  -> fresh uuid4 for EVERY destination (never shared with the
                                  source - shared ids were how a duplicate could toggle
                                  the original's publisher)
          inference_enabled    -> preserved (the old client-side clone dropped it)
          name                 -> "<name> (N)" unique against `existing_names` (a courtesy;
                                  pipelines.name is not unique in the schema)
          description          -> "Copy of <original description|name>"
          created_date now, status stopped, modified_date unset, no access rows.
        """
        src = self._get_config(source_id)
        if src is None:
            return None
        base_name = src.get('name') or 'Pipeline'
        taken = set(existing_names or [])
        n = 1
        new_name = f"{base_name} ({n})"
        while new_name in taken:
            n += 1
            new_name = f"{base_name} ({n})"

        destinations = []
        for dest in src.get('destinations') or []:
            d = dict(dest)
            d['id'] = str(uuid.uuid4())
            if 'enabled' not in d:
                d['enabled'] = True
            destinations.append(d)

        pipeline_id = str(uuid.uuid4())
        definition = {
            'id': pipeline_id,
            'name': new_name,
            'description': f"Copy of {src.get('description') or base_name}",
            'frame_source': json.loads(json.dumps(src.get('frame_source') or {})),
            'model': json.loads(json.dumps(src.get('model') or {})),
            'destinations': destinations,
            'created_date': datetime.now().isoformat(),
            'status': 'stopped',
            'inference_enabled': src.get('inference_enabled', True),
        }
        return pipeline_id, definition

    def get_pipeline(self, pipeline_id: str) -> Optional[Dict[str, Any]]:
        """Get pipeline configuration from PostgreSQL."""
        return self._get_config(pipeline_id)

    def list_pipelines(self, records: Optional[list] = None) -> Dict[str, Any]:
        """List pipeline definitions merged with live runtime metrics.

        `records` are the caller's already-authorized rows (pipeline_store scopes them
        per user). When omitted this lists everything, so route handlers must pass the
        scoped set for non-admins.
        """
        pipelines_with_metrics = {}

        if records is None:
            records = self.store.list(is_admin=True)

        for record in records:
            pipeline_id = record['pipeline_id']
            pipeline_copy = dict(record.get('config') or {})
            pipeline_copy['id'] = pipeline_id
            if record.get('name') is not None:
                pipeline_copy['name'] = record['name']
            if record.get('description') is not None:
                pipeline_copy['description'] = record['description']
            pipeline_copy['created_date'] = pipeline_copy.get('created_date') or record.get('created_at')
            pipeline_copy['modified_date'] = pipeline_copy.get('modified_date') or record.get('updated_at')
            # Runtime truth wins over the stored value.
            pipeline_copy['status'] = self.runtime_status(pipeline_id)
            pipeline_copy['stored_status'] = record.get('status')
            # Which worker owns it (None = any). Every worker lists EVERY pipeline, so
            # without this the UI shows another node's cameras as if they were startable
            # here - which is exactly how the same camera gets started twice.
            pipeline_copy['node_id'] = record.get('node_id')
            pipeline_copy['stats'] = self.runtime_stats.get(pipeline_id, {
                'frame_count': 0, 'inference_count': 0, 'fps': 0, 'latency_ms': 0})

            # If pipeline is running, get real-time metrics and publisher states
            if pipeline_id in self.active_pipelines and 'pipeline_instance' in self.active_pipelines[pipeline_id]:
                pipeline_instance = self.active_pipelines[pipeline_id]['pipeline_instance']
                
                # Get real-time metrics
                if hasattr(pipeline_instance, 'get_metrics'):
                    try:
                        current_metrics = pipeline_instance.get_metrics()
                        # Update the stats with real-time data
                        pipeline_copy['stats'] = {
                            'frame_count': current_metrics.get('frame_count', 0),
                            'inference_count': current_metrics.get('inference_count', 0),
                            'fps': round(current_metrics.get('fps', 0), 1),
                            'latency_ms': round(current_metrics.get('latency_ms', 0), 1),
                            'elapsed_time': round(current_metrics.get('elapsed_time', 0), 1)
                        }
                        # Add uptime to the pipeline data
                        pipeline_copy['uptime'] = current_metrics.get('uptime', '0s')
                    except Exception as e:
                        print(f"Error getting metrics for pipeline {pipeline_id}: {e}")
                
                # Get enhanced publisher states with failure information
                if hasattr(pipeline_instance, 'get_publisher_states'):
                    try:
                        publisher_states = pipeline_instance.get_publisher_states()
                        # Update destinations with enhanced state information
                        if 'destinations' in pipeline_copy and publisher_states:
                            for dest in pipeline_copy['destinations']:
                                dest_id = dest.get('id')
                                if dest_id in publisher_states:
                                    state = publisher_states[dest_id]
                                    dest['failure_count'] = state.get('failure_count', 0)
                                    dest['auto_disabled'] = state.get('auto_disabled', False)
                                    dest['last_error'] = state.get('last_error', None)
                                    # Effective destination (webhook): what the
                                    # UI shows must be where deliveries GO, not
                                    # the stored legacy url.
                                    if state.get('effective_url') is not None:
                                        dest['effective_url'] = state['effective_url']
                                        dest['effective_mode'] = state.get('effective_mode')
                    except Exception as e:
                        print(f"Error getting publisher states for pipeline {pipeline_id}: {e}")
            
            pipelines_with_metrics[pipeline_id] = pipeline_copy
            
        return pipelines_with_metrics
    
    def get_pipeline_summary(self, records: Optional[list] = None) -> Dict[str, Any]:
        """Summary of pipelines. `records` scopes it to the caller's visible pipelines
        (None = everything, for internal callers)."""
        all_pipelines = self.list_pipelines(records=records)
        
        total_pipelines = len(all_pipelines)
        active_pipelines = len([p for p in all_pipelines.values() if p.get('status') == 'running'])
        
        # Calculate averages
        total_fps = sum(p.get('stats', {}).get('fps', 0) for p in all_pipelines.values())
        avg_fps = round(total_fps / max(active_pipelines, 1), 1) if active_pipelines > 0 else 0
        
        total_latency = sum(p.get('stats', {}).get('latency_ms', 0) for p in all_pipelines.values())
        avg_latency = round(total_latency / max(active_pipelines, 1), 1) if active_pipelines > 0 else 0
        
        # Get pipeline details for cards
        pipeline_cards = []
        for pipeline_id, pipeline_data in all_pipelines.items():
            card_data = {
                'id': pipeline_id,
                'name': pipeline_data.get('name', 'Unnamed Pipeline'),
                'description': pipeline_data.get('description', ''),
                'status': pipeline_data.get('status', 'stopped'),
                'model': pipeline_data.get('model', {}),
                'frame_source': pipeline_data.get('frame_source', {}),
                'stats': pipeline_data.get('stats', {}),
                'created_date': pipeline_data.get('created_date'),
                'inference_enabled': pipeline_data.get('inference_enabled', True)
            }
            pipeline_cards.append(card_data)
        
        return {
            'total_pipelines': total_pipelines,
            'active_pipelines': active_pipelines,
            'avg_fps': avg_fps,
            'avg_latency': avg_latency,
            'pipeline_cards': pipeline_cards
        }
    
    # Fields whose change requires a full runtime rebuild - they cannot be applied to
    # an already-running pipeline without leaving DB config and runtime out of step.
    RUNTIME_AFFECTING_FIELDS = ('frame_source', 'model', 'destinations')

    def delete_pipeline(self, pipeline_id: str) -> bool:
        """Stop the runtime and remove pipeline-specific artefacts.

        The DB row (and its access grants, via FK CASCADE) is deleted by
        pipeline_store.delete_pipeline. Shared media/models/engines are never touched.
        """
        if not self._exists(pipeline_id):
            return False

        self.stop_pipeline(pipeline_id)
        self.delete_pipeline_thumbnail(pipeline_id)
        self.runtime_stats.pop(pipeline_id, None)
        self.runtime_errors.pop(pipeline_id, None)
        return True

    def delete_pipeline_coordinated(self, user, pipeline_id: str) -> Dict[str, Any]:
        """The ONE failure-safe pipeline deletion path (route + tests use it).

        Order: authorize (admin) -> stop runtime -> registered thumbnail DELETING + JPEG
        moved to managed trash -> delete the row (FK cascade removes the thumbnail row and
        access grants) -> COMMIT -> purge trash. Outcomes (never a false success):
          {"outcome": "deleted"}                                    all done
          {"outcome": "denied"}                                     404 semantics
          {"outcome": "thumbnail_stage_failed"}                     nothing removed
          {"outcome": "db_failed"}                                  JPEG restored from trash
        """
        from . import pipeline_store as _ps
        from . import thumbnail_registry as _thumbs
        try:
            _ps.require_admin(user, "pipeline_delete")
        except _ps.AccessDenied:
            return {"outcome": "denied"}
        if not self._exists(pipeline_id):
            return {"outcome": "denied"}
        self.stop_pipeline(pipeline_id)
        try:
            handle = _thumbs.begin_delete(pipeline_id)
        except Exception as te:  # noqa: BLE001
            self.logger.error(f"thumbnail trash move failed for {pipeline_id}: {te}")
            return {"outcome": "thumbnail_stage_failed", "error": str(te)}
        try:
            _ps.delete_pipeline(user, pipeline_id)            # row + cascades, committed
        except _ps.AccessDenied:
            _thumbs.abort_delete(handle)
            return {"outcome": "denied"}
        except Exception as de:  # noqa: BLE001
            _thumbs.abort_delete(handle)                      # JPEG restored, row untouched
            self.logger.error(f"pipeline row delete failed for {pipeline_id}: {de}")
            return {"outcome": "db_failed", "error": str(de)}
        _thumbs.finish_delete(handle)                         # purge trash only after commit
        self.runtime_stats.pop(pipeline_id, None)
        self.runtime_errors.pop(pipeline_id, None)
        return {"outcome": "deleted"}

    def update_pipeline(self, pipeline_id: str, config: Dict[str, Any]) -> bool:
        """Update a pipeline definition in PostgreSQL.

        Refuses runtime-affecting edits while the pipeline is running, so the stored
        config and the live runtime can never disagree. Callers should surface
        PIPELINE_RUNNING_REQUIRES_STOP; use requires_stop_for_update() to detect it
        before calling.
        """
        pipeline_data = self._get_config(pipeline_id)
        if pipeline_data is None:
            return False

        if self.is_running(pipeline_id) and self.requires_stop_for_update(config):
            self.logger.warning(
                f"Refusing runtime-affecting edit of running pipeline {pipeline_id}")
            return False

        # The client edits a sanitize_config()-redacted copy (GET returns "***" for
        # secrets and scheme://***@host for credentialed URLs). Merge the incoming
        # sub-objects onto the STORED ones so a redacted echo never overwrites a real
        # secret, a host/path-only URL edit keeps its credential, and destinations are
        # matched by id - never by index. See pipeline_store.unredact_into.
        from .pipeline_store import unredact_into
        for key in ('frame_source', 'model'):
            if key in config:
                config[key] = unredact_into(pipeline_data.get(key), config[key])
        if 'destinations' in config:
            config['destinations'] = unredact_into(pipeline_data.get('destinations') or [],
                                                   config['destinations'])

        # Update basic info
        if 'name' in config:
            pipeline_data['name'] = config['name']
        if 'description' in config:
            pipeline_data['description'] = config['description']
        if 'frame_source' in config:
            pipeline_data['frame_source'] = config['frame_source']
        if 'model' in config:
            pipeline_data['model'] = config['model']
        if 'destinations' in config:
            # Process destinations to preserve IDs and enabled states from existing destinations
            processed_destinations = []
            existing_destinations = pipeline_data.get('destinations', [])
            
            for new_dest in config['destinations']:
                processed_dest = new_dest.copy()
                
                # First try to find existing destination by ID if provided
                existing_dest = None
                if 'id' in new_dest and new_dest['id']:
                    for existing in existing_destinations:
                        if existing.get('id') == new_dest['id']:
                            existing_dest = existing
                            break
                
                # If not found by ID, try to match by type and config
                if not existing_dest:
                    for existing in existing_destinations:
                        if (existing.get('type') == new_dest.get('type') and 
                            existing.get('config') == new_dest.get('config')):
                            existing_dest = existing
                            break
                
                if existing_dest:
                    # Preserve existing ID, but allow enabled state to be updated from frontend
                    processed_dest['id'] = existing_dest.get('id', str(uuid.uuid4()))
                    # Use the enabled state from the frontend (allow UI changes to persist)
                    processed_dest['enabled'] = processed_dest.get('enabled', existing_dest.get('enabled', True))
                else:
                    # New destination - assign new ID if not provided and default to enabled
                    if 'id' not in processed_dest or not processed_dest['id']:
                        processed_dest['id'] = str(uuid.uuid4())
                    else:
                        # Convert frontend ID to proper UUID format
                        processed_dest['id'] = self._ensure_destination_uuid(processed_dest['id'])
                    processed_dest['enabled'] = processed_dest.get('enabled', True)
                
                processed_destinations.append(processed_dest)
            
            pipeline_data['destinations'] = processed_destinations
        if 'inference_enabled' in config:
            pipeline_data['inference_enabled'] = config['inference_enabled']

        pipeline_data['modified_date'] = datetime.now().isoformat()
        return self._put_config(pipeline_id, pipeline_data)

    def requires_stop_for_update(self, config: Dict[str, Any]) -> bool:
        """True when the requested edit touches a field the runtime is built from."""
        return any(field in config for field in self.RUNTIME_AFFECTING_FIELDS)

    def _owns_pipeline(self, pipeline_id: str) -> bool:
        """Whether THIS node may run the pipeline. Unassigned pipelines belong to everyone."""
        try:
            from .pipeline_store import runnable_on_node
            record = self.store.get(pipeline_id)
            if record is None:
                return False
            return runnable_on_node(record, self.node_id)
        except Exception as e:                      # never let assignment break startup
            self.logger.debug(f"node ownership check failed, allowing start: {e}")
            return True

    def start_pipeline(self, pipeline_id: str, model_repo, result_publisher) -> bool:
        """Start a pipeline, reconstructing it entirely from PostgreSQL.

        Works with a completely empty runtime map - a pipeline persisted before a
        restart, rebuild or container recreation is startable with no in-memory state.
        """
        pipeline_config = self._get_config(pipeline_id)
        if pipeline_config is None:
            self.logger.error(f"Cannot start pipeline {pipeline_id} - no definition in PostgreSQL")
            return False

        # Worker assignment. A single process caps at ~220-250 inferences/s on the GIL, so
        # larger deployments run several nodes against this same database; without this
        # check two of them would happily start the SAME camera, double-decoding the stream
        # and publishing every detection twice. An UNASSIGNED pipeline (node_id NULL) still
        # runs anywhere, which is why existing single-node installs are unaffected.
        if not self._owns_pipeline(pipeline_id):
            record = self.store.get(pipeline_id) or {}
            self.logger.warning(
                f"Refusing to start pipeline {pipeline_id} - assigned to node "
                f"{record.get('node_id')!r}, this node is {self.node_id!r}")
            return False

        # Check if pipeline is actually running, not just in the dictionary
        if pipeline_id in self.active_pipelines:
            active_pipeline = self.active_pipelines[pipeline_id]
            # Check if the pipeline instance exists and is actually running
            if 'pipeline_instance' in active_pipeline:
                pipeline_instance = active_pipeline['pipeline_instance']
                
                # Use the pipeline's state tracking to determine if it's actually running
                if hasattr(pipeline_instance, 'is_running') and pipeline_instance.is_running():
                    self.logger.warning(f"Cannot start pipeline {pipeline_id} - already running")
                    return False
                else:
                    self.logger.info(f"Cleaning up stale pipeline {pipeline_id} entry")
                    # Clean up stale entry
                    self._cleanup_stale_pipeline_state(pipeline_id)
            else:
                self.logger.info(f"Cleaning up incomplete pipeline {pipeline_id} entry")
                # Clean up incomplete entry
                self._cleanup_stale_pipeline_state(pipeline_id)
        
        self.logger.info(f"Starting pipeline {pipeline_id} ({pipeline_config.get('name', 'Unknown')}) "
                         f"from PostgreSQL definition")
        self.runtime_errors.pop(pipeline_id, None)

        # Check if this is a folder source and log the folder path
        frame_source = pipeline_config.get('frame_source', {})
        if frame_source.get('capture_type') in ['image_folder', 'folder']:
            folder_path = frame_source.get('config', {}).get('source', 'Unknown')
            self.logger.info(f"Folder source detected - watching folder: {folder_path}")
        
        try:
            # Create a startup status indicator
            startup_status = {'started': False, 'error': None}
            
            # Create pipeline thread with startup status callback
            pipeline_thread = threading.Thread(
                target=self._run_pipeline,
                args=(pipeline_id, pipeline_config, model_repo, result_publisher, startup_status),
                daemon=True
            )
            
            # Last-known state only; the API reports runtime truth, not this value.
            self.store.set_status(pipeline_id, 'starting')
            self.active_pipelines[pipeline_id] = {
                'config': pipeline_config,
                'start_time': time.time(),
                'frame_count': 0,
                'inference_count': 0,
                'startup_status': startup_status
            }
            
            self.logger.debug(f"Starting thread for pipeline {pipeline_id}")
            # Start thread
            pipeline_thread.start()
            self.pipeline_threads[pipeline_id] = pipeline_thread
            
            # Wait for pipeline to actually start or fail (max 10 seconds)
            max_wait_time = 10  # seconds
            wait_interval = 0.1  # seconds
            total_waited = 0
            
            while total_waited < max_wait_time:
                if startup_status['started']:
                    self.store.set_status(pipeline_id, 'running')
                    self.logger.info(f"Pipeline {pipeline_id} started successfully")
                    return True
                elif startup_status['error']:
                    self.runtime_errors[pipeline_id] = str(startup_status['error'])
                    self.logger.error(f"Pipeline {pipeline_id} failed to start: {startup_status['error']}")
                    if pipeline_id in self.active_pipelines:
                        del self.active_pipelines[pipeline_id]
                    self.store.set_status(pipeline_id, 'error')
                    return False

                time.sleep(wait_interval)
                total_waited += wait_interval

            # Timeout - pipeline didn't start in time
            self.runtime_errors[pipeline_id] = 'Timed out waiting for the pipeline to start'
            self.logger.error(f"Timeout waiting for pipeline {pipeline_id} to start")
            if pipeline_id in self.active_pipelines:
                del self.active_pipelines[pipeline_id]
            self.store.set_status(pipeline_id, 'error')
            return False

        except Exception as e:
            self.runtime_errors[pipeline_id] = str(e)
            self.logger.error(f"Error starting pipeline {pipeline_id}: {e}")
            return False

    def get_start_error(self, pipeline_id: str) -> Optional[str]:
        """Last runtime start failure, for error classification at the API layer."""
        return self.runtime_errors.get(pipeline_id)

    def stop_pipeline(self, pipeline_id: str) -> bool:
        """Stop a running pipeline and release its transient runtime state."""
        try:
            if pipeline_id in self.active_pipelines and 'pipeline_instance' in self.active_pipelines[pipeline_id]:
                pipeline_instance = self.active_pipelines[pipeline_id]['pipeline_instance']
                if hasattr(pipeline_instance, 'stop'):
                    pipeline_instance.stop()
                    if pipeline_instance.is_running():
                        self.runtime_errors[pipeline_id] = "Pipeline is still stopping"
                        return False

            if pipeline_id in self.active_pipelines:
                del self.active_pipelines[pipeline_id]

            # Note: the thread stops on its next iteration when it checks active_pipelines
            if pipeline_id in self.pipeline_threads:
                del self.pipeline_threads[pipeline_id]

            self.store.set_status(pipeline_id, 'stopped')
            return True

        except Exception as e:
            self.logger.error(f"Error stopping pipeline {pipeline_id}: {e}")
            return False
    
    def _save_control(self, pipeline_id, enabled, publisher_id=None):
        # Keep commit/application order consistent within this worker. PostgreSQL's
        # row lock additionally protects saved flags across workers and sessions.
        with self._control_lock:
            if not self.store.set_control(pipeline_id, enabled, publisher_id):
                return False
            instance = self.active_pipelines.get(pipeline_id, {}).get('pipeline_instance')
            if instance is not None:
                method = ('enable_' if enabled else 'disable_') + ('inference' if publisher_id is None else 'publisher')
                try:
                    getattr(instance, method)(*(() if publisher_id is None else (publisher_id,)))
                except Exception as exc:
                    self.logger.error('Saved control could not be applied to runtime for %s', pipeline_id)
                    raise ControlApplyError('Setting saved, but runtime update failed. Retry the setting or restart the pipeline.') from exc
            return True

    def _set_inference_enabled(self, pipeline_id: str, enabled: bool) -> bool:
        return self._save_control(pipeline_id, enabled)

    def enable_pipeline_inference(self, pipeline_id: str) -> bool:
        return self._set_inference_enabled(pipeline_id, True)

    def disable_pipeline_inference(self, pipeline_id: str) -> bool:
        return self._set_inference_enabled(pipeline_id, False)

    def _set_publisher_enabled(self, pipeline_id: str, publisher_id: str, enabled: bool) -> bool:
        return self._save_control(pipeline_id, enabled, publisher_id)

    def enable_pipeline_publisher(self, pipeline_id: str, publisher_id: str) -> bool:
        return self._set_publisher_enabled(pipeline_id, publisher_id, True)

    def disable_pipeline_publisher(self, pipeline_id: str, publisher_id: str) -> bool:
        return self._set_publisher_enabled(pipeline_id, publisher_id, False)

    def get_pipeline_publisher_states(self, pipeline_id: str,
                                      record: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Configured destinations (Postgres) merged with live publisher state."""
        try:
            config = record if record is not None else self._get_config(pipeline_id)
            if config is None:
                return {}

            metadata_states = {}
            destinations = config.get('destinations', []) or []
            for dest in destinations:
                dest_id = dest.get('id')
                if dest_id:
                    metadata_states[dest_id] = {
                        'enabled': dest.get('enabled', True),
                        'type': dest.get('type', 'unknown'),
                        'configured': True
                    }
            
            # Get real-time states from running pipeline if available
            if pipeline_id in self.active_pipelines and 'pipeline_instance' in self.active_pipelines[pipeline_id]:
                pipeline_instance = self.active_pipelines[pipeline_id]['pipeline_instance']
                if hasattr(pipeline_instance, 'get_publisher_states'):
                    try:
                        runtime_states = pipeline_instance.get_publisher_states()
                        # Merge runtime states with metadata states
                        for publisher_id, runtime_state in runtime_states.items():
                            # Ensure publisher_id is a string for JSON serialization
                            str_publisher_id = str(publisher_id)
                            if str_publisher_id in metadata_states:
                                metadata_states[str_publisher_id].update(runtime_state)
                    except Exception:
                        pass  # Silently continue if runtime states fail
            
            # Ensure all keys in the final dictionary are strings for JSON serialization
            string_keyed_states = {}
            for key, value in metadata_states.items():
                string_keyed_states[str(key)] = value
            
            return string_keyed_states
        except Exception as e:
            print(f"Error in get_pipeline_publisher_states for pipeline {pipeline_id}: {e}")
            print(f"Exception type: {type(e)}")
            import traceback
            traceback.print_exc()
            return {}
    
    def get_pipeline_thumbnail_path(self, pipeline_id: str) -> Optional[str]:
        """Resolved path ONLY when the registered thumbnail is servable (AVAILABLE +
        PASSED + present + unchanged fingerprint). A file existing is not enough."""
        try:
            from . import thumbnail_registry as _thumbs
            return _thumbs.servable_path(pipeline_id)
        except Exception as e:
            self.logger.debug(f"thumbnail lookup failed for {pipeline_id}: {e}")
            return None

    def has_pipeline_thumbnail(self, pipeline_id: str) -> bool:
        return self.get_pipeline_thumbnail_path(pipeline_id) is not None

    def delete_pipeline_thumbnail(self, pipeline_id: str):
        """Standalone thumbnail removal (managed trash flow via the registry)."""
        try:
            from . import thumbnail_registry as _thumbs
            handle = _thumbs.begin_delete(pipeline_id)
            _thumbs.finish_delete(handle)
        except Exception as e:
            self.logger.error(f"Failed to delete thumbnail for pipeline {pipeline_id}: {e}")

    def _thumbnail_callback(self, pipeline_id: str):
        """Registration hook handed to the pipeline instance: staged JPEG -> validate ->
        sha256/size -> atomic promote -> PostgreSQL row AVAILABLE."""
        def _cb(staged_path):
            from . import thumbnail_registry as _thumbs
            _thumbs.register_from_staged(pipeline_id, staged_path)
        return _cb

    def generate_pipeline_thumbnail(self, pipeline_id: str) -> bool:
        """Generate a fresh thumbnail for a pipeline from its current frame"""
        try:
            # Check if pipeline exists and is running
            if pipeline_id not in self.active_pipelines:
                print(f"Pipeline {pipeline_id} is not active, cannot generate thumbnail")
                return False
            
            # Get the pipeline instance
            active_pipeline = self.active_pipelines[pipeline_id]
            if 'pipeline_instance' not in active_pipeline:
                print(f"Pipeline {pipeline_id} has no instance, cannot generate thumbnail")
                return False
            
            pipeline_instance = active_pipeline['pipeline_instance']
            
            # Get the latest frame from the pipeline
            if not hasattr(pipeline_instance, 'get_latest_frame'):
                print(f"Pipeline {pipeline_id} does not support frame access")
                return False
            
            current_frame = pipeline_instance.get_latest_frame()
            if current_frame is None:
                print(f"Pipeline {pipeline_id} has no current frame available")
                return False
            
            # Set thumbnail path if not already set
            if not hasattr(pipeline_instance, 'set_thumbnail_path') or not hasattr(pipeline_instance, 'capture_thumbnail'):
                print(f"Pipeline {pipeline_id} does not support thumbnail capture")
                return False
            
            # Ensure thumbnails directory exists
            os.makedirs(self.thumbnails_staging_dir, exist_ok=True)
            pipeline_instance._on_thumbnail_captured = self._thumbnail_callback(pipeline_id)
            pipeline_instance.set_thumbnail_path(self.thumbnails_staging_dir)
            
            # Capture the thumbnail from the current frame
            success = pipeline_instance.capture_thumbnail(current_frame)
            
            if success:
                print(f"Successfully generated thumbnail for pipeline {pipeline_id}")
                return True
            else:
                print(f"Failed to capture thumbnail for pipeline {pipeline_id}")
                return False
                
        except Exception as e:
            print(f"Error generating thumbnail for pipeline {pipeline_id}: {e}")
            return False

    def _initialize_pipeline(self, pipeline_id: str, config: Dict[str, Any], model_repo) -> InferencePipeline:
        """Initialize and configure a pipeline instance
        
        Args:
            pipeline_id: The unique identifier for the pipeline
            config: The pipeline configuration dictionary
            model_repo: The model repository to get model paths from
            
        Returns:
            Configured InferencePipeline instance
            
        Raises:
            Exception: If pipeline initialization fails
        """
        # Create pipeline instance
        pipeline = InferencePipeline()
        pipeline.id = pipeline_id
        pipeline.node_id = self.node_id or pipeline.node_id
        pipeline.pipeline_name = config.get('name', '')
        
        # Configure frame source
        frame_source_config = config['frame_source']
        # Extract the proper configuration for FrameSourceFactory
        frame_source_type = frame_source_config.get('capture_type', 'webcam')  # UI sends 'type', not 'capture_type'
        frame_source_settings = frame_source_config.get('config', {})
        
        # Apply the shared UI -> library capture-type mapping (module constant, so the
        # frame-source API and the runtime cannot drift apart).
        mapped_capture_type = UI_TO_LIBRARY_CAPTURE_TYPE.get(frame_source_type, frame_source_type)
        
        # Create the frame source configuration that FrameSourceFactory expects
        final_frame_config = {
            'capture_type': mapped_capture_type,
            **frame_source_settings
        }

        # Resolve the media reference WITHOUT rewriting the stored definition:
        #   relative_source -> MEDIA_ROOT/<relative>   (what new pipelines store)
        #   legacy absolute path that no longer exists -> unique basename match
        # Network/stream/camera sources are passed through untouched. A missing or
        # ambiguous file raises a classified MediaError rather than a generic failure.
        if mapped_capture_type in ('video_file', 'video'):
            from .media_library import resolve_frame_source
            resolution = resolve_frame_source(frame_source_config)
            final_frame_config['source'] = resolution['effective_source']
            final_frame_config.pop('relative_source', None)
            # Emitted via print() as well: this module's logger does not propagate to
            # container stdout, and these diagnostics are the only way to tell a
            # fallback apart from a config that was silently rewritten.
            line = (f"pipeline_id={pipeline_id} "
                    f"configured_source=<configured> "
                    f"effective_source=<configured> "
                    f"source_fallback={str(resolution['source_fallback']).lower()} "
                    f"fallback_reason={resolution['fallback_reason'] or 'none'}")
            print(f"[SOURCE] {line}")
            if resolution['source_fallback']:
                self.logger.warning(line)
            else:
                self.logger.info(line)

        # For folder sources, ensure watch mode is enabled to keep pipeline active
        if mapped_capture_type == 'folder':
            # Enable watch mode for folder sources to keep pipeline running even when folder is empty
            final_frame_config['watch'] = True
            print(f"Pipeline {pipeline_id}: Folder source configured with watch mode enabled")
            if not frame_source_settings.get('source'):
                print(f"Pipeline {pipeline_id}: Warning - no source folder specified for folder capture")
            else:
                source_folder = frame_source_settings.get('source')
                print(f"Pipeline {pipeline_id}: Watching folder: {source_folder}")
                # Create the folder if it doesn't exist
                if not os.path.exists(source_folder):
                    try:
                        os.makedirs(source_folder, exist_ok=True)
                        print(f"Pipeline {pipeline_id}: Created folder: {source_folder}")
                    except Exception as e:
                        print(f"Pipeline {pipeline_id}: Warning - could not create folder {source_folder}: {e}")
        
        self.logger.debug("Pipeline %s source type=%s", pipeline_id, mapped_capture_type)
        
        # Configure inference engine
        model_config = config['model']
        engine = model_config.get('engine_type', 'ultralytics')
        
        # Handle Pass engine specially - it doesn't need a model
        if engine == 'pass':
            model_path = None
        else:
            model_path = model_repo.get_model_path(model_config['id'])
            if not model_path:
                raise Exception(f"Model {model_config['id']} not found")

        # Prepare inference engine configuration
        device = model_config.get('device', 'cpu')  # Default to cpu if not specified

        # Validate device availability based on engine type
        if engine in ['geti']:
            # For OpenVINO-based engines (GETI), validate OpenVINO devices
            if device.lower() in ['gpu', 'intel:gpu']:
                # Convert to OpenVINO format and validate
                device = 'GPU'  # OpenVINO expects uppercase
                try:
                    from openvino.runtime import Core
                    core = Core()
                    available_devices = core.available_devices
                    if not any('GPU' in dev for dev in available_devices):
                        print(f"WARNING: Intel GPU requested but not available in OpenVINO. Available devices: {available_devices}. Falling back to CPU.")
                        device = 'CPU'
                except ImportError:
                    print(f"WARNING: OpenVINO not available to validate GPU device. Falling back to CPU.")
                    device = 'CPU'
                except Exception as e:
                    print(f"WARNING: Error checking OpenVINO devices: {e}. Falling back to CPU.")
                    device = 'CPU'
            elif device.lower() in ['cpu', 'intel:cpu']:
                device = 'CPU'  # OpenVINO expects uppercase
        else:
            # For PyTorch-based engines (ultralytics, torch), validate CUDA devices
            # A CUDA request that cannot be satisfied FAILS LOUDLY. Silently falling back
            # to CPU turned a misconfigured GPU into a ~20x slowdown whose only symptom was
            # one log line - the operator saw a running pipeline and healthy FPS.
            if (isinstance(device, str)
                    and (device.lower().startswith(('cuda', 'nvidia:')) or device.isdigit())):
                try:
                    import torch
                    if not torch.cuda.is_available():
                        raise RuntimeError(
                            f"Pipeline requests CUDA device '{device}' but CUDA is not available "
                            f"on this node. Refusing to start on CPU silently - set the device to "
                            f"'cpu' explicitly if CPU inference is intended.")
                except ImportError as ie:
                    raise RuntimeError(
                        f"Pipeline requests CUDA device '{device}' but PyTorch is not installed, "
                        f"so CUDA cannot be verified. Refusing to start.") from ie

        if engine == 'pass':
            # Pass engine doesn't need model_path
            inference_config = {'engine_type': engine, 'device': device}
        else:
            inference_config = {'engine_type': engine, 'model_path': model_path, 'device': device, 'task': 'detect'}

        # Explicit per-pipeline tracking options, preserving the current default tracker.
        if engine in ('ultralytics', 'torch'):
            tracking = model_config.get('tracking', True)
            inference_config['tracking'] = tracking
            if model_config.get('tracker'):
                inference_config['tracker'] = model_config['tracker']
            inference_config['tracker_buffer_seconds'] = model_config.get('tracker_buffer_seconds', 6.0)
            target = config.get('detection_config', {}).get('target_inference_fps')
            inference_config['tracking_fps'] = target if target is not None else InferencePipeline._env_target_fps() or 5.0
        if engine == 'onnx' and model_config.get('cat_map'):
            inference_config['cat_map'] = {int(k): v for k, v in model_config['cat_map'].items()}

        # Configure result publisher with destinations
        pipeline_publisher = ResultPublisher()
        for dest_config in config['destinations']:
            # Store the destination ID for later reference
            dest_id = dest_config.get('id')
            dest_enabled = dest_config.get('enabled', True)
            
            # Create destination based on type
            if dest_config['type'] == 'mqtt':
                from ResultPublisher.result_destinations import MQTTDestination
                dest = MQTTDestination()
                # Set context variables for variable substitution (including dynamic port)
                if hasattr(self, 'node_id') and hasattr(self, 'node_name'):
                    # Get port dynamically (prefer instance port, then default)
                    api_port = str(self.port) if hasattr(self, 'port') and self.port else '5555'
                    
                    dest.set_context_variables(
                        node_id=self.node_id,
                        node_name=self.node_name,
                        api_port=api_port,  # Dynamic port for webhook URLs
                        port=api_port  # Alias for convenience
                    )
                
                # Configure destination with error handling
                try:
                    dest.configure(**dest_config['config'])
                    # Set the destination ID and enabled state
                    if dest_id:
                        dest._id = dest_id
                    dest.enabled = dest_enabled
                    pipeline_publisher.add(dest)
                    print(f"Successfully configured MQTT destination: {dest_config['config'].get('server', 'unknown')}")
                except Exception as e:
                    print(f"Failed to configure MQTT destination: {str(e)} - Pipeline will continue without this destination")
            elif dest_config['type'] == 'webhook':
                from ResultPublisher.result_destinations import WebhookDestination
                dest = WebhookDestination()
                # Set context variables for variable substitution (including dynamic port)
                if hasattr(self, 'node_id') and hasattr(self, 'node_name'):
                    # Get port dynamically (prefer instance port, then default)
                    api_port = str(self.port) if hasattr(self, 'port') and self.port else '5555'
                    
                    dest.set_context_variables(
                        node_id=self.node_id,
                        node_name=self.node_name,
                        api_port=api_port,  # Dynamic port for webhook URLs
                        port=api_port  # Alias for convenience
                    )
                
                # Configure destination with error handling
                try:
                    dest.configure(**dest_config['config'])
                    # Set the destination ID and enabled state
                    if dest_id:
                        dest._id = dest_id
                    dest.enabled = dest_enabled
                    pipeline_publisher.add(dest)
                    # Report where deliveries ACTUALLY go. The stored config may
                    # still carry a legacy url (e.g. http://127.0.0.1/...) that
                    # WEBHOOK_BASE_URL overrides - printing the stored value here
                    # claimed the wrong destination for every delivery.
                    eff = dest.effective_destination()
                    ignored = (" (stored legacy url ignored)"
                               if eff['mode'] == 'base_url' and dest_config['config'].get('url') else "")
                    print(f"Webhook destination active: mode={eff['mode']} url={eff['url']}{ignored}")
                except Exception as e:
                    print(f"Failed to configure Webhook destination: {str(e)} - Pipeline will continue without this destination")
            elif dest_config['type'] == 'null':
                from ResultPublisher.result_destinations import NullDestination
                dest = NullDestination()
                # Set context variables for variable substitution
                if hasattr(self, 'node_id') and hasattr(self, 'node_name'):
                    dest.set_context_variables(
                        node_id=self.node_id,
                        node_name=self.node_name
                    )
                
                # Configure destination with error handling
                try:
                    dest.configure(**dest_config.get('config', {}))
                    # Set the destination ID and enabled state
                    if dest_id:
                        dest._id = dest_id
                    dest.enabled = dest_enabled
                    pipeline_publisher.add(dest)
                    print(f"Successfully configured Null destination")
                except Exception as e:
                    print(f"Failed to configure Null destination: {str(e)} - Pipeline will continue without this destination")
            elif dest_config['type'] == 'serial':
                from ResultPublisher.result_destinations import SerialDestination
                dest = SerialDestination()
                # Set context variables for variable substitution
                if hasattr(self, 'node_id') and hasattr(self, 'node_name'):
                    dest.set_context_variables(
                        node_id=self.node_id,
                        node_name=self.node_name
                    )
                
                # Configure destination with error handling
                try:
                    dest.configure(**dest_config['config'])
                    # Set the destination ID and enabled state
                    if dest_id:
                        dest._id = dest_id
                    dest.enabled = dest_enabled
                    pipeline_publisher.add(dest)
                    print(f"Successfully configured Serial destination: {dest_config['config'].get('com_port', 'unknown')}")
                except Exception as e:
                    print(f"Failed to configure Serial destination: {str(e)} - Pipeline will continue without this destination")
            elif dest_config['type'] == 'folder':
                from ResultPublisher.result_destinations import FolderDestination
                dest = FolderDestination()
                # Set context variables for variable substitution
                if hasattr(self, 'node_id') and hasattr(self, 'node_name'):
                    dest.set_context_variables(
                        node_id=self.node_id,
                        node_name=self.node_name
                    )
                
                # Configure destination with error handling
                try:
                    dest.configure(**dest_config['config'])
                    # Set the destination ID and enabled state
                    if dest_id:
                        dest._id = dest_id
                    dest.enabled = dest_enabled
                    pipeline_publisher.add(dest)
                    print(f"Successfully configured File destination: {dest_config['config'].get('folder_path', 'unknown')}")
                except Exception as e:
                    print(f"Failed to configure File destination: {str(e)} - Pipeline will continue without this destination")
            elif dest_config['type'] == 'roboflow':
                from ResultPublisher.result_destinations import RoboflowDestination
                dest = RoboflowDestination()
                # Set context variables for variable substitution
                if hasattr(self, 'node_id') and hasattr(self, 'node_name'):
                    dest.set_context_variables(
                        node_id=self.node_id,
                        node_name=self.node_name
                    )
                
                # Configure destination with error handling
                try:
                    dest.configure(**dest_config['config'])
                    # Set the destination ID and enabled state
                    if dest_id:
                        dest._id = dest_id
                    dest.enabled = dest_enabled
                    pipeline_publisher.add(dest)
                    print(f"Successfully configured Roboflow destination: {dest_config['config'].get('workspace_id', 'unknown')}/{dest_config['config'].get('project_id', 'unknown')}")
                except Exception as e:
                    print(f"Failed to configure Roboflow destination: {str(e)} - Pipeline will continue without this destination")
            elif dest_config['type'] == 'geti':
                from ResultPublisher.result_destinations import GetiDestination
                dest = GetiDestination()
                # Set context variables for variable substitution
                if hasattr(self, 'node_id') and hasattr(self, 'node_name'):
                    dest.set_context_variables(
                        node_id=self.node_id,
                        node_name=self.node_name
                    )
                
                # Configure destination with error handling
                try:
                    dest.configure(**dest_config['config'])
                    # Set the destination ID and enabled state
                    if dest_id:
                        dest._id = dest_id
                    dest.enabled = dest_enabled
                    pipeline_publisher.add(dest)
                    project_identifier = dest_config['config'].get('project_name') or dest_config['config'].get('project_id', 'unknown')
                    print(f"Successfully configured Geti destination: {dest_config['config'].get('host', 'unknown')} -> {project_identifier}")
                except Exception as e:
                    print(f"Failed to configure Geti destination: {str(e)} - Pipeline will continue without this destination")
            else:
                print(f"Unknown destination type: {dest_config['type']} - Skipping this destination")
            # Add more destination types as needed
        
        # Make {pipeline_id}/{pipeline_name} resolve in destination URL templates
        # (otherwise substitution falls back to 'unknown-pipeline')
        for dest in pipeline_publisher.destinations:
            dest.set_context_variables(
                pipeline_id=pipeline_id,               # Stable builder-created UUID
                pipeline_name=config.get('name', ''),  # Human-readable builder name
            )

        # Configure the pipeline
        pipeline.configure(
            frame_source_config=final_frame_config,
            inference_engine_config=inference_config,
            result_publisher=pipeline_publisher,
            detection_config=config.get('detection_config')
        )
        
        # Thumbnails: capture into staging; the callback promotes + registers (PostgreSQL)
        pipeline._on_thumbnail_captured = self._thumbnail_callback(pipeline_id)
        pipeline.set_thumbnail_path(self.thumbnails_staging_dir)
        
        # Set initial inference enabled state
        inference_enabled = config.get('inference_enabled', True)
        if inference_enabled:
            pipeline.enable_inference()
        else:
            pipeline.disable_inference()
        
        return pipeline

    def _run_pipeline(self, pipeline_id: str, config: Dict[str, Any], model_repo, result_publisher, startup_status=None):
        """Run a pipeline in a background thread
        
        Args:
            pipeline_id: The unique identifier for the pipeline
            config: The pipeline configuration dictionary
            model_repo: The model repository to get model paths from
            result_publisher: The result publisher (unused, kept for compatibility)
            startup_status: Optional dict to signal startup success/failure
        """
        try:
            # Initialize and configure the pipeline
            pipeline = self._initialize_pipeline(pipeline_id, config, model_repo)
            
            # Store the pipeline instance so we can stop it
            self.active_pipelines[pipeline_id]['pipeline_instance'] = pipeline
            
            print(f"Starting pipeline {pipeline_id}: {config['name']}")
            
            # The manager thread is the processing thread; no extra polling thread.
            pipeline.thread = threading.current_thread()
            pipeline._start_time = time.perf_counter()
            if startup_status is not None:
                startup_status['started'] = True
            pipeline.run()
            error = pipeline.get_error()
            self._cleanup_stale_pipeline_state(pipeline_id)
            if error:
                self.runtime_errors[pipeline_id] = str(error)
                self.store.set_status(pipeline_id, 'error')

            self.logger.info(f"Pipeline {pipeline_id} stopped")

        except Exception as e:
            self.logger.error(f"Pipeline {pipeline_id} error: {e}")

            # Signal startup failure
            if startup_status:
                startup_status['error'] = str(e)

            # Record the failure as RUNTIME state; the persisted definition is not
            # rewritten because of a transient runtime problem.
            self.runtime_errors[pipeline_id] = str(e)
            self._cleanup_stale_pipeline_state(pipeline_id)
            self.store.set_status(pipeline_id, 'error')

    def get_pipeline_stats(self, records: Optional[list] = None) -> Dict[str, Any]:
        """Overall pipeline statistics with real-time metrics.

        `records` = the pipelines the CALLER is allowed to see (from
        pipeline_store.list_pipelines_for_user). When given, totals/active/averages are
        computed over that set only, so a user with two grants sees "Total 2", not the
        node-wide count. When None (internal/admin callers) the full store is used.
        """
        try:
            if records is None:
                records = self.store.list(is_admin=True)
            visible = {r['pipeline_id'] for r in records}
            total_pipelines = len(records)
        except Exception:
            visible = None
            total_pipelines = 0
        active_items = [(pid, info) for pid, info in self.active_pipelines.items()
                        if visible is None or pid in visible]
        active_pipelines = len(active_items)
        
        # Calculate average FPS and latency across active pipelines using real-time data
        avg_fps = 0
        avg_latency = 0
        if active_pipelines > 0:
            total_fps = 0
            total_latency = 0
            valid_fps_count = 0
            valid_latency_count = 0
            
            for pipeline_id, pipeline_info in active_items:
                if 'pipeline_instance' in pipeline_info:
                    pipeline_instance = pipeline_info['pipeline_instance']
                    if hasattr(pipeline_instance, 'get_metrics'):
                        try:
                            metrics = pipeline_instance.get_metrics()
                            fps = metrics.get('fps', 0)
                            if fps > 0:
                                total_fps += fps
                                valid_fps_count += 1
                            
                            # Get actual inference latency from pipeline metrics
                            latency = metrics.get('latency_ms', 0)
                            if latency > 0:
                                total_latency += latency
                                valid_latency_count += 1
                        except Exception as e:
                            print(f"Error getting metrics for pipeline {pipeline_id}: {e}")
            
            if valid_fps_count > 0:
                avg_fps = total_fps / valid_fps_count
            if valid_latency_count > 0:
                avg_latency = total_latency / valid_latency_count
        
        return {
            'total': total_pipelines,
            'active': active_pipelines,
            'avg_fps': round(avg_fps, 1),
            'avg_latency': round(avg_latency, 0)
        }

