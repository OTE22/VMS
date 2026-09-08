import os
import sys
import uuid
import logging
import platform
import json
import tempfile
import shutil
import time
from datetime import datetime
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify, render_template, Response

# Import version
try:
    from ._version import __version__
except ImportError:
    # Fallback if version file is not available
    __version__ = "0.1.0"
import cv2
import zipfile

# Add InferenceEngine imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from InferenceEngine import InferenceEngineFactory


# Add parent directory to path for imports when running standalone
if __name__ == "__main__":
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, parent_dir)
else:
    # When imported as a module, also ensure parent directory is in path
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)

from ResultPublisher import ResultPublisher, ResultDestination

# Import ModelRepository
try:
    from .model_repo import ModelRepository, read_model_labels, UNSAFE_LABELS_REASON
except ImportError:
    from InferenceNode.model_repo import ModelRepository, read_model_labels, UNSAFE_LABELS_REASON

# Import HardwareDetector
try:
    from .hardware_detector import HardwareDetector
except ImportError:
    from InferenceNode.hardware_detector import HardwareDetector

# Import utility functions
try:
    from .utils import parse_windows_platform
except ImportError:
    from InferenceNode.utils import parse_windows_platform

# Import log manager
try:
    from .log_manager import LogManager
except ImportError:
    try:
        from InferenceNode.log_manager import LogManager
    except ImportError:
        LogManager = None
        print("Warning: LogManager not available")

# Import PipelineManager from pipeline module
try:
    from .pipeline_manager import PipelineManager
except ImportError:
    try:
        from InferenceNode.pipeline_manager import PipelineManager
    except ImportError:
        PipelineManager = None
        print("Warning: PipelineManager not available")

# Import DiscoveryManager from discovery_manager module
try:
    from .discovery_manager import DiscoveryManager
except ImportError:
    try:
        from InferenceNode.discovery_manager import DiscoveryManager
    except ImportError:
        DiscoveryManager = None
        print("Warning: DiscoveryManager not available")

# Try to import optional components (graceful degradation)
try:
    from .telemetry import NodeTelemetry
except ImportError:
    try:
        # Try absolute import if relative import fails
        from InferenceNode.telemetry import NodeTelemetry
    except ImportError:
        NodeTelemetry = None
        print("Warning: NodeTelemetry not available")


class InferenceNode:
    """Main inference node class that coordinates all components"""
    
    def __init__(self, node_name: Optional[str] = None, port: int = 5000, node_id: Optional[str] = None,
                 legacy_root: Optional[str] = None):
        # Track app start time
        import time
        self.app_start_time = time.time()
        
        self.node_id = node_id or str(uuid.uuid4())
        self.node_name = node_name or f"InferNode-{platform.node()}"
        self.port = port

        # LEGACY ROOT: where the one-time migrations read their pre-registry sources
        # (node_settings.json, model_repository/, media/, pipelines/thumbnails/). Defaults to
        # the InferenceNode package directory (production). Tests / E2E point it at an
        # empty isolated directory (ARMYEYE_LEGACY_ROOT) so a test node NEVER ingests
        # development settings, models, media or thumbnails.
        self.legacy_root = legacy_root or os.environ.get('ARMYEYE_LEGACY_ROOT') or os.path.dirname(os.path.abspath(__file__))
        
        # Settings file path (legacy: read once for migration to PostgreSQL)
        self.settings_file = os.path.join(self.legacy_root, 'node_settings.json')
        
        # Setup logging first
        self.log_manager = None
        if LogManager:
            self.log_manager = LogManager()
            self.log_manager.setup_logging(log_level='INFO', enable_file_logging=True)
            print("[OK] Log manager initialized")
        else:
            # Fallback to basic logging
            logging.basicConfig(
                level=logging.INFO,
                format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            print("[ERROR] Log manager not available, using basic logging")
        
        self.logger = logging.getLogger(self.__class__.__name__)

        # PostgreSQL FIRST: every registry / migration / settings consumer below needs the
        # engine and the schema at head. Fail fast if ARMYEYE_DATABASE_URL is missing.
        # (Previously the engine was only created inside setup_auth() during Flask app
        # construction, so on a fresh boot the model/media/thumbnail migrations, the
        # settings load and the startup verification all ran before the DB existed.)
        try:
            from InferenceNode.auth import db as auth_db
            from InferenceNode.auth.bootstrap import bootstrap_database
        except Exception:
            from auth import db as auth_db
            from auth.bootstrap import bootstrap_database
        auth_db.init_engine()
        bootstrap_database(legacy_root=self.legacy_root)   # 0004 -> legacy models -> 0005/head + admin seed
        
        # Hardware detection (initialize early so node capabilities can use it)
        self.hardware_detector = HardwareDetector()
        print(f"[TOOL] Hardware detection completed:")
        print(f"   Available devices: {', '.join(self.hardware_detector.available_devices)}")
        self._log_hardware_capability()

        # Core components
        self.inference_engine = None
        self.result_publisher = ResultPublisher()
        self.current_engine_info = None
        
        # Favorite publisher configurations
        # favorites: PostgreSQL-backed via publisher_store (no in-memory dict)
        
        # Model repository
        repo_path = os.path.join(self.legacy_root, 'model_repository')
        self.model_repo = ModelRepository(repo_path)

        # Media + thumbnails: one-time physical migration into ARTIFACT_ROOT (+ PostgreSQL
        # registry rows) BEFORE any pipeline resolves a media reference. Markers in
        # app_state; a blocked migration is reported and resumed on the next start.
        self._migrate_media_and_thumbnails_once()
        
        # Pipeline manager
        if PipelineManager:
            self.pipeline_manager = PipelineManager(repo_path, node_id=self.node_id, node_name=self.node_name, port=self.port)
        else:
            self.pipeline_manager = None
            print("[ERROR] Pipeline manager not available")
        
        # Discovery manager
        if DiscoveryManager:
            self.discovery_manager = DiscoveryManager()
            print(f"[OK] Discovery Manager initialized")
            print(f"   Discovery port: {self.discovery_manager.discovery_port}")
        else:
            self.discovery_manager = None
            print("[ERROR] DiscoveryManager not available")
        
        # Node capabilities and info (hardware detector is now available)
        self.node_info = self._get_node_capabilities()
        
        # Set node info in discovery manager for broadcasting
        if self.discovery_manager:
            self.discovery_manager.set_node_info(self.node_id, self.node_info)
        
        # Services (optional)
        self.telemetry = None
        
        if NodeTelemetry:
            self.telemetry = NodeTelemetry(self.node_id)
            print(f"[OK] Telemetry service initialized")
        else:
            print("[ERROR] Telemetry service not available")
        
        # Load saved settings
        self._load_settings()

        # Startup reconciliation: registry <-> filesystem over every managed plane
        # (models, engines, thumbnails, media, secrets, pipeline->model refs). Report only.
        self.last_verify_report = None
        try:
            from InferenceNode.artifact_verify import log_startup_report
            self.last_verify_report = log_startup_report(self.model_repo, self.logger)
        except Exception as e:  # noqa: BLE001 - verification must never block startup
            self.logger.error(f"[VERIFY] startup reconciliation failed: {e}")
        
        # Flask web API - use local templates and static files
        self.app = Flask(__name__, template_folder='templates', static_folder='static')
        # Use environment variable or generate a secure random key
        self.app.secret_key = os.environ.get('FLASK_SECRET_KEY') or os.urandom(24).hex()

        # Authentication: shared-DB login + roles (reuses FACE_DETECTOR's users).
        # Registers /login, /logout, the global login gate, CSRF and rate limiting.
        # admin_required is stored for admin-only routes (Phase 2).
        try:
            from InferenceNode.auth.flask_auth import setup_auth, admin_required
        except Exception:
            from auth.flask_auth import setup_auth, admin_required
        self._admin_required = admin_required
        # Admin + CSRF for state-changing, non-pipeline routes (models, publishers,
        # telemetry/node config, logs, media). Pipeline routes are guarded by
        # pipeline_authz; user/access/engine-builder routes carry their own guards.
        from InferenceNode.auth.admin_routes import _require_csrf as _csrf_check
        def _admin_csrf(view):
            from functools import wraps
            @wraps(view)
            def wrapper(*args, **kwargs):
                _csrf_check()
                return view(*args, **kwargs)
            return admin_required(wrapper)
        self._admin_csrf = _admin_csrf
        self._limiter = setup_auth(self.app, bootstrap=False)   # DB already bootstrapped in __init__

        # Admin-only guided custom-engine builder (gated by ENABLE_ENGINE_BUILDER).
        try:
            from InferenceNode.engine_builder import register_engine_builder
            from InferenceNode.auth.service import record_audit
        except Exception:
            from engine_builder import register_engine_builder
            from auth.service import record_audit
        register_engine_builder(self.app, audit_cb=record_audit)

        # Centralized per-pipeline ownership gate (below the route layer).
        try:
            from InferenceNode.pipeline_authz import register_pipeline_authz
        except Exception:
            from pipeline_authz import register_pipeline_authz
        register_pipeline_authz(self.app)

        # Admin-only pipeline assignment API (pipeline_user_access management).
        try:
            from InferenceNode.pipeline_access_routes import register_pipeline_access
        except Exception:
            from pipeline_access_routes import register_pipeline_access
        register_pipeline_access(self.app)

        self._setup_routes()

        # One-time, idempotent import of legacy pipeline JSON into the owned DB,
        # assigning ownership to the seeded admin.
        self._import_legacy_pipelines_once()
        
        self.logger.info(f"Inference node initialized: {self.node_name} ({self.node_id})")
    
    def _log_hardware_capability(self):
        """One honest line about what this node can actually execute on.

        Distinguishes the DEPLOYMENT accelerator (a Docker build/runtime choice) from
        the devices the inference runtime really enumerates, and states WHY a GPU is
        unavailable rather than silently downgrading to CPU.
        """
        accelerator = os.environ.get('ACCELERATOR', 'unset')
        devices = list(self.hardware_detector.available_devices or [])

        runtime_available, runtime_devices, reason = False, [], None
        try:
            # openvino.runtime was removed in 2026.x - Core lives at the package root.
            try:
                from openvino import Core
            except ImportError:
                from openvino.runtime import Core
            runtime_available = True
            runtime_devices = list(Core().available_devices)
        except ImportError:
            reason = 'intel_runtime_not_installed'
        except Exception as e:
            reason = f'intel_runtime_error_{e.__class__.__name__}'

        gpu_visible = any('gpu' in str(d).lower() for d in runtime_devices)
        if runtime_available and not gpu_visible and reason is None:
            # The runtime works but reports no GPU. On Docker Desktop/WSL2 that is
            # normally because no GPU device node is passed into the container.
            reason = ('device_not_exposed_to_container'
                      if not (os.path.exists('/dev/dri') or os.path.exists('/dev/dxg'))
                      else 'gpu_not_enumerated_by_runtime')

        msg = (f"deployment_accelerator={accelerator} "
               f"intel_runtime_available={str(runtime_available).lower()} "
               f"runtime_devices={runtime_devices or '[]'} "
               f"gpu_device_visible={str(gpu_visible).lower()} "
               f"available_devices={devices} "
               f"optimal_device={self.hardware_detector.get_optimal_device_for_hardware()}")
        if not gpu_visible:
            msg += f" gpu_unavailable_reason={reason or 'unknown'}"
        self.logger.info(msg)
        print(f"   {msg}")

    def _classify_pipeline_prerequisites(self, pipeline_id, definition):
        """Check media / model / engine / device before starting.

        Returns a classified error dict, or None when everything resolves. Keeps the
        configured values untouched - only reports what the runtime would actually use.
        """
        # --- frame source ---------------------------------------------------
        try:
            from InferenceNode.media_library import resolve_frame_source, MediaError, is_network_source
            frame_source = definition.get('frame_source') or {}
            capture_type = frame_source.get('capture_type')
            if capture_type in ('video_file', 'video'):
                try:
                    resolution = resolve_frame_source(frame_source)
                except MediaError as e:
                    payload = {'error': e.code, 'message': str(e)}
                    if e.extra.get('candidates'):
                        payload['candidates'] = e.extra['candidates']
                    return payload
                if resolution['source_fallback']:
                    self.logger.info(
                        f"pipeline_id={pipeline_id} will run on a fallback media path "
                        f"(stored configuration unchanged)")
        except Exception as e:
            self.logger.debug(f"Frame source pre-check skipped: {e}")

        # --- model ----------------------------------------------------------
        model = definition.get('model') or {}
        model_id = model.get('id') or model.get('model_id')
        if model_id:
            try:
                if not self.model_repo.get_model_metadata(model_id):
                    return {'error': 'MODEL_MISSING',
                            'message': f'Model "{model_id}" is not in the model repository.'}
                path = self.model_repo.get_model_path(model_id)
                if not path or not os.path.isfile(path):
                    return {'error': 'MODEL_MISSING',
                            'message': f'The file for model "{model_id}" is missing from storage.'}
            except Exception as e:
                self.logger.debug(f"Model pre-check skipped: {e}")

        # --- engine ---------------------------------------------------------
        engine_type = model.get('engine_type')
        if engine_type:
            try:
                from InferenceEngine import InferenceEngineFactory
                available = {m.get('type'): m for m in
                             InferenceEngineFactory.get_available_engines_with_metadata()}
                meta = available.get(engine_type)
                if meta is None:
                    return {'error': 'ENGINE_UNAVAILABLE',
                            'message': f'Engine "{engine_type}" is not registered on this node.'}
                if meta.get('available') is False:
                    return {'error': 'ENGINE_UNAVAILABLE',
                            'message': f'Engine "{engine_type}" is registered but its '
                                       f'dependencies are not installed on this node.'}
            except Exception as e:
                self.logger.debug(f"Engine pre-check skipped: {e}")

        # --- device ---------------------------------------------------------
        # ACCELERATOR (deployment capability) and pipeline.device (per-pipeline request)
        # are separate concepts and are never mapped onto one another. The stored device
        # is NOT rewritten when the requested one is unavailable.
        configured_device = model.get('device')
        if configured_device:
            try:
                available_devices = list(self.hardware_detector.available_devices or [])
                effective = configured_device
                fallback = False
                if available_devices and configured_device not in available_devices:
                    effective = self.hardware_detector.get_optimal_device_for_hardware()
                    fallback = True
                line = (f"pipeline_id={pipeline_id} "
                        f"deployment_accelerator={os.environ.get('ACCELERATOR', 'unset')} "
                        f"configured_pipeline_device={configured_device} "
                        f"effective_pipeline_device={effective} "
                        f"device_fallback={str(fallback).lower()} "
                        f"fallback_reason="
                        f"{'device_not_available_on_node' if fallback else 'none'}")
                print(f"[DEVICE] {line}")
                self.logger.info(line)
            except Exception as e:
                self.logger.debug(f"Device pre-check skipped: {e}")
        return None

    def _collect_frame_source_types(self):
        """Build the frame-source type list from whichever library version is installed.

        The packaged library renamed itself `frame_source` -> `framesource` and dropped
        the module-level `get_available_sources()` helper in 0.3.x. Relying on that one
        symbol made the whole endpoint fall back to a hardcoded "basic sources" list,
        which is what the Pipeline Builder was reporting. Prefer the modern factory API
        and keep the legacy helper as a fallback for older installs.
        """
        # Modern API (framesource >= 0.3): types from the factory + per-class schema.
        try:
            try:
                from framesource import FrameSourceFactory
            except ImportError:
                from frame_source import FrameSourceFactory

            try:
                from InferenceNode.pipeline_manager import LIBRARY_TO_UI_CAPTURE_TYPE
            except Exception:
                from pipeline_manager import LIBRARY_TO_UI_CAPTURE_TYPE

            # Presentation metadata the card renderer needs (type.description / type.icon /
            # type.has_discovery). The library exposes none of this, and omitting it is why
            # every card read "undefined". Keyed by LIBRARY name because that is the stable
            # identifier; the UI name is derived below.
            # has_discovery is True only for genuinely device-backed sources - file/folder/
            # screen/network sources have a discover() that returns [] by design, and
            # offering a "Discover" button that can never find anything is worse than none.
            # (description, icon, has_discovery, primary)
            # `primary` drives the quick-search badges above the selector, so it marks the
            # handful of sources an operator reaches for routinely - not every type.
            META = {
                'webcam':            ('Local webcam or capture device',        'fas fa-video',          True,  True),
                'ipcam':             ('IP camera or RTSP/HTTP network stream', 'fas fa-network-wired',  False, True),
                'video_file':        ('Recorded video file from local media',  'fas fa-file-video',     False, True),
                'folder':            ('Sequence of images in a folder',        'fas fa-folder-open',    False, True),
                'screen':            ('Screen or window capture',              'fas fa-desktop',        False, False),
                'basler':            ('Basler industrial camera (pylon)',      'fas fa-camera',         True,  False),
                'genicam':           ('GenICam/GigE Vision industrial camera', 'fas fa-camera-retro',   True,  False),
                'realsense':         ('Intel RealSense depth camera',          'fas fa-cube',           True,  False),
                'ximea':             ('XIMEA industrial camera',               'fas fa-camera',         True,  False),
                'huateng':           ('Huateng industrial camera',             'fas fa-camera',         True,  False),
                'audio_spectrogram': ('Audio input rendered as a spectrogram', 'fas fa-wave-square',    True,  False),
            }

            types = list(FrameSourceFactory.get_available_types() or [])
            sources = []
            for lib_name in types:
                # Advertise the name the UI and every STORED pipeline config already use
                # ('ip_camera', not 'ipcam'), or the builder cannot find the type and
                # reports a perfectly working source as unavailable.
                ui_name = LIBRARY_TO_UI_CAPTURE_TYPE.get(lib_name, lib_name)
                description, icon, has_discovery, primary = META.get(
                    lib_name, ('Frame source', 'fas fa-camera', False, False))
                entry = {'type': ui_name, 'name': ui_name.replace('_', ' ').title(),
                         'description': description, 'icon': icon,
                         'has_discovery': has_discovery, 'primary': primary}

                cls = None
                for attr in ('_capture_types', 'CAPTURE_TYPES', 'capture_types'):
                    registry = getattr(FrameSourceFactory, attr, None)
                    if isinstance(registry, dict) and lib_name in registry:
                        cls = registry[lib_name]
                        break

                # Availability is REPORTED, not assumed. A source whose class exposes no
                # config schema cannot be configured in the builder - usually a vendor SDK
                # that is not installed (e.g. Huateng's libMVSDK) - so say so rather than
                # advertising something that will fail at start.
                schema = None
                if cls is not None and hasattr(cls, 'get_config_schema'):
                    try:
                        schema = cls.get_config_schema()
                    except Exception as e:
                        self.logger.debug(f"No schema for frame source {lib_name}: {e}")
                if schema:
                    entry['config_schema'] = schema
                    entry['available'] = True
                else:
                    entry['available'] = False
                    entry['error'] = ('This source type is registered but not configurable on '
                                      'this node (its SDK or driver is not installed).')
                sources.append(entry)
            if sources:
                return sources
        except Exception as e:
            self.logger.debug(f"Modern frame-source enumeration unavailable: {e}")

        # Legacy API (framesource <= 0.2.x)
        try:
            from frame_source import get_available_sources
            return list(get_available_sources() or [])
        except Exception as e:
            self.logger.warning(f"Frame source enumeration failed: {e}")
            return []

    def _import_legacy_pipelines_once(self):
        """Migrate legacy pipelines_metadata.json into PostgreSQL.

        Per-pipeline_id and idempotent, so unrelated rows can never block it (the bug
        that left every real pipeline unstartable). Once verified, completion is
        recorded in app_state and this becomes a permanent no-op - the stale JSON can
        never resurrect a pipeline an admin deleted. The source file is never renamed
        or removed; a .pre-db-migration.json copy is written alongside it.
        """
        try:
            from InferenceNode.auth import db as auth_db
            from InferenceNode.auth.service import list_users
            from InferenceNode.pipeline_migration import run_migration_if_needed
        except Exception:
            return
        if not auth_db.is_configured():
            return
        try:
            legacy = os.path.join(self.legacy_root, 'pipelines', 'pipelines_metadata.json')
            wanted = (os.environ.get('ARMYEYE_IMPORT_OWNER_USERNAME') or '').strip().lower()
            users = list_users()
            owner = None
            if wanted:
                owner = next((u for u in users if u.username.lower() == wanted), None)
            if owner is None:
                owner = next((u for u in users if u.is_admin and u.is_active), None)
            if owner is None:
                self.logger.warning("Pipeline migration skipped: no admin user to record as creator")
                return

            report = run_migration_if_needed(legacy, owner.id, owner.username)
            if report.get("already_complete"):
                return
            self.logger.info(
                "Pipeline migration: imported=%d skipped_existing=%d conflicts=%d "
                "verified=%s marked_complete=%s",
                len(report.get("imported", [])), len(report.get("skipped_existing", [])),
                len(report.get("conflicts", [])), report.get("verified"),
                report.get("marked_complete"))
            for c in report.get("conflicts", []):
                self.logger.warning(
                    f"  migration_conflict pipeline_id={c['pipeline_id']} "
                    f"fields={','.join(c.get('fields', []))}")
        except Exception as e:
            self.logger.error(f"Pipeline migration failed (continuing): {e}")

    def _update_node_info_with_pipelines(self):
        """Update node_info with current pipeline information"""
        if self.pipeline_manager:
            # Only include basic pipeline stats for discovery announcements
            # Full pipeline info will be fetched by discovery server via API
            stats = self.pipeline_manager.get_pipeline_stats()
            self.node_info['pipeline_stats'] = stats
            
            # Update discovery manager with new info if it's running
            if self.discovery_manager and self.node_id:
                self.discovery_manager.set_node_info(self.node_id, self.node_info)
    
    def _get_node_capabilities(self) -> Dict[str, Any]:
        """Get node hardware capabilities"""
        try:
            import psutil
            
            # Basic system info
            capabilities = {
                "node_id": self.node_id,
                "node_name": self.node_name,
                "version": __version__,
                "platform": parse_windows_platform(platform.platform()),
                "processor": platform.processor(),
                "architecture": platform.architecture()[0],
                "cpu_count": psutil.cpu_count(),
                "memory_gb": round(psutil.virtual_memory().total / (1024**3), 2),
                "available_engines": InferenceEngineFactory.get_available_types(),
                "api_port": self.port
            }
            
            # Hardware detection using HardwareDetector
            capabilities["hardware"] = self.hardware_detector.hardware_info
            capabilities["available_devices"] = self.hardware_detector.available_devices
            capabilities["optimal_device"] = self.hardware_detector.get_optimal_device_for_hardware()
            
            return capabilities
            
        except ImportError:
            self.logger.warning("psutil not available for capability detection")
            # Fallback capabilities
            capabilities = {
                "node_id": self.node_id,
                "node_name": self.node_name,
                "version": __version__,
                "platform": parse_windows_platform(platform.platform()),
                "architecture": platform.architecture()[0],
                "processor": platform.processor(),
                # Fallbacks when psutil is unavailable
                "cpu_count": os.cpu_count() or 0,
                "memory_gb": None,
                "available_engines": InferenceEngineFactory.get_available_types(),
                "api_port": self.port
            }
            
            # Hardware detection using HardwareDetector (even without psutil)
            capabilities["hardware"] = self.hardware_detector.hardware_info
            capabilities["available_devices"] = self.hardware_detector.available_devices
            capabilities["optimal_device"] = self.hardware_detector.get_optimal_device_for_hardware()
            
            return capabilities
    
    def _setup_routes(self):
        """Setup Flask API routes"""
        
        # Web GUI Routes
        @self.app.route('/')
        def dashboard():
            """Main dashboard page"""
            return render_template('dashboard.html', node_info=self.node_info)
        
        @self.app.route('/models')
        @self._admin_required
        def models_page():
            """Model management page (admin only)"""
            return render_template('models.html', node_info=self.node_info)
        
        @self.app.route('/pipeline-builder')
        def pipeline_page():
            """Pipeline builder page"""
            return render_template('pipeline_builder.html', node_info=self.node_info)
        
        @self.app.route('/pipeline-management')
        def pipeline_management_page():
            """Pipeline management page"""
            return render_template('pipeline_management.html', node_info=self.node_info)
        
        @self.app.route('/publisher')
        def publisher_page():
            """Result publisher configuration page"""
            return render_template('publisher.html', node_info=self.node_info)
        
        @self.app.route('/telemetry')
        def telemetry_page():
            """Telemetry monitoring page"""
            return render_template('telemetry.html', node_info=self.node_info)
        
        @self.app.route('/api-docs')
        def api_docs():
            """API documentation page"""
            return render_template('api_docs.html', node_info=self.node_info)
        
        @self.app.route('/node-info')
        def node_info_page():
            """Detailed node information page"""
            return render_template('node_info.html', node_info=self.node_info)
        
        @self.app.route('/logs')
        def logs_page():
            """System logs page"""
            return render_template('logs.html', node_info=self.node_info)
        
        @self.app.route('/node-discovery')
        def node_discovery_page():
            """Node discovery page"""
            return render_template('node_discovery.html', node_info=self.node_info)
        
        # API Routes
        @self.app.route('/health', methods=['GET'])
        def health_check():
            """Health check endpoint for Docker and monitoring"""
            return jsonify({
                'status': 'healthy',
                'timestamp': datetime.now().isoformat(),
                'version': self.node_info.get('version', __version__)
            })
        
        @self.app.route('/api/info', methods=['GET'])
        def get_node_info():
            """Get node information and capabilities"""
            return jsonify(self.node_info)
        
        # Logs API Routes
        @self.app.route('/api/logs', methods=['GET'])
        def get_logs():
            """Get system logs with optional filtering"""
            try:
                if not self.log_manager or not self.log_manager.memory_handler:
                    return jsonify({'error': 'Log manager not available'}), 500
                
                # Get query parameters for filtering
                level = request.args.get('level')
                component = request.args.get('component')
                search = request.args.get('search')
                limit = request.args.get('limit', type=int)
                
                # Get filtered logs
                logs = self.log_manager.memory_handler.get_logs(
                    level=level,
                    component=component,
                    search=search,
                    limit=limit
                )
                
                # Get statistics
                stats = self.log_manager.memory_handler.get_log_statistics()
                
                return jsonify({
                    'success': True,
                    'data': {
                        'logs': logs,
                        'stats': stats,
                        'count': len(logs)
                    }
                })
                
            except Exception as e:
                self.logger.error(f"Get logs error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/logs/settings', methods=['GET'])
        def get_log_settings():
            """Get current log settings"""
            try:
                if not self.log_manager:
                    return jsonify({'error': 'Log manager not available'}), 500
                
                settings = self.log_manager.get_settings()
                return jsonify({
                    'success': True,
                    'settings': settings
                })
                
            except Exception as e:
                self.logger.error(f"Get log settings error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/logs/settings', methods=['POST'])
        @self._admin_csrf
        def update_log_settings():
            """Update log settings"""
            try:
                if not self.log_manager:
                    return jsonify({'error': 'Log manager not available'}), 500
                
                data = request.get_json()
                success = self.log_manager.update_settings(data)
                
                if success:
                    return jsonify({
                        'success': True,
                        'message': 'Log settings updated successfully'
                    })
                else:
                    return jsonify({'error': 'Failed to update log settings'}), 500
                
            except Exception as e:
                self.logger.error(f"Update log settings error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/logs/clear', methods=['POST'])
        @self._admin_csrf
        def clear_logs():
            """Clear all stored logs"""
            try:
                if not self.log_manager or not self.log_manager.memory_handler:
                    return jsonify({'error': 'Log manager not available'}), 500
                
                self.log_manager.memory_handler.clear_logs()
                
                return jsonify({
                    'success': True,
                    'message': 'All logs cleared successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Clear logs error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/node/info', methods=['GET'])
        def get_detailed_node_info():
            """Get comprehensive node information for the node info page"""
            try:
                import psutil
                import socket
                import uuid
                import time
                from datetime import datetime
                
                # Get system info
                memory = psutil.virtual_memory()
                disk = psutil.disk_usage('/')
                boot_time = psutil.boot_time()
                
                detailed_info = {
                    'success': True,
                    'data': {
                        # Basic system information
                        'node_id': self.node_id,
                        'version': __version__,
                        'platform': parse_windows_platform(platform.platform()),
                        'architecture': platform.architecture()[0],
                        'python_version': platform.python_version(),
                        'hostname': socket.gethostname(),
                        'ip_address': socket.gethostbyname(socket.gethostname()),
                        'mac_address': ':'.join(['{:02x}'.format((uuid.getnode() >> ele) & 0xff) for ele in range(0,8*6,8)][::-1]),
                        'uptime': int(time.time() - boot_time),
                        'app_uptime': int(time.time() - self.app_start_time),
                        'start_time': datetime.fromtimestamp(boot_time).strftime('%Y-%m-%d %H:%M:%S'),
                        
                        # Hardware information
                        'hardware': {
                            'cpu_model': platform.processor() or 'Unknown',
                            'cpu_cores': psutil.cpu_count(logical=False),
                            'cpu_threads': psutil.cpu_count(logical=True),
                            'cpu_freq': psutil.cpu_freq().current if psutil.cpu_freq() else None,
                            'memory_total': memory.total,
                            'memory_available': memory.available,
                            'memory_used': memory.used,
                            'disk_total': disk.total,
                            'disk_used': disk.used,
                            'disk_free': disk.free,
                            'gpu_info': self.hardware_detector.get_gpu_details(),
                            'storage_info': self.hardware_detector.get_storage_details(),
                            'resource_usage': {
                                'cpu': psutil.cpu_percent(interval=1),
                                'memory': memory.percent,
                                'disk': (disk.used / disk.total) * 100
                            }
                        },
                        
                        # Configuration
                        'config': {
                            'node_name': self.node_name,
                            'log_level': 'INFO',  # You can make this configurable
                            'web_port': self.port
                        },
                        
                        # Status
                        'status': {
                            'healthy': True,
                            'load_average': psutil.cpu_percent(interval=0.1),
                            'inference_count': len(getattr(self, 'active_pipelines', {})),
                            'error_count': 0  # You can track this
                        }
                    }
                }
                
                return jsonify(detailed_info)
                
            except Exception as e:
                self.logger.error(f"Detailed node info error: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to get detailed node info: {str(e)}'
                }), 500
        
        @self.app.route('/api/node/config', methods=['POST'])
        @self._admin_csrf
        def update_node_config():
            """Update node configuration"""
            try:
                data = request.get_json()
                if not data:
                    return jsonify({'error': 'No configuration data provided'}), 400
                
                # Update node name if provided
                if 'node_name' in data and data['node_name']:
                    old_name = self.node_name
                    self.node_name = data['node_name']
                    self.logger.info(f"Node name updated from '{old_name}' to '{self.node_name}'")
                    
                    # Update node info for discovery
                    self.node_info['node_name'] = self.node_name
                    if self.discovery_manager:
                        self.discovery_manager.set_node_info(self.node_id, self.node_info)
                
                # Update log level if provided
                if 'log_level' in data and self.log_manager:
                    try:
                        self.log_manager.setup_logging(log_level=data['log_level'], enable_file_logging=True)
                        self.logger.info(f"Log level updated to {data['log_level']}")
                    except Exception as e:
                        self.logger.warning(f"Failed to update log level: {e}")
                
                # Note: Web port changes would require restart, so we'll just log it
                if 'web_port' in data and data['web_port'] != self.port:
                    self.logger.info(f"Web port change requested to {data['web_port']} (requires restart)")
                
                # Save settings
                self._save_settings()
                
                return jsonify({
                    'success': True,
                    'message': 'Configuration updated successfully',
                    'config': {
                        'node_name': self.node_name,
                        'log_level': data.get('log_level', 'INFO'),
                        'web_port': self.port
                    }
                })
                
            except Exception as e:
                self.logger.error(f"Update node config error: {str(e)}")
                return jsonify({'error': f'Failed to update configuration: {str(e)}'}), 500
        
        @self.app.route('/api/node/restart', methods=['POST'])
        @self._admin_csrf
        def restart_node():
            """Restart the inference node"""
            try:
                self.logger.info("Restart requested via API")
                
                # Schedule restart in a separate thread
                import threading
                import subprocess
                
                def perform_restart():
                    import time
                    time.sleep(1)  # Give time for response to be sent
                    self.logger.info("Performing restart...")
                    
                    # Stop current services
                    self.stop()
                    
                    # Restart the application using the same command
                    import sys
                    import os
                    
                    # Get the original command line arguments
                    python = sys.executable
                    script = sys.argv[0]
                    
                    # On Windows, we need to use a different approach
                    if platform.system() == 'Windows':
                        # Create a batch script to restart
                        batch_content = f'@echo off\ntimeout /t 2 /nobreak > nul\nstart "" "{python}" "{script}" {" ".join(sys.argv[1:])}\n'
                        batch_file = os.path.join(tempfile.gettempdir(), 'infernode_restart.bat')
                        with open(batch_file, 'w') as f:
                            f.write(batch_content)
                        subprocess.Popen(['cmd.exe', '/c', batch_file], 
                                       creationflags=subprocess.CREATE_NEW_CONSOLE)
                    else:
                        # Unix-like systems
                        os.execv(python, [python] + sys.argv)
                    
                    # Force exit
                    os._exit(0)
                
                restart_thread = threading.Thread(target=perform_restart)
                restart_thread.daemon = True
                restart_thread.start()
                
                return jsonify({
                    'success': True,
                    'message': 'Restart initiated'
                })
                
            except Exception as e:
                self.logger.error(f"Restart error: {str(e)}")
                return jsonify({'error': f'Failed to restart: {str(e)}'}), 500
        
        @self.app.route('/api/hardware', methods=['GET'])
        def get_hardware_info():
            """Get detailed hardware information and available devices"""
            try:
                # Get Intel GPU details for enhanced display
                intel_gpu_details = self.hardware_detector.get_intel_gpu_details()
                intel_gpu_info = {}
                for device_id, details in intel_gpu_details.items():
                    intel_gpu_info[device_id] = {
                        'name': details['name'],
                        'type': details['type'],
                        'is_igpu': details['is_igpu'],
                        'friendly_name': self.hardware_detector.get_intel_gpu_friendly_name(device_id),
                        'description': self.hardware_detector.get_intel_gpu_description(device_id)
                    }
                
                # Get NVIDIA GPU details for enhanced display
                nvidia_gpu_details = self.hardware_detector.get_nvidia_gpu_details()
                nvidia_gpu_info = {}
                for device_id, details in nvidia_gpu_details.items():
                    nvidia_gpu_info[device_id] = {
                        'name': details['name'],
                        'uuid': details['uuid'],
                        'friendly_name': self.hardware_detector.get_nvidia_gpu_friendly_name(device_id),
                        'description': self.hardware_detector.get_nvidia_gpu_description(device_id)
                    }
                
                hardware_info = {
                    'detected_hardware': self.hardware_detector.hardware_info,
                    'available_devices': self.hardware_detector.available_devices,
                    'optimal_device': self.hardware_detector.get_optimal_device_for_hardware(),
                    'intel_gpu_details': intel_gpu_info,  # Add detailed Intel GPU information
                    'nvidia_gpu_details': nvidia_gpu_info,  # Add detailed NVIDIA GPU information
                    'device_capabilities': {
                        'nvidia_gpu': self.hardware_detector.has_nvidia_gpu(),
                        'nvidia_gpu_count': self.hardware_detector.get_nvidia_gpu_count(),
                        'intel_gpu': self.hardware_detector.has_intel_gpu(),
                        'intel_gpu_count': self.hardware_detector.get_intel_gpu_count(),
                        'intel_cpu': self.hardware_detector.has_intel_cpu(),
                        'intel_npu': self.hardware_detector.has_intel_npu(),
                        'amd_gpu': self.hardware_detector.has_amd_gpu(),
                        'amd_cpu': self.hardware_detector.has_amd_cpu(),
                        'apple_silicon': self.hardware_detector.has_apple_silicon(),
                        'apple_neural_engine': self.hardware_detector.has_apple_neural_engine()
                    }
                }
                return jsonify(hardware_info)
            except Exception as e:
                self.logger.error(f"Hardware info error: {str(e)}")
                return jsonify({'error': f'Failed to get hardware info: {str(e)}'}), 500
        
        @self.app.route('/api/hardware/format-device', methods=['POST'])
        def format_device_for_engine():
            """Format a device string for a specific inference engine"""
            try:
                data = request.get_json()
                if not data or 'engine' not in data or 'device' not in data:
                    return jsonify({'error': 'Missing required fields: engine and device'}), 400
                
                engine = data['engine']
                device = data['device']
                
                formatted_device = self.hardware_detector.format_for(engine, device)
                
                return jsonify({
                    'original_device': device,
                    'formatted_device': formatted_device,
                    'engine': engine
                })
                
            except Exception as e:
                self.logger.error(f"Device formatting error: {str(e)}")
                return jsonify({'error': f'Failed to format device: {str(e)}'}), 500
        
        @self.app.route('/api/models/upload', methods=['POST'])
        @self._admin_csrf
        def upload_model():
            """Upload a model file"""
            try:
                if 'file' not in request.files:
                    return jsonify({'error': 'No file provided'}), 400
                
                file = request.files['file']
                engine_type = request.form.get('engine_type', 'custom')
                description = request.form.get('description', '')
                name = request.form.get('name', '')
                
                if file.filename == '':
                    return jsonify({'error': 'No file selected'}), 400
                
                # Save uploaded file temporarily
                temp_dir = tempfile.gettempdir()
                # secure_filename: the raw client filename could contain "../" and
                # file.save() would write outside the temp dir.
                from werkzeug.utils import secure_filename
                safe_name = secure_filename(file.filename or '') or 'uploaded_model'
                temp_path = os.path.join(temp_dir, safe_name)
                file.save(temp_path)
                
                try:
                    # Store model: PostgreSQL registry (STAGING -> ... -> AVAILABLE) +
                    # ARTIFACT_ROOT bytes. Uploader identity is server-derived (never from
                    # the client) and recorded on the model row itself - the old separate
                    # write-only `record_model` shadow is gone (it was the split-brain).
                    from flask_login import current_user
                    model_id = self.model_repo.store_model(
                        temp_path,
                        file.filename or 'uploaded_model',
                        engine_type,
                        description,
                        name,
                        uploader_id=getattr(current_user, 'id', None),
                        uploader_username=getattr(current_user, 'username', None),
                    )

                    self.logger.info(f"Model uploaded successfully: {model_id}")

                    return jsonify({
                        'model_id': model_id,
                        'status': 'uploaded',
                        'message': f'Model {file.filename} uploaded successfully'
                    })
                    
                finally:
                    # Clean up temp file
                    if os.path.exists(temp_path):
                        os.remove(temp_path)
                
            except Exception as e:
                self.logger.error(f"Model upload error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/media/upload-video', methods=['POST'])
        @self._admin_csrf
        def upload_video():
            """Upload a video into the managed media root (ARTIFACT_ROOT/media) through the
            artifact creation state machine; registered in PostgreSQL (media_assets).
            The response carries the canonical RELATIVE reference only - no server paths."""
            try:
                if 'file' not in request.files:
                    return jsonify({'error': 'No file provided'}), 400
                file = request.files['file']
                if file.filename == '' or file.filename is None:
                    return jsonify({'error': 'No file selected'}), 400
                from InferenceNode import media_registry as _media
                from flask_login import current_user
                try:
                    uid = getattr(current_user, 'id', None) if current_user.is_authenticated else None
                except Exception:
                    uid = None
                try:
                    row = _media.ingest_upload(file, original_filename=file.filename, created_by=uid)
                except _media.MediaIngestError as e:
                    self._audit_event('media_upload_rejected', current_user, file.filename,
                                      {'code': e.code, 'reason': str(e)})
                    return jsonify({'error': str(e), 'code': e.code}), e.http_status
                self.logger.info(f"Video file uploaded and registered: {row['relative_path']} ({row['media_id']})")
                self._audit_event('media_uploaded', current_user, row['relative_path'],
                                  {'media_id': row['media_id'], 'original_filename': file.filename,
                                   'size_bytes': row['size_bytes'], 'sha256': row['sha256'],
                                   'media_type': row['media_type']})
                return jsonify({
                    'status': 'uploaded',
                    'media_id': row['media_id'],
                    'filename': row['relative_path'],
                    'relative_source': row['relative_path'],
                    'sha256': row['sha256'],
                    'size_bytes': row['size_bytes'],
                    'message': f'Video {file.filename} uploaded successfully'
                })
            except Exception as e:
                self.logger.error(f"Video upload error: {str(e)}")
                return jsonify({'error': 'Video upload failed'}), 500

        @self.app.route('/api/media/<media_id>', methods=['DELETE'])
        @self._admin_csrf
        def delete_media(media_id):
            """Retire a media asset (admin). Refuses with 409 while any pipeline still
            references it - media is referenced by a STRING in pipeline config, so unlike
            models there is no foreign key to refuse the delete for us. `?force=true`
            overrides deliberately and still reports which pipelines were affected."""
            try:
                from InferenceNode import media_registry as _media
                force = (request.args.get('force', '') or '').strip().lower() in ('1', 'true', 'yes')
                from flask_login import current_user
                r = _media.delete_media(media_id, force=force)
                outcome = r.get('outcome')
                if outcome == 'not_found':
                    return jsonify({'error': 'Media not found', 'media_id': media_id}), 404
                if outcome == 'referenced':
                    return jsonify({'error': 'Media is referenced by pipelines and cannot be deleted',
                                    'media_id': media_id, 'relative_path': r.get('relative_path'),
                                    'pipelines': r.get('pipelines'),
                                    'hint': 'repoint or delete those pipelines, or pass ?force=true'}), 409
                if outcome != 'deleted':
                    self.logger.error(f"Media delete failed for {media_id}: {r.get('error')}")
                    return jsonify({'error': 'Delete failed; the media asset is unchanged'}), 500
                self._audit_event('media_deleted', current_user, r.get('relative_path'),
                                  {'media_id': media_id, 'forced': force,
                                   'was_referenced_by': [p['pipeline_id'] for p in (r.get('was_referenced_by') or [])]})
                return jsonify({'status': 'deleted', 'media_id': media_id,
                                'relative_path': r.get('relative_path')})
            except Exception as e:
                self.logger.error(f"Media delete error: {str(e)}")
                return jsonify({'error': 'Media delete failed'}), 500

        @self.app.route('/api/models/download-ultralytics', methods=['POST'])
        @self._admin_csrf
        def download_ultralytics_model():
            """Download a model from Ultralytics and add it to the repository"""
            try:
                data = request.get_json()
                if not data:
                    return jsonify({'error': 'No data provided'}), 400
                
                model_name = data.get('model_name', '').strip()
                description = data.get('description', '').strip()
                name = data.get('name', '').strip()
                
                if not model_name:
                    return jsonify({'error': 'Model name is required'}), 400
                
                self.logger.info(f"Starting download of Ultralytics model: {model_name}")
                
                try:
                    # Import ultralytics - this should be available if user selected ultralytics
                    from ultralytics import YOLO
                except ImportError:
                    return jsonify({'error': 'Ultralytics package not available. Please install ultralytics: pip install ultralytics'}), 500
                
                # Track if model was downloaded to project root (for cleanup)
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                project_root_model_path = os.path.join(project_root, model_name)
                model_was_in_root_before = os.path.exists(project_root_model_path)
                
                try:
                    # Download the model using ultralytics
                    self.logger.info(f"Downloading {model_name} from Ultralytics...")
                    
                    # Initialize YOLO with the model name - this will download it automatically
                    model = YOLO(model_name)
                    
                    # Get the actual model file path after download
                    # Ultralytics downloads models to a cache directory or current directory
                    model_path = None
                    
                    # Try to get model path from the YOLO object
                    if hasattr(model, 'model_path') and isinstance(model.model_path, str):
                        model_path = model.model_path
                    elif hasattr(model, 'ckpt_path') and isinstance(model.ckpt_path, str):
                        model_path = model.ckpt_path
                    
                    if not model_path or not os.path.exists(model_path):
                        # Try to find the model in ultralytics cache
                        cache_dir = os.path.join(os.path.expanduser('~'), '.ultralytics', 'cache')
                        potential_path = os.path.join(cache_dir, model_name)
                        
                        if os.path.exists(potential_path):
                            model_path = potential_path
                        # Check if it was downloaded to project root
                        elif os.path.exists(project_root_model_path):
                            model_path = project_root_model_path
                            self.logger.info(f"Found model in project root: {project_root_model_path}")
                        else:
                            # Search for the model file in the cache directory
                            for root, dirs, files in os.walk(cache_dir):
                                for file in files:
                                    if file == model_name:
                                        model_path = os.path.join(root, file)
                                        break
                                if model_path:
                                    break
                    
                    if not model_path or not isinstance(model_path, str) or not os.path.exists(model_path):
                        return jsonify({'error': f'Failed to locate downloaded model: {model_name}'}), 500
                    
                    # Generate description if not provided
                    if not description:
                        description = f"Pre-trained {model_name} model from Ultralytics"
                    
                    # Generate name if not provided - use model name without extension
                    if not name:
                        name = os.path.splitext(model_name)[0]
                    
                    # Store the model in the repository
                    model_id = self.model_repo.store_model(
                        model_path,
                        model_name,
                        'ultralytics',  # Engine type
                        description,
                        name
                    )
                    
                    self.logger.info(f"Ultralytics model downloaded and stored successfully: {model_id}")
                    
                    return jsonify({
                        'model_id': model_id,
                        'status': 'downloaded',
                        'model_name': model_name,
                        'message': f'Model {model_name} downloaded and uploaded successfully'
                    })
                    
                except Exception as download_error:
                    self.logger.error(f"Error downloading Ultralytics model {model_name}: {str(download_error)}")
                    return jsonify({'error': f'Failed to download model: {str(download_error)}'}), 500
                    
                finally:
                    # Clean up model file from project root if it was downloaded there
                    try:
                        # Only delete if the file exists in project root AND it wasn't there before download
                        if (os.path.exists(project_root_model_path) and 
                            not model_was_in_root_before and 
                            os.path.isfile(project_root_model_path)):
                            os.remove(project_root_model_path)
                            self.logger.info(f"Cleaned up downloaded model from project root: {project_root_model_path}")
                    except Exception as cleanup_error:
                        self.logger.warning(f"Failed to cleanup model file from project root: {cleanup_error}")
                
            except Exception as e:
                self.logger.error(f"Download Ultralytics model error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/models', methods=['GET'])
        def list_models():
            """List all uploaded models"""
            try:
                models = self.model_repo.list_models()
                stats = self.model_repo.get_storage_stats()
                
                return jsonify({
                    'models': models,
                    'stats': stats
                })
                
            except Exception as e:
                self.logger.error(f"List models error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/registry/verify', methods=['GET'])
        @self._admin_required
        def verify_registry():
            """Unified reconciliation over models / engines / thumbnails / media / secrets /
            pipeline->model references. Report only (nothing deleted or repaired); admin.
            No host paths are returned - only relative artifact paths and ids."""
            try:
                from InferenceNode.artifact_verify import verify_all
                report = verify_all(self.model_repo)
                self.last_verify_report = report
                return jsonify(report)
            except Exception as e:
                self.logger.error(f"Registry verify error: {str(e)}")
                return jsonify({'error': 'Registry verification failed'}), 500

        @self.app.route('/api/models/verify', methods=['GET'])
        @self._admin_required
        def verify_models():
            """Registry <-> filesystem reconciliation for models. Report only: identifies
            AVAILABLE+missing / hash-mismatch / STAGING leftovers / DELETING trash /
            orphan files / degraded multi-file representations. Never deletes."""
            try:
                return jsonify(self.model_repo.verify())
            except Exception as e:
                self.logger.error(f"Model verify error: {str(e)}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/models/<model_id>', methods=['GET'])
        def get_model_info(model_id):
            """Get detailed information about a specific model"""
            try:
                metadata = self.model_repo.get_model_metadata(model_id)
                if not metadata:
                    return jsonify({'error': 'Model not found'}), 404
                
                return jsonify(metadata)
                
            except Exception as e:
                self.logger.error(f"Get model info error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/models/<model_id>/labels', methods=['GET'])
        @self._admin_required
        def get_model_labels(model_id):
            """Best-effort class labels for a stored model, used by the engine
            builder to prefill class mapping.

            All the safety rules live in model_repo.read_model_labels: resolution
            by repository id only (no caller-supplied paths), containment check,
            metadata-only ONNX read, no GPU load, and an honest null when labels
            cannot be obtained safely. The wizard degrades to manual mapping - it
            must never fail because of this endpoint.
            """
            try:
                labels, reason = read_model_labels(self.model_repo, model_id)
            except KeyError:
                return jsonify({'error': 'Model not found'}), 404
            if not labels:
                return jsonify({'labels': None, 'reason': reason or UNSAFE_LABELS_REASON})
            return jsonify({'labels': labels, 'count': len(labels)})

        @self.app.route('/api/models/<model_id>', methods=['DELETE'])
        @self._admin_csrf
        def delete_model(model_id):
            """Delete a model from the repository"""
            try:
                # Dependency protection: never silently break stored pipelines. The
                # pipeline->model relationship lives in PostgreSQL (config.model.id today,
                # pipelines.model_id FK after migration 0005).
                try:
                    from InferenceNode.pipeline_repository import repository as _prepo
                    users_of_model = [r.get('name') or r['pipeline_id'] for r in _prepo.list(is_admin=True)
                                      if ((r.get('config') or {}).get('model') or {}).get('id') == model_id]
                except Exception as _e:
                    self.logger.error(f"Model dependency check failed, refusing delete: {_e}")
                    return jsonify({'error': 'Dependency check unavailable; delete refused'}), 503
                if users_of_model:
                    return jsonify({'error': 'Model is referenced by pipelines and cannot be deleted',
                                    'pipelines': users_of_model}), 409
                success = self.model_repo.delete_model(model_id)
                if not success:
                    return jsonify({'error': 'Model not found or could not be deleted'}), 404
                
                return jsonify({
                    'status': 'deleted',
                    'model_id': model_id,
                    'message': f'Model {model_id} deleted successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Delete model error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/publisher/configure', methods=['POST'])
        @self._admin_csrf
        def configure_publisher():
            """Configure result publisher destinations"""
            try:
                data = request.get_json()
                destination_type = data.get('type')
                config = data.get('config', {})
                
                # Extract rate_limit from config if present
                rate_limit = config.pop('rate_limit', None)
                
                destination = ResultDestination(destination_type)
                
                # Set context variables for variable substitution (including dynamic port)
                destination.set_context_variables(
                    node_id=self.node_id,
                    node_name=self.node_name,
                    api_port=str(self.port),  # Dynamic port for webhook URLs
                    port=str(self.port)  # Alias for convenience
                )
                
                # Configure destination with error handling
                try:
                    destination.configure(**config)
                except Exception as config_error:
                    self.logger.error(f"Failed to configure {destination_type} destination: {str(config_error)}")
                    return jsonify({
                        'error': f'Configuration failed: {str(config_error)}',
                        'type': destination_type
                    }), 400
                
                # Only proceed if configuration succeeded
                if not destination.is_configured:
                    return jsonify({
                        'error': f'{destination_type} destination configuration failed - check logs for details',
                        'type': destination_type
                    }), 400
                
                # Set rate limit if provided
                if rate_limit is not None:
                    destination.set_rate_limit(rate_limit)
                
                publisher_id = self.result_publisher.add(destination)
                
                # Save settings after adding publisher
                self._save_settings()
                
                return jsonify({
                    'status': 'configured', 
                    'type': destination_type,
                    'id': publisher_id
                })
                
            except Exception as e:
                self.logger.error(f"Publisher configuration error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        # UNUSED: No frontend calls this endpoint
        # @self.app.route('/api/telemetry/start', methods=['POST'])
        # def start_telemetry():
        #     """Start telemetry publishing"""
        #     try:
        #         data = request.get_json() or {}
        #         
        #         if 'mqtt' in data:
        #             mqtt_config = data['mqtt']
        #             if self.telemetry:
        #                 self.telemetry.configure_mqtt(**mqtt_config)
        #         
        #         if self.telemetry:
        #             self.telemetry.start_telemetry()
        #         return jsonify({'status': 'telemetry started'})
        #         
        #     except Exception as e:
        #         self.logger.error(f"Telemetry start error: {str(e)}")
        #         return jsonify({'error': str(e)}), 500
        
        # UNUSED: No frontend calls this endpoint
        # @self.app.route('/api/telemetry/stop', methods=['POST'])
        # def stop_telemetry():
        #     """Stop telemetry publishing"""
        #     if self.telemetry:
        #         self.telemetry.stop_telemetry()
        #     return jsonify({'status': 'telemetry stopped'})
        
        @self.app.route('/api/telemetry/configure', methods=['POST'])
        @self._admin_csrf
        def configure_telemetry():
            """Configure telemetry settings"""
            try:
                data = request.get_json()
                
                if not self.telemetry:
                    return jsonify({'error': 'Telemetry service not available'}), 400
                
                enabled = data.get('enabled', True)
                publish_interval = data.get('publish_interval', 30)
                mqtt_server = data.get('mqtt_server', '')
                mqtt_port = data.get('mqtt_port', 1883)
                mqtt_topic = data.get('mqtt_topic', 'infernode/telemetry')
                # MQTT credentials: stored ENCRYPTED in PostgreSQL, redacted on GET. A '***'
                # echo keeps the stored value; null clears; a new value replaces.
                from InferenceNode import node_settings_store as _nss
                from InferenceNode.pipeline_store import unredact_into as _unredact
                stored_tel = _nss.get_setting(_nss.KEY_TELEMETRY, runtime=True) or {}
                creds = _unredact({k: stored_tel.get(k) for k in ('mqtt_username', 'mqtt_password')},
                                  {k: data.get(k) for k in ('mqtt_username', 'mqtt_password') if k in data})
                mqtt_username = creds.get('mqtt_username') if creds else stored_tel.get('mqtt_username')
                mqtt_password = creds.get('mqtt_password') if creds else stored_tel.get('mqtt_password')
                
                # Configure MQTT if server is provided
                if mqtt_server:
                    try:
                        self.telemetry.mqtt_username = mqtt_username
                        self.telemetry.mqtt_password = mqtt_password
                        self.telemetry.configure_mqtt(
                            mqtt_server=mqtt_server,
                            mqtt_port=int(mqtt_port),
                            mqtt_topic=mqtt_topic,
                            mqtt_username=mqtt_username,
                            mqtt_password=mqtt_password
                        )
                    except Exception as e:
                        return jsonify({'error': f'Failed to configure MQTT: {str(e)}'}), 400
                
                # Configure publish interval (update the update_interval attribute)
                if hasattr(self.telemetry, 'update_interval'):
                    self.telemetry.update_interval = float(publish_interval)
                
                # Start or stop telemetry based on enabled flag
                if enabled:
                    self.telemetry.start_telemetry()
                else:
                    self.telemetry.stop_telemetry()
                
                # Save settings after configuring telemetry
                self._save_settings()
                
                return jsonify({
                    'status': 'configured',
                    'enabled': enabled,
                    'publish_interval': publish_interval,
                    'mqtt_server': mqtt_server,
                    'mqtt_port': mqtt_port,
                    'mqtt_topic': mqtt_topic
                })
                
            except Exception as e:
                self.logger.error(f"Telemetry configuration error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/telemetry/config', methods=['GET'])
        def get_telemetry_config():
            """Get current telemetry configuration"""
            try:
                if not self.telemetry:
                    return jsonify({'error': 'Telemetry service not available'}), 400
                
                # Return current telemetry configuration
                config = {
                    'enabled': getattr(self.telemetry, 'running', False),
                    'publish_interval': getattr(self.telemetry, 'update_interval', 30),
                    'mqtt_server': getattr(self.telemetry, 'mqtt_server', ''),
                    'mqtt_port': getattr(self.telemetry, 'mqtt_port', 1883),
                    'mqtt_topic': getattr(self.telemetry, 'mqtt_topic', 'infernode/telemetry'),
                    # never the values - only whether credentials are configured (redacted)
                    'mqtt_username': '***' if getattr(self.telemetry, 'mqtt_username', None) else '',
                    'mqtt_password': '***' if getattr(self.telemetry, 'mqtt_password', None) else '',
                }
                
                return jsonify(config)
                
            except Exception as e:
                self.logger.error(f"Get telemetry config error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/telemetry', methods=['GET'])
        def get_telemetry_data():
            """Get current telemetry data"""
            try:
                if not self.telemetry:
                    # Return mock data if telemetry service not available
                    return jsonify({
                        'metrics': {
                            'cpu': 0,
                            'memory': 0,
                            'disk': 0,
                            'temperature': None
                        },
                        'system': {
                            'uptime': int(time.time() - self.app_start_time),
                            'node_id': self.node_id,
                            'platform': parse_windows_platform(platform.platform()),
                            'cpu_cores': self.node_info.get('cpu_count', 0),
                            'total_memory': (self.node_info.get('memory_gb') or 0) * 1024**3,
                            'disk_space': 0,
                            'gpu_info': 'Not available'
                        },
                        'network': {
                            'ip_address': 'Unknown',
                            'hostname': platform.node(),
                            'usage_percent': 0,
                            'bytes_recv': 0,
                            'bytes_sent': 0
                        }
                    })
                
                # Get telemetry data from telemetry service
                system_info = self.telemetry.get_system_info()
                
                # Transform the data to match what the frontend expects
                telemetry_data = {
                    'metrics': {
                        'cpu': system_info.get('cpu', {}).get('usage_percent', 0),
                        'memory': system_info.get('memory', {}).get('usage_percent', 0),
                        'disk': system_info.get('disk', {}).get('usage_percent', 0),
                        'temperature': system_info.get('cpu', {}).get('temperature_c', None)
                    },
                    'system': {
                        'uptime': int(time.time() - self.app_start_time),
                        'node_id': self.node_id,
                        'platform': system_info.get('system', {}).get('platform', parse_windows_platform(platform.platform())),
                        'cpu_cores': system_info.get('cpu', {}).get('count', 0),
                        'total_memory': system_info.get('memory', {}).get('total_gb', 0) * 1024**3,
                        'disk_space': system_info.get('disk', {}).get('total_gb', 0) * 1024**3,
                        'gpu_info': str(system_info.get('gpu', {}).get('devices', 'Not available'))
                    },
                    'network': {
                        'ip_address': 'Unknown',  # Placeholder
                        'hostname': platform.node(),
                        'usage_percent': 0,  # Placeholder
                        'bytes_recv': system_info.get('network', {}).get('bytes_recv', 0),
                        'bytes_sent': system_info.get('network', {}).get('bytes_sent', 0)
                    },
                    # Structured GPU payload for tooling. system.gpu_info above is a
                    # stringified copy kept for the existing UI; this is the parseable one,
                    # per device, which per-GPU benchmarking requires.
                    'gpu': system_info.get('gpu', {}),
                    'memory_detail': system_info.get('memory', {}),
                }
                
                return jsonify(telemetry_data)
                
            except Exception as e:
                self.logger.error(f"Get telemetry data error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/publisher/test', methods=['POST'])
        @self._admin_csrf
        def test_publish():
            """Test publishing a message to all configured destinations"""
            try:
                data = request.get_json()
                message = data.get('message', {})
                
                if not message:
                    return jsonify({'error': 'No message provided'}), 400
                
                # Check if any destinations are configured
                if not self.result_publisher.destinations:
                    return jsonify({
                        'status': 'warning',
                        'message': 'No destinations configured - cannot publish test message',
                        'results': {},
                        'destinations_count': 0
                    })
                
                # Add metadata to the test message
                test_message = {
                    'test': True,
                    'node_id': self.node_id,
                    'node_name': self.node_name,
                    'timestamp': data.get('timestamp') or message.get('timestamp'),
                    'data': message
                }
                
                # Publish to all configured destinations
                results = self.result_publisher.publish(test_message)
                
                return jsonify({
                    'status': 'success',
                    'message': 'Test message published',
                    'results': results,
                    'destinations_count': len(self.result_publisher.destinations)
                })
                
            except Exception as e:
                self.logger.error(f"Test publish error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/publisher/test-favorites', methods=['POST'])
        @self._admin_csrf
        def test_publish_favorites():
            """Test publishing a message to selected favorite destinations"""
            try:
                data = request.get_json()
                message = data.get('message', {})
                favorite_ids = data.get('favorite_ids', [])
                
                if not message:
                    return jsonify({'error': 'No message provided'}), 400
                
                if not favorite_ids:
                    return jsonify({'error': 'No favorite destinations selected'}), 400
                
                # Get selected favorites (RUNTIME view: decrypted secrets, PostgreSQL)
                from InferenceNode import publisher_store as _pst
                selected_favorites = []
                for fav_id in favorite_ids:
                    fav = _pst.get_publisher(fav_id, runtime=True)
                    if fav and fav.get('kind') == 'favorite':
                        if not fav.get('secrets_ok', True):
                            return jsonify({'error': f'Favorite {fav_id}: encryption key unavailable, '
                                                     f'cannot use its credentials'}), 503
                        selected_favorites.append(fav)
                
                if not selected_favorites:
                    return jsonify({
                        'status': 'warning',
                        'message': 'No valid favorite destinations found',
                        'destinations_count': 0
                    })
                
                # Add metadata to the test message
                test_message = {
                    'test': True,
                    'node_id': self.node_id,
                    'node_name': self.node_name,
                    'timestamp': data.get('timestamp') or message.get('timestamp'),
                    'data': message
                }
                
                # Create temporary destinations from favorites and publish
                temp_destinations = []
                for favorite in selected_favorites:
                    try:
                        destination = ResultDestination(favorite['type'])
                        destination.set_context_variables(
                            node_id=self.node_id,
                            node_name=self.node_name
                        )
                        destination.configure(**favorite['config'])
                        temp_destinations.append(destination)
                    except Exception as e:
                        self.logger.error(f"Failed to create destination for favorite {favorite.get('name', 'unknown')}: {str(e)}")
                
                # Publish using temporary destinations
                results = {}
                for dest in temp_destinations:
                    try:
                        result = dest.publish(test_message)
                        results[dest.__class__.__name__] = result
                    except Exception as e:
                        results[dest.__class__.__name__] = {'error': str(e)}
                
                return jsonify({
                    'status': 'success',
                    'message': f'Test message sent to {len(temp_destinations)} favorite destination(s)',
                    'results': results,
                    'destinations_count': len(temp_destinations)
                })
                
            except Exception as e:
                self.logger.error(f"Test publish favorites error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        # @self.app.route('/api/publisher/destinations', methods=['GET'])
        # def get_destinations():
        #     """Get list of configured destinations"""
        #     try:
        #         destinations = []
        #         for dest in self.result_publisher.destinations:
        #             # Determine destination type from attributes
        #             dest_type = 'unknown'
        #             if hasattr(dest, 'server') and hasattr(dest, 'port') and hasattr(dest, 'topic'):
        #                 dest_type = 'mqtt'
        #             elif hasattr(dest, 'url'):
        #                 dest_type = 'webhook'
        #             elif hasattr(dest, 'com_port'):
        #                 dest_type = 'serial'
        #             elif hasattr(dest, 'file_path'):
        #                 dest_type = 'file'
                    
        #             dest_info = {
        #                 'id': getattr(dest, '_id', None),
        #                 'type': dest_type,
        #                 'configured': dest.is_configured,
        #                 'rate_limit': dest.rate_limit,
        #                 'include_image_data': getattr(dest, 'include_image_data', False)
        #             }
                    
        #             # Add specific config info based on destination type
        #             if dest_type == 'mqtt':
        #                 # MQTT destination
        #                 server = getattr(dest, 'server', 'unknown')
        #                 port = getattr(dest, 'port', 1883)
        #                 topic = getattr(dest, 'topic', 'unknown')
        #                 dest_info['details'] = f"{server}:{port}/{topic}"
        #                 dest_info['config'] = {
        #                     'server': server,
        #                     'port': port,
        #                     'topic': topic,
        #                     'username': getattr(dest, 'username', ''),
        #                     'password': '***' if getattr(dest, 'password', '') else '',
        #                     'include_image_data': getattr(dest, 'include_image_data', False)
        #                 }
        #             elif dest_type == 'webhook':
        #                 # Webhook destination
        #                 url = getattr(dest, 'url', 'unknown')
        #                 dest_info['details'] = url
        #                 dest_info['config'] = {
        #                     'url': url,
        #                     'timeout': getattr(dest, 'timeout', 30),
        #                     'include_image_data': getattr(dest, 'include_image_data', False)
        #                 }
        #             elif dest_type == 'serial':
        #                 # Serial destination
        #                 com_port = getattr(dest, 'com_port', 'unknown')
        #                 baud = getattr(dest, 'baud', 9600)
        #                 dest_info['details'] = f"{com_port} @ {baud} baud"
        #                 dest_info['config'] = {
        #                     'com_port': com_port,
        #                     'baud': baud,
        #                     'include_image_data': getattr(dest, 'include_image_data', False)
        #                 }
        #             elif dest_type == 'file':
        #                 # File destination
        #                 file_path = getattr(dest, 'file_path', 'unknown')
        #                 dest_info['details'] = file_path
        #                 dest_info['config'] = {
        #                     'file_path': file_path,
        #                     'format': getattr(dest, 'format', 'json')
        #                 }
        #             else:
        #                 dest_info['details'] = "Unknown configuration"
        #                 dest_info['config'] = {}
                    
        #             destinations.append(dest_info)
                
        #         return jsonify({
        #             'destinations': destinations,
        #             'count': len(destinations)
        #         })
                
        #     except Exception as e:
        #         self.logger.error(f"Get destinations error: {str(e)}")
        #         return jsonify({'error': str(e)}), 500
        
        # @self.app.route('/api/publisher/status', methods=['GET'])
        # def get_publisher_status():
        #     """Get detailed status of all publishers including configuration"""
        #     try:
        #         publishers = []
        #         for i, dest in enumerate(self.result_publisher.destinations):
        #             # Determine destination type from attributes
        #             dest_type = 'unknown'
        #             if hasattr(dest, 'server') and hasattr(dest, 'port') and hasattr(dest, 'topic'):
        #                 dest_type = 'mqtt'
        #             elif hasattr(dest, 'url'):
        #                 dest_type = 'webhook'
        #             elif hasattr(dest, 'com_port'):
        #                 dest_type = 'serial'
        #             elif hasattr(dest, 'file_path'):
        #                 dest_type = 'file'
                    
        #             # Get configuration details
        #             config_details = {}
        #             if dest_type == 'mqtt':
        #                 config_details = {
        #                     'server': getattr(dest, 'server', 'N/A'),
        #                     'port': getattr(dest, 'port', 1883),
        #                     'topic': getattr(dest, 'topic', 'N/A'),
        #                     'username': getattr(dest, 'username', '') or 'None',
        #                     'password': '***' if getattr(dest, 'password', '') else 'None'
        #                 }
        #             elif dest_type == 'webhook':
        #                 config_details = {
        #                     'url': getattr(dest, 'url', 'N/A'),
        #                     'method': getattr(dest, 'method', 'POST'),
        #                     'headers': getattr(dest, 'headers', {})
        #                 }
        #             elif dest_type == 'serial':
        #                 config_details = {
        #                     'com_port': getattr(dest, 'com_port', 'N/A'),
        #                     'baud': getattr(dest, 'baud', 9600),
        #                     'timeout': getattr(dest, 'timeout', 1)
        #                 }
        #             elif dest_type == 'file':
        #                 config_details = {
        #                     'file_path': getattr(dest, 'file_path', 'N/A'),
        #                     'format': getattr(dest, 'format', 'json')
        #                 }
                    
        #             publishers.append({
        #                 'index': i,
        #                 'id': getattr(dest, '_id', None),
        #                 'type': dest_type,
        #                 'configured': dest.is_configured if hasattr(dest, 'is_configured') else False,
        #                 'enabled': getattr(dest, 'enabled', True),
        #                 'rate_limit': dest.rate_limit if hasattr(dest, 'rate_limit') else 0,
        #                 'failure_count': getattr(dest, 'failure_count', 0),
        #                 'max_failures': getattr(dest, 'max_failures', 5),
        #                 'auto_disabled': getattr(dest, 'failure_threshold_reached', False),
        #                 'last_failure_time': getattr(dest, 'last_failure_time', 0),
        #                 'config': config_details
        #             })
                
        #         return jsonify({
        #             'publishers': publishers,
        #             'total_count': len(publishers),
        #             'configured_count': sum(1 for p in publishers if p['configured']),
        #             'enabled_count': sum(1 for p in publishers if p['enabled']),
        #             'auto_disabled_count': sum(1 for p in publishers if p['auto_disabled'])
        #         })
                
        #     except Exception as e:
        #         self.logger.error(f"Get publisher status error: {str(e)}")
        #         return jsonify({'error': str(e)}), 500
        
        # UNUSED: No frontend calls this endpoint
        # @self.app.route('/api/publisher/clear', methods=['POST'])
        # def clear_publishers():
        #     """Clear all configured publishers"""
        #     try:
        #         self.result_publisher.clear()
        #         
        #         # Save settings after clearing publishers
        #         self._save_settings()
        #         
        #         return jsonify({'status': 'cleared', 'message': 'All publishers removed'})
        #         
        #     except Exception as e:
        #         self.logger.error(f"Clear publishers error: {str(e)}")
        #         return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/publisher/edit/<publisher_id>', methods=['PUT'])
        @self._admin_csrf
        def edit_publisher(publisher_id):
            """Edit a specific publisher by ID"""
            try:
                data = request.get_json()
                config = data.get('config', {})
                
                # Find the publisher by ID
                destination = self.result_publisher.get_by_id(publisher_id)
                if not destination:
                    return jsonify({'error': 'Publisher not found'}), 404
                
                # Extract rate_limit from config if present
                rate_limit = config.pop('rate_limit', None)
                
                # Clean up config - remove null values and empty strings
                cleaned_config = {}
                for key, value in config.items():
                    if value is not None and value != '':
                        cleaned_config[key] = value
                
                # Set context variables for variable substitution
                destination.set_context_variables(
                    node_id=self.node_id,
                    node_name=self.node_name
                )
                
                # Reconfigure the destination
                destination.configure(**cleaned_config)
                
                # Set rate limit if provided
                if rate_limit is not None:
                    destination.set_rate_limit(rate_limit)
                
                # Save settings after editing publisher
                self._save_settings()
                
                return jsonify({
                    'status': 'updated',
                    'id': publisher_id,
                    'message': 'Publisher configuration updated'
                })
                
            except Exception as e:
                self.logger.error(f"Edit publisher error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/publisher/delete/<publisher_id>', methods=['DELETE'])
        @self._admin_csrf
        def delete_publisher(publisher_id):
            """Delete a specific publisher by ID"""
            try:
                success = self.result_publisher.remove_by_id(publisher_id)
                if not success:
                    return jsonify({'error': 'Publisher not found'}), 404
                
                # Save settings after deleting publisher
                self._save_settings()
                
                return jsonify({
                    'status': 'deleted',
                    'id': publisher_id,
                    'message': 'Publisher removed successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Delete publisher error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        # UNUSED: No frontend calls this endpoint
        # @self.app.route('/api/publisher/reset-failures/<publisher_id>', methods=['POST'])
        # def reset_publisher_failures(publisher_id):
        #     """Reset failure count and re-enable an auto-disabled publisher"""
        #     try:
        #         destination = self.result_publisher.get_by_id(publisher_id)
        #         if not destination:
        #             return jsonify({'error': 'Publisher not found'}), 404
        #         
        #         # Reset failure count and re-enable if auto-disabled
        #         destination.reset_failure_count()
        #         
        #         # Save settings after resetting
        #         self._save_settings()
        #         
        #         return jsonify({
        #             'status': 'reset',
        #             'id': publisher_id,
        #             'message': 'Publisher failure count reset and re-enabled',
        #             'enabled': destination.enabled,
        #             'failure_count': destination.failure_count
        #         })
        #         
        #     except Exception as e:
        #         self.logger.error(f"Reset publisher failures error: {str(e)}")
        #         return jsonify({'error': str(e)}), 500
        
        # Favorite Configuration API Routes - PostgreSQL authoritative (publisher_store).
        # node_settings.json is no longer read or written for favorites; secrets are
        # encrypted at rest and every response is redacted (never requires the key).
        @self.app.route('/api/publisher/favorites', methods=['GET'])
        def get_favorite_configs():
            """Get all saved favorite publisher configurations (redacted)."""
            try:
                from InferenceNode import publisher_store as _pst
                return jsonify({'status': 'success', 'favorites': _pst.list_publishers(kind='favorite')})
            except Exception as e:
                self.logger.error(f"Get favorites error: {str(e)}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/publisher/favorites', methods=['POST'])
        @self._admin_csrf
        def save_favorite_config():
            """Save a publisher configuration as a favorite (PostgreSQL row; secrets encrypted)."""
            try:
                from flask_login import current_user
                from InferenceNode import publisher_store as _pst
                from InferenceNode.config_secrets import SecretsUnavailable
                data = request.get_json() or {}
                name = (data.get('name') or '').strip()
                destination_type = data.get('type')
                config = data.get('config') or {}
                if not name:
                    return jsonify({'error': 'Name is required'}), 400
                if not destination_type:
                    return jsonify({'error': 'Destination type is required'}), 400
                for existing in _pst.list_publishers(kind='favorite'):
                    if (existing.get('name') or '').lower() == name.lower():
                        return jsonify({'error': f'A favorite named "{name}" already exists'}), 400
                try:
                    favorite = _pst.create_publisher(name=name, type=destination_type, config=config,
                                                     kind='favorite', created_by=getattr(current_user, 'id', None))
                except SecretsUnavailable:
                    return jsonify({'error': 'Configuration encryption key unavailable; refusing to store a '
                                             'secret in clear (set ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE)'}), 503
                return jsonify({'status': 'saved', 'favorite': favorite,
                                'message': f'Configuration saved as favorite: {name}'})
            except Exception as e:
                self.logger.error(f"Save favorite error: {str(e)}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/publisher/favorites/<favorite_id>', methods=['DELETE'])
        @self._admin_csrf
        def delete_favorite_config(favorite_id):
            """Delete a favorite configuration"""
            try:
                from InferenceNode import publisher_store as _pst
                fav = _pst.get_publisher(favorite_id)
                if fav is None or fav.get('kind') != 'favorite':
                    return jsonify({'error': 'Favorite not found'}), 404
                _pst.delete_publisher(favorite_id)
                return jsonify({'status': 'deleted', 'id': favorite_id,
                                'message': f'Favorite "{fav.get("name")}" deleted successfully'})
            except Exception as e:
                self.logger.error(f"Delete favorite error: {str(e)}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/publisher/favorites/<favorite_id>', methods=['PUT'])
        @self._admin_csrf
        def update_favorite_config(favorite_id):
            """Update a favorite: honors name/type/config; config merge is redaction-safe
            (a '***' echo keeps the stored secret, null clears, new value replaces)."""
            try:
                from InferenceNode import publisher_store as _pst
                from InferenceNode.config_secrets import SecretsUnavailable
                fav = _pst.get_publisher(favorite_id)
                if fav is None or fav.get('kind') != 'favorite':
                    return jsonify({'error': 'Favorite not found'}), 404
                data = request.get_json() or {}
                new_name = None
                if 'name' in data:
                    new_name = (data.get('name') or '').strip()
                    if not new_name:
                        return jsonify({'error': 'Name cannot be empty'}), 400
                    for other in _pst.list_publishers(kind='favorite'):
                        if other['id'] != favorite_id and (other.get('name') or '').lower() == new_name.lower():
                            return jsonify({'error': f'A favorite named "{new_name}" already exists'}), 400
                try:
                    updated = _pst.update_publisher(favorite_id, name=new_name, type=data.get('type'),
                                                    config=data.get('config') if 'config' in data else None)
                except SecretsUnavailable:
                    return jsonify({'error': 'Configuration encryption key unavailable'}), 503
                return jsonify({'status': 'updated', 'favorite': updated,
                                'message': f'Favorite "{updated["name"]}" updated successfully'})
            except Exception as e:
                self.logger.error(f"Update favorite error: {str(e)}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/publisher/types', methods=['GET'])
        def get_publisher_types():
            """Get available publisher/destination types"""
            try:
                from ResultPublisher import get_available_destination_types
                destination_types = get_available_destination_types()
                
                return jsonify({
                    'status': 'success',
                    'destination_types': destination_types
                })
                
            except Exception as e:
                self.logger.error(f"Get publisher types error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/publisher/destination-types', methods=['GET'])
        def get_destination_types_with_schemas():
            """Get available destination types with their configuration schemas"""
            try:
                from ResultPublisher import get_available_destination_types
                destination_types = get_available_destination_types()
                
                return jsonify({
                    'status': 'success',
                    'destination_types': destination_types
                })
                
            except Exception as e:
                self.logger.error(f"Get destination types with schemas error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/media/sources', methods=['GET'])
        def list_media_sources():
            """List usable local media under MEDIA_ROOT as stable RELATIVE references.

            Rescans on every call, so a file dropped into the mounted media directory
            appears without restarting VMS. Absolute server paths are never returned.
            """
            try:
                from InferenceNode.media_library import MediaLibrary
                library = MediaLibrary()
                sources, excluded = library.list_media()
                return jsonify({
                    'status': 'success',
                    'sources': sources,
                    'count': len(sources),
                    'excluded': excluded,
                    'excluded_count': len(excluded),
                })
            except Exception as e:
                self.logger.error(f"Media source listing error: {e}")
                return jsonify({'error': 'Could not list media sources'}), 500

        @self.app.route('/api/frame-sources', methods=['GET'])
        def get_frame_sources():
            """Get available frame source types with their metadata."""
            try:
                frame_sources = self._collect_frame_source_types()
                if not frame_sources:
                    raise ImportError("no frame source types reported by the library")

                # Enhance video_file source with upload capability
                for source in frame_sources:
                    if source.get('type') == 'video_file' and 'config_schema' in source:
                        schema = source['config_schema']
                        # Check if it has a fields array
                        if 'fields' in schema and isinstance(schema['fields'], list):
                            # Check if upload_file field doesn't already exist
                            has_upload_field = any(f.get('name') == 'upload_file' for f in schema['fields'])
                            if not has_upload_field:
                                # Insert upload_file field at the beginning
                                upload_field = {
                                    'name': 'upload_file',
                                    'type': 'file',
                                    'label': 'Upload Video File',
                                    'description': 'Upload a video file (MP4, AVI, MOV, etc.)',
                                    'accept': '.mp4,.avi,.mov,.mkv,.wmv,.flv,.webm,.m4v,.mpg,.mpeg',
                                    'upload_endpoint': '/api/media/upload-video',
                                    'required': False
                                }
                                schema['fields'].insert(0, upload_field)
                                
                                # Update the source field description to mention upload
                                for field in schema['fields']:
                                    if field.get('name') == 'source':
                                        field['description'] = 'Path to the video file (auto-populated after upload, or enter manually)'
                                        field['placeholder'] = 'Enter file path or upload a video above'
                
                return jsonify({
                    'status': 'success',
                    'frame_sources': frame_sources
                })
                
            except ImportError as e:
                self.logger.warning(f"FrameSource module not available: {str(e)}. Using fallback frame sources.")
                # Provide fallback frame sources when the module is not available
                fallback_sources = [
                    {
                        'type': 'webcam',
                        'name': 'Webcam',
                        'description': 'Local webcam or camera device',
                        'icon': 'fas fa-video',
                        'primary': True,
                        'available': True,
                        'config_schema': {
                            'fields': [
                                {
                                    'name': 'source',
                                    'type': 'number',
                                    'label': 'Camera Index',
                                    'description': 'Camera device index (0 for default camera)',
                                    'default': 0,
                                    'min': 0,
                                    'required': True
                                },
                                {
                                    'name': 'width',
                                    'type': 'number',
                                    'label': 'Width',
                                    'description': 'Frame width in pixels',
                                    'default': 640,
                                    'min': 320,
                                    'required': False
                                },
                                {
                                    'name': 'height',
                                    'type': 'number',
                                    'label': 'Height',
                                    'description': 'Frame height in pixels',
                                    'default': 480,
                                    'min': 240,
                                    'required': False
                                },
                                {
                                    'name': 'fps',
                                    'type': 'number',
                                    'label': 'FPS',
                                    'description': 'Frames per second',
                                    'default': 30,
                                    'min': 1,
                                    'max': 60,
                                    'required': False
                                }
                            ]
                        }
                    },
                    {
                        'type': 'video_file',
                        'name': 'Video File',
                        'description': 'Video file from local storage',
                        'icon': 'fas fa-file-video',
                        'primary': True,
                        'available': True,
                        'config_schema': {
                            'fields': [
                                {
                                    'name': 'upload_file',
                                    'type': 'file',
                                    'label': 'Upload Video File',
                                    'description': 'Upload a video file (MP4, AVI, MOV, etc.)',
                                    'accept': '.mp4,.avi,.mov,.mkv,.wmv,.flv,.webm,.m4v,.mpg,.mpeg',
                                    'upload_endpoint': '/api/media/upload-video',
                                    'required': False
                                },
                                {
                                    'name': 'source',
                                    'type': 'text',
                                    'label': 'Video File Path',
                                    'description': 'Path to the video file (auto-populated after upload, or enter manually)',
                                    'placeholder': 'Enter file path or upload a video above',
                                    'required': True,
                                    'readonly_when_uploaded': True
                                },
                                {
                                    'name': 'loop',
                                    'type': 'checkbox',
                                    'label': 'Loop Video',
                                    'description': 'Loop the video when it ends',
                                    'default': True,
                                    'required': False
                                }
                            ]
                        }
                    },
                    {
                        'type': 'ip_camera',
                        'name': 'IP Camera',
                        'description': 'Network camera via RTSP/HTTP',
                        'icon': 'fas fa-wifi',
                        'primary': False,
                        'available': True,
                        'config_schema': {
                            'fields': [
                                {
                                    'name': 'source',
                                    'type': 'url',
                                    'label': 'Camera URL',
                                    'description': 'RTSP or HTTP URL to the camera stream',
                                    'placeholder': 'rtsp://192.168.1.100:554/stream',
                                    'required': True
                                },
                                {
                                    'name': 'username',
                                    'type': 'text',
                                    'label': 'Username',
                                    'description': 'Camera authentication username (optional)',
                                    'placeholder': '',
                                    'required': False
                                },
                                {
                                    'name': 'password',
                                    'type': 'password',
                                    'label': 'Password',
                                    'description': 'Camera authentication password (optional)',
                                    'placeholder': '',
                                    'required': False
                                }
                            ]
                        }
                    }
                ]
                
                return jsonify({
                    'status': 'success',
                    'frame_sources': fallback_sources,
                    'fallback': True
                })
                
            except Exception as e:
                self.logger.error(f"Get frame sources error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/frame-sources/<source_type>/discover', methods=['GET'])
        def discover_frame_sources(source_type):
            """Discover available devices for a specific frame source type"""
            success = False
            try:
                devices = []
                
                # Try to import and use the frame source module
                try:
                    try:
                        from framesource import FrameSourceFactory
                    except ImportError:
                        from frame_source import FrameSourceFactory
                    try:
                        from InferenceNode.pipeline_manager import UI_TO_LIBRARY_CAPTURE_TYPE
                    except Exception:
                        from pipeline_manager import UI_TO_LIBRARY_CAPTURE_TYPE

                    # The caller passes the UI name ('ip_camera'); the factory only knows
                    # its own ('ipcam').
                    lib_type = UI_TO_LIBRARY_CAPTURE_TYPE.get(source_type, source_type)
                    try:
                        frame_source = FrameSourceFactory.create(capture_type=lib_type)
                        if hasattr(frame_source, 'discover'):
                            discovered = frame_source.discover()
                            # Ensure the returned data has the expected format
                            if isinstance(discovered, list):
                                devices = discovered
                            else:
                                devices = []
                        elif hasattr(frame_source.__class__, 'discover'):
                            discovered = frame_source.__class__.discover()
                            if isinstance(discovered, list):
                                devices = discovered
                            else:
                                devices = []
                        else:
                            devices = []
                        
                        success = True
                    except Exception as inner_e:
                        self.logger.debug(f"Could not create {source_type} frame source for discovery: {str(inner_e)}")
                        devices = []
                    
                    # self.logger.info(f"Discovered {len(devices)} devices for {source_type}")
                        
                except ImportError:
                    # Fallback discovery for basic types
                    # if source_type == 'webcam':
                    #     devices = self._discover_webcam_devices()
                    # elif source_type == 'audio_spectrogram':
                    #     devices = self._discover_audio_devices()
                    # else:
                    devices = []
                
                return jsonify({
                    'success': success,
                    'devices': devices or [],
                    'count': len(devices) if devices else 0,
                    'source_type': source_type
                })
                
            except Exception as e:
                self.logger.error(f"Frame source device discovery error for {source_type}: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': str(e),
                    'devices': []
                }), 500

        @self.app.route('/api/inference/engines', methods=['GET'])
        def get_inference_engines():
            """Get available inference engines with their metadata"""
            try:
                from InferenceEngine import InferenceEngineFactory
                engine_types = InferenceEngineFactory.get_available_engines_with_metadata()
                
                return jsonify({
                    'status': 'success',
                    'engine_types': engine_types
                })
                
            except Exception as e:
                self.logger.error(f"Get inference engines error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        # Pipeline API Routes
        @self.app.route('/api/pipeline/create', methods=['POST'])
        def create_pipeline():
            """Create a new inference pipeline"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                config = request.get_json()
                
                # Validate required fields
                required_fields = ['name', 'frame_source', 'model', 'destinations']
                for field in required_fields:
                    if field not in config:
                        return jsonify({'error': f'Missing required field: {field}'}), 400
                
                # Format device string for the specific inference engine
                if 'model' in config and 'device' in config['model'] and 'engine_type' in config['model']:
                    original_device = config['model']['device']
                    engine_type = config['model']['engine_type']
                    
                    # Use hardware detector to format device for the specific engine
                    formatted_device = self.hardware_detector.format_for(engine_type, original_device)
                    config['model']['device'] = formatted_device
                    
                    self.logger.info(f"Device '{original_device}' formatted to '{formatted_device}' for engine '{engine_type}'")
                
                # Build the definition (pure) and persist it ONCE, to PostgreSQL.
                # Creation is admin-only; create_pipeline() enforces that and records
                # the creator from the authenticated user - any owner field in the
                # request body is ignored.
                from flask_login import current_user
                from InferenceNode import pipeline_store as _ps

                pipeline_id, definition = self.pipeline_manager.build_pipeline_definition(config)
                try:
                    _ps.create_pipeline(current_user, pipeline_id=pipeline_id,
                                        name=config.get('name'),
                                        description=config.get('description'),
                                        config=definition, status='stopped')
                except _ps.AccessDenied:
                    return jsonify({'error': 'Pipeline not found or access denied'}), 404
                except ValueError as e:
                    return jsonify({'error': str(e)}), 409

                # Update node info with new pipeline information
                self._update_node_info_with_pipelines()
                
                self.logger.info(f"Pipeline created: {config['name']} ({pipeline_id})")
                
                return jsonify({
                    'pipeline_id': pipeline_id,
                    'status': 'created',
                    'message': f'Pipeline "{config["name"]}" created successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Create pipeline error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/duplicate', methods=['POST'])
        def duplicate_pipeline(pipeline_id):
            """Server-authoritative duplicate - the ONLY duplication path for both
            Pipeline Management and Pipeline Builder.

            Security boundary = view permission on the SOURCE (enforced by the
            pipeline_authz gate: 'duplicate' -> view) + create permission (admin,
            enforced by pipeline_store.create_pipeline). Knowing a UUID grants nothing.
            The clone is built from the UNREDACTED stored definition server-side, so
            secrets are preserved intact; the response is sanitize_config()-redacted
            like every other pipeline response - real credentials never leave the
            server. New pipeline uuid + new destination uuids; uuid collision retried.
            """
            try:
                from flask_login import current_user
                from InferenceNode import pipeline_store as _ps
                from InferenceNode.pipeline_store import sanitize_config

                try:
                    names = [r.get('name') for r in _ps.list_pipelines_for_user(current_user)]
                except Exception:
                    names = []

                new_id, definition, last_err = None, None, None
                for _attempt in range(3):   # bounded retry on pipeline_id collision only
                    built = self.pipeline_manager.build_duplicate_definition(pipeline_id, names)
                    if built is None:
                        return jsonify({'error': 'Pipeline not found or access denied'}), 404
                    cand_id, definition = built
                    try:
                        _ps.create_pipeline(current_user, pipeline_id=cand_id,
                                            name=definition['name'],
                                            description=definition['description'],
                                            config=definition, status='stopped')
                        new_id = cand_id
                        break
                    except _ps.AccessDenied:
                        return jsonify({'error': 'Pipeline not found or access denied'}), 404
                    except ValueError as e:      # "pipeline_id already exists"
                        last_err = e
                        continue
                if new_id is None:
                    return jsonify({'error': str(last_err or 'could not allocate pipeline id')}), 409

                self._update_node_info_with_pipelines()
                self.logger.info(f"Pipeline duplicated: {pipeline_id} -> {new_id}")
                return jsonify({
                    'pipeline_id': new_id,
                    'status': 'created',
                    'message': f'Pipeline "{definition["name"]}" created successfully',
                    'pipeline': sanitize_config(self.pipeline_manager.get_pipeline(new_id) or definition),
                })
            except Exception as e:
                self.logger.error(f"Duplicate pipeline error: {str(e)}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/pipelines/metrics', methods=['GET'])
        def get_pipeline_metrics():
            """Get only pipeline metrics (lighter endpoint for frequent polling)"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                
                # Scoped to the caller's visible pipelines (ids + metrics leaked node-wide before)
                from flask_login import current_user
                from InferenceNode.pipeline_store import list_pipelines_for_user
                try:
                    records = list_pipelines_for_user(current_user)
                except Exception:
                    records = []
                visible = {r['pipeline_id'] for r in records}
                stats = self.pipeline_manager.get_pipeline_stats(records=records)
                
                # Get metrics for running pipelines only
                running_metrics = {}
                for pipeline_id, pipeline_info in self.pipeline_manager.active_pipelines.items():
                    if pipeline_id not in visible:
                        continue
                    if 'pipeline_instance' in pipeline_info:
                        pipeline_instance = pipeline_info['pipeline_instance']
                        if hasattr(pipeline_instance, 'get_metrics'):
                            try:
                                metrics = pipeline_instance.get_metrics()
                                running_metrics[pipeline_id] = {
                                    'fps': round(metrics.get('fps', 0), 1),
                                    'frame_count': metrics.get('frame_count', 0),
                                    'elapsed_time': round(metrics.get('elapsed_time', 0), 1),
                                    'latency_ms': round(metrics.get('latency_ms', 0), 1),
                                    'uptime': metrics.get('uptime', '0s'),
                                    # Step 0 primitives - raw values for the benchmark harness,
                                    # which is where every percentile/rate is computed.
                                    'inference_count': metrics.get('inference_count', 0),
                                    'capture_timestamp': metrics.get('capture_timestamp', 0),
                                    'read_wait_ms': metrics.get('read_wait_ms', 0),
                                    'failed_read_count': metrics.get('failed_read_count', 0),
                                    'effective_device': metrics.get('effective_device'),
                                }
                            except Exception as e:
                                print(f"Error getting metrics for pipeline {pipeline_id}: {e}")
                
                import threading as _th
                return jsonify({
                    'stats': stats,
                    'running_pipelines': running_metrics,
                    # Process-level primitives sampled at response time.
                    'sampled_at': time.time(),
                    'thread_count': _th.active_count(),
                })
                
            except Exception as e:
                self.logger.error(f"Get pipeline metrics error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipelines', methods=['GET'])
        def list_pipelines():
            """List all pipelines"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                # Scoping happens in SQL: admins get every row, everyone else only the
                # pipelines they were granted can_view on. Nothing unauthorized is ever
                # loaded and then filtered afterwards.
                from flask_login import current_user
                from InferenceNode.pipeline_store import list_pipelines_for_user
                try:
                    records = list_pipelines_for_user(current_user)
                except Exception as e:
                    self.logger.error(f"Pipeline list scoping failed, denying: {e}")
                    records = []

                pipelines = self.pipeline_manager.list_pipelines(records=records)
                stats = self.pipeline_manager.get_pipeline_stats(records=records)

                # Pipeline config can carry RTSP/HTTP credentials and webhook tokens.
                # Redact before it ever leaves the process, for admins too.
                from InferenceNode.pipeline_store import sanitize_config
                items = [sanitize_config(p) for p in pipelines.values()]

                return jsonify({
                    'pipelines': items,
                    'stats': stats
                })
                
            except Exception as e:
                self.logger.error(f"List pipelines error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipelines/summary', methods=['GET'])
        def get_pipeline_summary():
            """Get pipeline summary for discovery service"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                # Scoped to what the caller may see and sanitized - this used to return
                # every pipeline's raw model/frame_source to any logged-in user.
                from flask_login import current_user
                from InferenceNode.pipeline_store import list_pipelines_for_user, sanitize_config
                try:
                    records = list_pipelines_for_user(current_user)
                except Exception:
                    records = []
                summary = self.pipeline_manager.get_pipeline_summary(records=records)
                return jsonify(sanitize_config(summary))
                
            except Exception as e:
                self.logger.error(f"Get pipeline summary error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>', methods=['GET'])
        def get_pipeline(pipeline_id):
            """Get pipeline configuration"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                pipeline = self.pipeline_manager.get_pipeline(pipeline_id)
                if not pipeline:
                    return jsonify({'error': 'Pipeline not found'}), 404

                # Secrets in the stored config are redacted before responding.
                from InferenceNode.pipeline_store import sanitize_config
                return jsonify(sanitize_config(pipeline))

            except Exception as e:
                self.logger.error(f"Get pipeline error: {str(e)}")
                return jsonify({'error': str(e)}), 500
            
        @self.app.route('/api/pipeline/<pipeline_id>/fullstatus', methods=['GET'])
        def get_pipeline_full_status(pipeline_id):
            """Get the full status of the pipeline
                - The state of the pipeline, running/stopped
                - The state of the inference enabled/disabled
                - Preview enabled/disabled
                - The metrics of the pipeline
                - The publishers and their states enabled/disabled
            """
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503

                status = self.pipeline_manager.get_pipeline_status(pipeline_id)
                if not status:
                    return jsonify({'error': 'Pipeline not found'}), 404

                from InferenceNode.pipeline_store import sanitize_config
                return jsonify(sanitize_config(status))  # never raw model/frame_source/destinations

            except Exception as e:
                self.logger.error(f"Get pipeline status error: {str(e)}")
                return jsonify({'error': str(e)}), 500
            
        @self.app.route('/api/pipeline/<pipeline_id>', methods=['DELETE'])
        def delete_pipeline(pipeline_id):
            """Delete a pipeline"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                # Deletion is ADMIN ONLY (already enforced by the authz gate; enforced
                # again inside the ONE coordinated delete path). Failure-safe order:
                # authorize -> stop -> thumbnail DELETING + trash -> row delete (cascade)
                # -> commit -> purge trash; a DB failure restores the JPEG.
                from flask_login import current_user
                result = self.pipeline_manager.delete_pipeline_coordinated(current_user, pipeline_id)
                outcome = result.get('outcome')
                if outcome == 'denied':
                    return jsonify({'error': 'Pipeline not found or access denied'}), 404
                if outcome == 'thumbnail_stage_failed':
                    return jsonify({'error': 'Could not stage thumbnail for deletion; nothing removed'}), 500
                if outcome == 'db_failed':
                    return jsonify({'error': 'Delete failed; nothing removed'}), 500

                # Update node info after pipeline deletion
                self._update_node_info_with_pipelines()
                
                return jsonify({
                    'status': 'deleted',
                    'pipeline_id': pipeline_id,
                    'message': 'Pipeline deleted successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Delete pipeline error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>', methods=['PUT'])
        def update_pipeline(pipeline_id):
            """Update an existing pipeline"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                data = request.get_json()
                if not data:
                    return jsonify({'error': 'No data provided'}), 400
                
                # Check if pipeline exists
                pipeline = self.pipeline_manager.get_pipeline(pipeline_id)
                if not pipeline:
                    return jsonify({'error': 'Pipeline not found'}), 404
                
                # Runtime-affecting edits are refused while the pipeline runs, so the
                # stored definition and the live runtime can never disagree. Runtime
                # truth comes from the manager, not from the stored status value.
                if (self.pipeline_manager.is_running(pipeline_id)
                        and self.pipeline_manager.requires_stop_for_update(data)):
                    return jsonify({
                        'error': 'PIPELINE_RUNNING_REQUIRES_STOP',
                        'message': 'Stop the pipeline before changing its source, model or destinations.'
                    }), 409

                # Format device string for the specific inference engine if present
                if 'model' in data and 'device' in data['model'] and 'engine_type' in data['model']:
                    original_device = data['model']['device']
                    engine_type = data['model']['engine_type']
                    
                    # Use hardware detector to format device for the specific engine
                    formatted_device = self.hardware_detector.format_for(engine_type, original_device)
                    data['model']['device'] = formatted_device
                    
                    self.logger.info(f"Device '{original_device}' formatted to '{formatted_device}' for engine '{engine_type}' (update)")
                
                # Update the pipeline
                success = self.pipeline_manager.update_pipeline(pipeline_id, data)
                if not success:
                    return jsonify({'error': 'Failed to update pipeline'}), 500
                
                self.logger.info(f"Pipeline updated: {data.get('name', 'Unknown')} ({pipeline_id})")
                
                return jsonify({
                    'status': 'updated',
                    'pipeline_id': pipeline_id,
                    'message': 'Pipeline updated successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Update pipeline error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/start', methods=['POST'])
        def start_pipeline(pipeline_id):
            """Start a pipeline, reconstructing its runtime from PostgreSQL.

            Authorization already succeeded by the time we get here, so failures are
            reported as concrete runtime problems (missing media, missing model,
            unavailable engine/device) rather than being collapsed into the opaque
            authorization message.
            """
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503

                self.logger.info(f"Attempting to start pipeline: {pipeline_id}")

                definition = self.pipeline_manager.get_pipeline(pipeline_id)
                if not definition:
                    return jsonify({'error': 'PIPELINE_CONFIG_INVALID',
                                    'message': 'The pipeline has no stored configuration.'}), 422

                # Classify what the runtime will need BEFORE launching a thread, so the
                # caller gets a specific reason instead of a generic failure.
                problem = self._classify_pipeline_prerequisites(pipeline_id, definition)
                if problem:
                    self.logger.warning(f"Pipeline {pipeline_id} not startable: {problem['error']}")
                    return jsonify(problem), 422

                success = self.pipeline_manager.start_pipeline(
                    pipeline_id,
                    self.model_repo,
                    self.result_publisher
                )

                if not success:
                    detail = self.pipeline_manager.get_start_error(pipeline_id)
                    self.logger.error(f"Pipeline {pipeline_id} failed to start: {detail}")
                    return jsonify({
                        'error': 'PIPELINE_START_FAILED',
                        'message': detail or 'The pipeline could not be started. Check the node logs.'
                    }), 400

                # Update node info with pipeline status change
                self._update_node_info_with_pipelines()
                
                self.logger.info(f"Pipeline started successfully: {pipeline_id}")
                
                return jsonify({
                    'status': 'started',
                    'pipeline_id': pipeline_id,
                    'message': 'Pipeline started successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Start pipeline error for {pipeline_id}: {str(e)}", exc_info=True)
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/stop', methods=['POST'])
        def stop_pipeline(pipeline_id):
            """Stop a pipeline"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                success = self.pipeline_manager.stop_pipeline(pipeline_id)
                if not success:
                    return jsonify({'error': 'Pipeline not found or not running'}), 400
                
                # Update node info with pipeline status change
                self._update_node_info_with_pipelines()
                
                self.logger.info(f"Pipeline stopped: {pipeline_id}")
                
                return jsonify({
                    'status': 'stopped',
                    'pipeline_id': pipeline_id,
                    'message': 'Pipeline stopped successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Stop pipeline error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/inference/enable', methods=['POST'])
        def enable_pipeline_inference(pipeline_id):
            """Enable inference for a pipeline"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                success = self.pipeline_manager.enable_pipeline_inference(pipeline_id)
                if not success:
                    return jsonify({'error': 'Pipeline not found'}), 404
                
                self.logger.info(f"Pipeline inference enabled: {pipeline_id}")
                
                return jsonify({
                    'status': 'inference_enabled',
                    'pipeline_id': pipeline_id,
                    'message': 'Inference enabled successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Enable pipeline inference error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/inference/disable', methods=['POST'])
        def disable_pipeline_inference(pipeline_id):
            """Disable inference for a pipeline"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                success = self.pipeline_manager.disable_pipeline_inference(pipeline_id)
                if not success:
                    return jsonify({'error': 'Pipeline not found'}), 404
                
                self.logger.info(f"Pipeline inference disabled: {pipeline_id}")
                
                return jsonify({
                    'status': 'inference_disabled',
                    'pipeline_id': pipeline_id,
                    'message': 'Inference disabled successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Disable pipeline inference error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/publisher/<publisher_id>/enable', methods=['POST'])
        def enable_pipeline_publisher(pipeline_id, publisher_id):
            """Enable a specific publisher for a pipeline"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                success = self.pipeline_manager.enable_pipeline_publisher(pipeline_id, publisher_id)
                if not success:
                    return jsonify({'error': 'Pipeline or publisher not found'}), 404
                
                self.logger.info(f"Pipeline publisher enabled: {pipeline_id}/{publisher_id}")
                
                return jsonify({
                    'status': 'publisher_enabled',
                    'pipeline_id': pipeline_id,
                    'publisher_id': publisher_id,
                    'message': 'Publisher enabled successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Enable pipeline publisher error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/publisher/<publisher_id>/disable', methods=['POST'])
        def disable_pipeline_publisher(pipeline_id, publisher_id):
            """Disable a specific publisher for a pipeline"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                success = self.pipeline_manager.disable_pipeline_publisher(pipeline_id, publisher_id)
                if not success:
                    return jsonify({'error': 'Pipeline or publisher not found'}), 404
                
                self.logger.info(f"Pipeline publisher disabled: {pipeline_id}/{publisher_id}")
                
                return jsonify({
                    'status': 'publisher_disabled',
                    'pipeline_id': pipeline_id,
                    'publisher_id': publisher_id,
                    'message': 'Publisher disabled successfully'
                })
                
            except Exception as e:
                self.logger.error(f"Disable pipeline publisher error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/publishers/status', methods=['GET'])
        def get_pipeline_publishers_status(pipeline_id):
            """Get the status of all publishers for a pipeline"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                publisher_states = self.pipeline_manager.get_pipeline_publisher_states(pipeline_id)
                if publisher_states is None:
                    return jsonify({'error': 'Pipeline not found'}), 404

                from InferenceNode.pipeline_store import sanitize_config
                return jsonify({
                    'pipeline_id': pipeline_id,
                    'publishers': sanitize_config(publisher_states)
                })
                
            except Exception as e:
                self.logger.error(f"Get pipeline publishers status error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/status', methods=['GET'])
        def get_pipeline_status(pipeline_id):
            """Get pipeline status"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                    
                pipeline = self.pipeline_manager.get_pipeline(pipeline_id)
                if not pipeline:
                    return jsonify({'error': 'Pipeline not found'}), 404

                # Persistent definition + LIVE runtime status. `status` is runtime truth;
                # the stored value is exposed separately as stored_status and is never
                # treated as proof that a process exists.
                #
                # NOTE: active_pipelines holds the live InferencePipeline object, which is
                # not JSON-serialisable - this route used to return it verbatim and so
                # failed with a 500 exactly when a pipeline WAS running.
                from InferenceNode.pipeline_store import sanitize_config
                merged = self.pipeline_manager.get_pipeline_status(pipeline_id, record=pipeline)

                return jsonify({
                    'pipeline_id': pipeline_id,
                    'status': merged.get('status'),
                    'stored_status': merged.get('stored_status'),
                    'last_error': merged.get('last_error'),
                    'stats': merged.get('stats', {}),
                    'inference_enabled': merged.get('inference_enabled', True),
                    'config': sanitize_config(pipeline),
                })

            except Exception as e:
                self.logger.error(f"Get pipeline status error: {str(e)}")
                return jsonify({'error': str(e)}), 500

        @self.app.route('/api/pipeline/<pipeline_id>/stream')
        def stream_pipeline(pipeline_id):
            """Stream processed frames from a running pipeline"""
            def generate_frames():
                if not self.pipeline_manager:
                    return
                    
                frame_count = 0
                max_retries = 50  # Try for 5 seconds (50 * 0.1s)
                retry_count = 0
                last_frame_time = 0
                frame_skip_threshold = 1.0 / 30  # Target 30 FPS max
                
                # Get pipeline instance reference for cleanup
                pipeline_info = self.pipeline_manager.active_pipelines.get(pipeline_id)
                pipeline_instance = pipeline_info.get('pipeline_instance') if pipeline_info else None
                
                try:
                    while retry_count < max_retries:
                        try:
                            # Check if pipeline is running
                            if pipeline_id not in self.pipeline_manager.active_pipelines:
                                self.logger.warning(f"Pipeline {pipeline_id} not in active pipelines")
                                break
                            
                            # Get the pipeline instance
                            pipeline_info = self.pipeline_manager.active_pipelines[pipeline_id]
                            pipeline_instance = pipeline_info.get('pipeline_instance')
                            
                            if not pipeline_instance:
                                self.logger.warning(f"No pipeline instance found for {pipeline_id}")
                                retry_count += 1
                                time.sleep(0.1)
                                continue
                            
                            # Check if pipeline is actually running
                            if hasattr(pipeline_instance, 'is_running') and not pipeline_instance.is_running():
                                self.logger.warning(f"Pipeline {pipeline_id} is not running")
                                break
                            
                            # Throttle frame rate to prevent overwhelming the browser
                            current_time = time.time()
                            if current_time - last_frame_time < frame_skip_threshold:
                                time.sleep(0.01)  # Small sleep to prevent busy waiting
                                continue
                            
                            # Get the latest processed frame
                            frame = pipeline_instance.get_latest_frame()
                            
                            if frame is not None:
                                # Resize frame for preview (smaller = faster transmission)
                                height, width = frame.shape[:2]
                                if width > 640:  # Resize to max 640px width for preview
                                    scale = 640 / width
                                    new_width = 640
                                    new_height = int(height * scale)
                                    frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
                                
                                # Encode frame as JPEG with lower quality for faster streaming
                                ret, buffer = cv2.imencode('.jpg', frame, [
                                    cv2.IMWRITE_JPEG_QUALITY, 70,  # Lower quality for speed
                                    cv2.IMWRITE_JPEG_OPTIMIZE, 1   # Optimize compression
                                ])
                                if ret:
                                    frame_bytes = buffer.tobytes()
                                    yield (b'--frame\r\n'
                                           b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                                    frame_count += 1
                                    retry_count = 0  # Reset retry count on successful frame
                                    last_frame_time = current_time
                                else:
                                    self.logger.warning(f"Failed to encode frame for pipeline {pipeline_id}")
                            else:
                                # No frame available yet, increment retry count
                                retry_count += 1
                                time.sleep(0.01)  # Much smaller sleep when no frame
                            
                        except Exception as e:
                            self.logger.error(f"Stream error for pipeline {pipeline_id}: {e}")
                            retry_count += 1
                            time.sleep(0.1)
                finally:
                    # Disable streaming flag when generator ends
                    if pipeline_instance and hasattr(pipeline_instance, 'stop_streaming'):
                        pipeline_instance.stop_streaming()
                    self.logger.info(f"Stream ended for pipeline {pipeline_id}, streamed {frame_count} frames")
            
            try:
                # Check if pipeline exists and is in active pipelines
                if not self.pipeline_manager or pipeline_id not in self.pipeline_manager.active_pipelines:
                    return jsonify({'error': 'Pipeline not found or not running'}), 404
                
                # Get the pipeline instance
                pipeline_info = self.pipeline_manager.active_pipelines.get(pipeline_id)
                if not pipeline_info or 'pipeline_instance' not in pipeline_info:
                    return jsonify({'error': 'Pipeline instance not available'}), 404
                
                pipeline_instance = pipeline_info['pipeline_instance']
                
                # Check if pipeline is actually running
                if hasattr(pipeline_instance, 'is_running') and not pipeline_instance.is_running():
                    return jsonify({'error': 'Pipeline is not running'}), 400
                
                # Check if pipeline is initialized
                if hasattr(pipeline_instance, 'is_initialized') and not pipeline_instance.is_initialized():
                    return jsonify({'error': 'Pipeline is not initialized'}), 400
                
                # Enable streaming BEFORE checking for frames, so frames will be processed with results
                if hasattr(pipeline_instance, 'start_streaming'):
                    pipeline_instance.start_streaming()
                
                # Wait for first frame to be available (max 5 seconds)
                max_wait_time = 5.0  # seconds
                wait_start = time.time()
                frame_available = False
                
                while time.time() - wait_start < max_wait_time:
                    if pipeline_instance.get_latest_frame() is not None:
                        frame_available = True
                        break
                    time.sleep(0.1)  # Check every 100ms
                
                if not frame_available:
                    # Disable streaming since we're not going to stream
                    if hasattr(pipeline_instance, 'stop_streaming'):
                        pipeline_instance.stop_streaming()
                    return jsonify({'error': 'Pipeline is starting - no frames available yet. Please try again in a moment.'}), 503
                
                return Response(generate_frames(),
                              mimetype='multipart/x-mixed-replace; boundary=frame',
                              headers={'Cache-Control': 'no-cache, no-store, must-revalidate',
                                     'Pragma': 'no-cache',
                                     'Expires': '0'})
            except Exception as e:
                self.logger.error(f"Failed to start stream for pipeline {pipeline_id}: {e}")
                return jsonify({'error': 'Failed to start video stream'}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/stream/hq')
        def stream_pipeline_hq(pipeline_id):
            """High-quality stream for full preview modal"""
            def generate_frames():
                if not self.pipeline_manager:
                    return
                    
                frame_count = 0
                max_retries = 50
                retry_count = 0
                last_frame_time = 0
                frame_skip_threshold = 1.0 / 60  # Target 60 FPS max for HQ
                
                # Get pipeline instance reference for cleanup
                pipeline_info = self.pipeline_manager.active_pipelines.get(pipeline_id)
                pipeline_instance = pipeline_info.get('pipeline_instance') if pipeline_info else None
                
                try:
                    while retry_count < max_retries:
                        try:
                            # Check if pipeline is running
                            if pipeline_id not in self.pipeline_manager.active_pipelines:
                                break
                            
                            # Get the pipeline instance
                            pipeline_info = self.pipeline_manager.active_pipelines[pipeline_id]
                            pipeline_instance = pipeline_info.get('pipeline_instance')
                            
                            if not pipeline_instance:
                                retry_count += 1
                                time.sleep(0.05)
                                continue
                            
                            # Check if pipeline is actually running
                            if hasattr(pipeline_instance, 'is_running') and not pipeline_instance.is_running():
                                self.logger.warning(f"HQ Stream: Pipeline {pipeline_id} is not running")
                                break
                            
                            # Throttle frame rate
                            current_time = time.time()
                            if current_time - last_frame_time < frame_skip_threshold:
                                time.sleep(0.005)
                                continue
                            
                            # Get the latest processed frame
                            frame = pipeline_instance.get_latest_frame()
                            
                            if frame is not None:
                                # Keep original resolution for HQ stream, but limit to reasonable size
                                height, width = frame.shape[:2]
                                if width > 1280:  # Limit to 1280px width for HQ
                                    scale = 1280 / width
                                    new_width = 1280
                                    new_height = int(height * scale)
                                    frame = cv2.resize(frame, (new_width, new_height), interpolation=cv2.INTER_AREA)
                                
                                # Higher quality encoding for HQ stream
                                ret, buffer = cv2.imencode('.jpg', frame, [
                                    cv2.IMWRITE_JPEG_QUALITY, 85,  # Higher quality
                                    cv2.IMWRITE_JPEG_OPTIMIZE, 1
                                ])
                                if ret:
                                    frame_bytes = buffer.tobytes()
                                    yield (b'--frame\r\n'
                                           b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                                    frame_count += 1
                                    retry_count = 0
                                    last_frame_time = current_time
                            else:
                                retry_count += 1
                                time.sleep(0.005)
                            
                        except Exception as e:
                            self.logger.error(f"HQ Stream error for pipeline {pipeline_id}: {e}")
                            retry_count += 1
                            time.sleep(0.05)
                finally:
                    # Disable streaming flag when generator ends
                    if pipeline_instance and hasattr(pipeline_instance, 'stop_streaming'):
                        pipeline_instance.stop_streaming()
                    self.logger.info(f"HQ Stream ended for pipeline {pipeline_id}, streamed {frame_count} frames")
            
            try:
                # Check if pipeline exists and is in active pipelines
                if not self.pipeline_manager or pipeline_id not in self.pipeline_manager.active_pipelines:
                    return jsonify({'error': 'Pipeline not found or not running'}), 404
                
                # Get the pipeline instance
                pipeline_info = self.pipeline_manager.active_pipelines.get(pipeline_id)
                if not pipeline_info or 'pipeline_instance' not in pipeline_info:
                    return jsonify({'error': 'Pipeline instance not available'}), 404
                
                pipeline_instance = pipeline_info['pipeline_instance']
                
                # Check if pipeline is actually running
                if hasattr(pipeline_instance, 'is_running') and not pipeline_instance.is_running():
                    return jsonify({'error': 'Pipeline is not running'}), 400
                
                # Check if pipeline is initialized
                if hasattr(pipeline_instance, 'is_initialized') and not pipeline_instance.is_initialized():
                    return jsonify({'error': 'Pipeline is not initialized'}), 400
                
                # Enable streaming BEFORE checking for frames, so frames will be processed with results
                if hasattr(pipeline_instance, 'start_streaming'):
                    pipeline_instance.start_streaming()
                
                # Wait for first frame to be available (max 5 seconds)
                max_wait_time = 5.0  # seconds
                wait_start = time.time()
                frame_available = False
                
                while time.time() - wait_start < max_wait_time:
                    if pipeline_instance.get_latest_frame() is not None:
                        frame_available = True
                        break
                    time.sleep(0.1)  # Check every 100ms
                
                if not frame_available:
                    # Disable streaming since we're not going to stream
                    if hasattr(pipeline_instance, 'stop_streaming'):
                        pipeline_instance.stop_streaming()
                    return jsonify({'error': 'Pipeline is starting - no frames available yet. Please try again in a moment.'}), 503
                
                return Response(generate_frames(),
                              mimetype='multipart/x-mixed-replace; boundary=frame',
                              headers={'Cache-Control': 'no-cache, no-store, must-revalidate',
                                     'Pragma': 'no-cache',
                                     'Expires': '0'})
            except Exception as e:
                self.logger.error(f"Failed to start HQ stream for pipeline {pipeline_id}: {e}")
                return jsonify({'error': 'Failed to start HQ video stream'}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/thumbnail')
        def get_pipeline_thumbnail(pipeline_id):
            """Serve pipeline thumbnail image"""
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                
                # Get thumbnail path from pipeline manager
                thumbnail_path = self.pipeline_manager.get_pipeline_thumbnail_path(pipeline_id)
                
                if not thumbnail_path:
                    # Return a default "no thumbnail" image or 404
                    return jsonify({'error': 'Thumbnail not found'}), 404
                
                # Serve the thumbnail image file
                from flask import send_file
                return send_file(thumbnail_path, mimetype='image/jpeg')
                
            except Exception as e:
                self.logger.error(f"Error serving thumbnail for pipeline {pipeline_id}: {e}")
                return jsonify({'error': 'Failed to serve thumbnail'}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/thumbnail/exists')
        def check_pipeline_thumbnail(pipeline_id):
            """Check if pipeline has a thumbnail"""
            try:
                if not self.pipeline_manager:
                    self.logger.error(f"Pipeline manager not available for thumbnail check")
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                
                self.logger.info(f"Checking thumbnail existence for pipeline {pipeline_id}")
                has_thumbnail = self.pipeline_manager.has_pipeline_thumbnail(pipeline_id)
                
                # Additional debugging
                thumbnail_path = self.pipeline_manager.get_pipeline_thumbnail_path(pipeline_id)
                if thumbnail_path:
                    file_exists = os.path.exists(thumbnail_path)
                    self.logger.info(f"Pipeline {pipeline_id}: thumbnail_path={thumbnail_path}, file_exists={file_exists}")
                else:
                    self.logger.info(f"Pipeline {pipeline_id}: No thumbnail path found")
                
                self.logger.info(f"Pipeline {pipeline_id} has_thumbnail: {has_thumbnail}")
                return jsonify({'has_thumbnail': has_thumbnail})
                
            except Exception as e:
                self.logger.error(f"Error checking thumbnail for pipeline {pipeline_id}: {e}")
                return jsonify({'error': 'Failed to check thumbnail'}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/thumbnail/generate', methods=['POST'])
        def generate_pipeline_thumbnail(pipeline_id):
            """Generate a fresh thumbnail for a pipeline from current frame"""
            try:
                if not self.pipeline_manager:
                    self.logger.error(f"Pipeline manager not available for thumbnail generation")
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                
                # Check if pipeline exists
                pipeline = self.pipeline_manager.get_pipeline(pipeline_id)
                if not pipeline:
                    self.logger.error(f"Pipeline {pipeline_id} not found for thumbnail generation")
                    return jsonify({'error': 'Pipeline not found'}), 404
                
                self.logger.info(f"Generating fresh thumbnail for pipeline {pipeline_id}")
                
                # Generate thumbnail - this should capture the current frame
                success = self.pipeline_manager.generate_pipeline_thumbnail(pipeline_id)
                
                if success:
                    self.logger.info(f"Successfully generated thumbnail for pipeline {pipeline_id}")
                    
                    # Verify the thumbnail was created
                    has_thumbnail = self.pipeline_manager.has_pipeline_thumbnail(pipeline_id)
                    thumbnail_path = self.pipeline_manager.get_pipeline_thumbnail_path(pipeline_id)
                    
                    return jsonify({
                        'success': True,
                        'message': 'Thumbnail generated successfully',
                        'pipeline_id': pipeline_id,
                        'has_thumbnail': has_thumbnail,
                        'thumbnail_path': thumbnail_path if has_thumbnail else None
                    })
                else:
                    self.logger.error(f"Failed to generate thumbnail for pipeline {pipeline_id}")
                    return jsonify({
                        'success': False,
                        'error': 'Failed to generate thumbnail - pipeline may not be running or accessible',
                        'pipeline_id': pipeline_id
                    }), 500
                
            except Exception as e:
                self.logger.error(f"Generate thumbnail error for pipeline {pipeline_id}: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/<pipeline_id>/export', methods=['GET'])
        def export_pipeline(pipeline_id):
            """Export a pipeline as a ZIP file containing configuration and model files"""
            from InferenceNode.pipeline_store import sanitize_config as _sanitize
            try:
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                
                # Get pipeline configuration
                pipeline = self.pipeline_manager.get_pipeline(pipeline_id)
                if not pipeline:
                    return jsonify({'error': 'Pipeline not found'}), 404
                
                # Create temporary file for the ZIP outside of directory context
                zip_fd, zip_path = tempfile.mkstemp(suffix='.zip')
                
                try:
                    # Create temporary directory for export preparation
                    with tempfile.TemporaryDirectory() as temp_dir:
                        # Create pipeline config file
                        config_data = {
                            'name': pipeline['name'],
                            'description': pipeline.get('description', ''),
                            # Exports leave the server: never carry stored credentials
                            'frame_source': _sanitize(pipeline['frame_source']),
                            'model': _sanitize(pipeline['model']),
                            'destinations': _sanitize(pipeline.get('destinations', [])),
                            'export_metadata': {
                                'exported_by': self.node_name,
                                'export_date': datetime.now().isoformat(),
                                'pipeline_id': pipeline_id,
                                'version': '1.0'
                            }
                        }
                        
                        # Write pipeline configuration
                        config_file = os.path.join(temp_dir, 'pipeline_config.json')
                        with open(config_file, 'w') as f:
                            json.dump(config_data, f, indent=2)
                        
                        # Create models directory in temp folder
                        models_dir = os.path.join(temp_dir, 'models')
                        os.makedirs(models_dir, exist_ok=True)
                        
                        # Copy model files if they exist
                        model_files_included = []
                        if 'model' in pipeline and 'id' in pipeline['model']:
                            model_id = pipeline['model']['id']
                            model_metadata = self.model_repo.get_model_metadata(model_id)
                            
                            if model_metadata:
                                model_path = self.model_repo.get_model_path(model_id)
                                if model_path and os.path.exists(model_path):
                                    # Copy main model file
                                    model_filename = model_metadata['stored_filename']
                                    dest_path = os.path.join(models_dir, model_filename)
                                    shutil.copy2(model_path, dest_path)
                                    model_files_included.append(model_filename)
                                    
                                    # For some models, there might be additional files (e.g., OpenVINO models)
                                    model_dir = os.path.dirname(model_path)
                                    model_base_name = os.path.splitext(model_metadata['stored_filename'])[0]
                                    
                                    # Look for related files (same base name, different extensions)
                                    for file in os.listdir(model_dir):
                                        if file.startswith(model_base_name) and file != model_metadata['stored_filename']:
                                            src_file = os.path.join(model_dir, file)
                                            dest_file = os.path.join(models_dir, file)
                                            if os.path.isfile(src_file):
                                                shutil.copy2(src_file, dest_file)
                                                model_files_included.append(file)
                                    
                                    # Include model metadata
                                    model_metadata_file = os.path.join(models_dir, 'model_metadata.json')
                                    with open(model_metadata_file, 'w') as f:
                                        json.dump(model_metadata, f, indent=2)
                                    model_files_included.append('model_metadata.json')
                        
                        # Add model files list to config
                        config_data['export_metadata']['model_files'] = model_files_included
                        
                        # Re-write config with updated metadata
                        with open(config_file, 'w') as f:
                            json.dump(config_data, f, indent=2)
                        
                        # Create ZIP file outside of temp directory
                        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                            # Add config file
                            zipf.write(config_file, 'pipeline_config.json')
                            
                            # Add model files
                            for root, dirs, files in os.walk(models_dir):
                                for file in files:
                                    file_path = os.path.join(root, file)
                                    arc_name = os.path.join('models', os.path.relpath(file_path, models_dir))
                                    zipf.write(file_path, arc_name)
                    
                    # Close the file descriptor and read the contents
                    os.close(zip_fd)
                    
                    # Generate clean filename
                    zip_filename = f"{pipeline['name'].replace(' ', '_').replace('/', '_')}_export.zip"
                    # Remove invalid characters for filename
                    import re
                    zip_filename = re.sub(r'[<>:"/\\|?*]', '_', zip_filename)
                    
                    self.logger.info(f"Pipeline exported: {pipeline['name']} ({pipeline_id})")
                    
                    # Read the ZIP file contents
                    with open(zip_path, 'rb') as f:
                        zip_contents = f.read()
                    
                    # Clean up the temporary file
                    try:
                        os.unlink(zip_path)
                    except:
                        pass
                    
                    # Return the file contents as a response
                    response = Response(
                        zip_contents,
                        mimetype='application/zip',
                        headers={
                            'Content-Disposition': f'attachment; filename="{zip_filename}"',
                            'Content-Length': str(len(zip_contents))
                        }
                    )
                    return response
                    
                except Exception as e:
                    # Clean up in case of error
                    try:
                        os.close(zip_fd)
                        os.unlink(zip_path)
                    except:
                        pass
                    raise e
                    
            except Exception as e:
                self.logger.error(f"Export pipeline error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/pipeline/import', methods=['POST'])
        def import_pipeline():
            """Import a pipeline from an uploaded ZIP file"""
            try:
                # Admin is checked FIRST: previously the zip was extracted and its model
                # stored into the repository BEFORE the create-time admin check, so a
                # non-admin upload still landed model files on disk.
                from flask_login import current_user
                from InferenceNode import pipeline_store as _ps_guard
                try:
                    _ps_guard.require_admin(current_user, "pipeline_import")
                except _ps_guard.AccessDenied:
                    return jsonify({'error': 'Pipeline not found or access denied'}), 404
                if not self.pipeline_manager:
                    return jsonify({'error': 'Pipeline manager not available'}), 503
                
                # Check if file was uploaded
                if 'file' not in request.files:
                    return jsonify({'error': 'No file uploaded'}), 400
                
                file = request.files['file']
                if file.filename == '' or file.filename is None:
                    return jsonify({'error': 'No file selected'}), 400
                
                if not file.filename.endswith('.zip'):
                    return jsonify({'error': 'File must be a ZIP archive'}), 400
                
                # Create temporary directory for import
                with tempfile.TemporaryDirectory() as temp_dir:
                    # Save uploaded file
                    zip_path = os.path.join(temp_dir, file.filename)
                    file.save(zip_path)
                    
                    # Extract ZIP file
                    extract_dir = os.path.join(temp_dir, 'extracted')
                    with zipfile.ZipFile(zip_path, 'r') as zipf:
                        zipf.extractall(extract_dir)
                    
                    # Read pipeline configuration
                    config_file = os.path.join(extract_dir, 'pipeline_config.json')
                    if not os.path.exists(config_file):
                        return jsonify({'error': 'Invalid pipeline export: missing pipeline_config.json'}), 400
                    
                    with open(config_file, 'r') as f:
                        config_data = json.load(f)
                    
                    # Validate configuration structure
                    required_fields = ['name', 'frame_source', 'model']
                    for field in required_fields:
                        if field not in config_data:
                            return jsonify({'error': f'Invalid pipeline configuration: missing {field}'}), 400
                    
                    # Handle model import
                    models_dir = os.path.join(extract_dir, 'models')
                    new_model_id = None
                    
                    if os.path.exists(models_dir):
                        # Read model metadata if it exists
                        model_metadata_file = os.path.join(models_dir, 'model_metadata.json')
                        model_metadata = None
                        if os.path.exists(model_metadata_file):
                            with open(model_metadata_file, 'r') as f:
                                model_metadata = json.load(f)
                        
                        # Find the main model file
                        model_files = [f for f in os.listdir(models_dir) if f != 'model_metadata.json']
                        if model_files:
                            # Use the first model file (or the one specified in metadata)
                            main_model_file = model_files[0]
                            if model_metadata and 'stored_filename' in model_metadata:
                                main_model_file = model_metadata['stored_filename']
                                if main_model_file not in model_files:
                                    main_model_file = model_files[0]
                            
                            # Import the model
                            model_file_path = os.path.join(models_dir, main_model_file)
                            original_filename = model_metadata.get('original_filename', main_model_file) if model_metadata else main_model_file
                            engine_type = config_data['model'].get('engine_type', 'unknown')
                            description = f"Imported with pipeline: {config_data['name']}"
                            # Use name from metadata if available, otherwise use filename without extension
                            imported_name = model_metadata.get('name', os.path.splitext(original_filename)[0]) if model_metadata else os.path.splitext(original_filename)[0]
                            
                            # Store the model in the repository
                            new_model_id = self.model_repo.store_model(
                                model_file_path, 
                                original_filename, 
                                engine_type, 
                                description,
                                imported_name
                            )
                            
                            # Copy any additional model files
                            new_model_metadata = self.model_repo.get_model_metadata(new_model_id)
                            if new_model_metadata:
                                new_model_dir = os.path.dirname(new_model_metadata['stored_path'])
                                new_model_base = os.path.splitext(new_model_metadata['stored_filename'])[0]
                                
                                for model_file in model_files:
                                    if model_file != main_model_file and model_file != 'model_metadata.json':
                                        src_path = os.path.join(models_dir, model_file)
                                        # Rename additional files to match new model ID
                                        file_ext = os.path.splitext(model_file)[1]
                                        dest_filename = f"{new_model_base}{file_ext}"
                                        dest_path = os.path.join(new_model_dir, dest_filename)
                                        shutil.copy2(src_path, dest_path)
                    
                    # Update model ID in configuration
                    if new_model_id:
                        config_data['model']['id'] = new_model_id
                    
                    # Generate unique pipeline name if needed
                    original_name = config_data['name']
                    pipeline_name = original_name
                    existing_pipelines = self.pipeline_manager.list_pipelines()
                    existing_names = [p.get('name') for p in existing_pipelines.values()]
                    
                    counter = 1
                    while pipeline_name in existing_names:
                        pipeline_name = f"{original_name} (imported {counter})"
                        counter += 1
                    
                    config_data['name'] = pipeline_name
                    
                    # Remove export metadata before creating pipeline
                    if 'export_metadata' in config_data:
                        del config_data['export_metadata']

                    # Import creates a pipeline, so it is ADMIN ONLY and persists to
                    # PostgreSQL through the same service path as /api/pipeline/create.
                    from flask_login import current_user
                    from InferenceNode import pipeline_store as _ps
                    pipeline_id, definition = self.pipeline_manager.build_pipeline_definition(config_data)
                    try:
                        _ps.create_pipeline(current_user, pipeline_id=pipeline_id,
                                            name=config_data.get('name'),
                                            description=config_data.get('description'),
                                            config=definition, status='stopped')
                    except _ps.AccessDenied:
                        return jsonify({'error': 'Pipeline not found or access denied'}), 404

                    self.logger.info(f"Pipeline imported: {pipeline_name} ({pipeline_id})")
                    
                    return jsonify({
                        'status': 'imported',
                        'pipeline_id': pipeline_id,
                        'pipeline_name': pipeline_name,
                        'model_id': new_model_id,
                        'message': f'Pipeline "{pipeline_name}" imported successfully'
                    })
                    
            except Exception as e:
                self.logger.error(f"Import pipeline error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        # Discovery API Routes
        # UNUSED: No frontend calls this endpoint
        # @self.app.route('/api/discovery/start', methods=['POST'])
        # def start_discovery():
        #     """Start network discovery"""
        #     try:
        #         if not self.discovery_manager:
        #             return jsonify({'error': 'Discovery manager not available'}), 503
        #         
        #         self.discovery_manager.start_discovery()
        #         return jsonify({'status': 'discovery_started'})
        #     except Exception as e:
        #         self.logger.error(f"Start discovery error: {str(e)}")
        #         return jsonify({'error': str(e)}), 500
        
        # UNUSED: No frontend calls this endpoint
        # @self.app.route('/api/discovery/stop', methods=['POST'])
        # def stop_discovery():
        #     """Stop network discovery"""
        #     try:
        #         if not self.discovery_manager:
        #             return jsonify({'error': 'Discovery manager not available'}), 503
        #         
        #         self.discovery_manager.stop_discovery()
        #         return jsonify({'status': 'discovery_stopped'})
        #     except Exception as e:
        #         self.logger.error(f"Stop discovery error: {str(e)}")
        #         return jsonify({'error': str(e)}), 500
        
        # UNUSED: No frontend calls this endpoint
        # @self.app.route('/api/discovery/scan', methods=['POST'])
        # def scan_network():
        #     """Manually scan network for nodes"""
        #     try:
        #         if not self.discovery_manager:
        #             return jsonify({'error': 'Discovery manager not available'}), 503
        #         
        #         # Run network scan in background thread
        #         import threading
        #         scan_thread = threading.Thread(target=self.discovery_manager.scan_network, daemon=True)
        #         scan_thread.start()
        #         
        #         return jsonify({'status': 'scan_started'})
        #     except Exception as e:
        #         self.logger.error(f"Network scan error: {str(e)}")
        #         return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/discovery/nodes', methods=['GET'])
        def get_discovered_nodes():
            """Get all discovered nodes"""
            try:
                if not self.discovery_manager:
                    return jsonify({'error': 'Discovery manager not available'}), 503
                
                nodes = self.discovery_manager.get_discovered_nodes()
                return jsonify({
                    'nodes': nodes,
                    'count': len(nodes),
                    'timestamp': datetime.now().isoformat()
                })
            except Exception as e:
                self.logger.error(f"Get discovered nodes error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/discovery/nodes/refresh', methods=['POST'])
        def refresh_discovered_nodes():
            """Refresh all discovered nodes"""
            try:
                if not self.discovery_manager:
                    return jsonify({'error': 'Discovery manager not available'}), 503
                
                # Trigger refresh of all nodes
                self.discovery_manager.refresh_all_nodes()
                
                # Return updated node list
                nodes = self.discovery_manager.get_discovered_nodes()
                return jsonify({
                    'success': True,
                    'message': 'Nodes refreshed successfully',
                    'nodes': nodes,
                    'count': len(nodes),
                    'timestamp': datetime.now().isoformat()
                })
            except Exception as e:
                self.logger.error(f"Refresh discovered nodes error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        @self.app.route('/api/discovery/nodes/<node_id>', methods=['GET'])
        def get_discovered_node(node_id):
            """Get specific discovered node information"""
            try:
                if not self.discovery_manager:
                    return jsonify({'error': 'Discovery manager not available'}), 503
                
                node = self.discovery_manager.get_node(node_id)
                if not node:
                    return jsonify({'error': 'Node not found'}), 404
                
                return jsonify(node)
            except Exception as e:
                self.logger.error(f"Get discovered node error: {str(e)}")
                return jsonify({'error': str(e)}), 500
        
        # Webhook receiver endpoint - receives POST requests from webhook destinations
        @self.app.route('/webhook/<webhook_id>', methods=['POST'])
        def webhook_receiver(webhook_id):
            """Receive webhook POST requests from detection pipelines"""
            try:
                # Get JSON data from request
                data = request.get_json(force=True) if request.is_json else request.form.to_dict()
                
                # Log the received webhook
                self.logger.info(f"Webhook received: ID={webhook_id}, Data keys: {list(data.keys()) if isinstance(data, dict) else 'N/A'}")
                print(f"[WEBHOOK RECEIVER] ✓ Received POST to /webhook/{webhook_id}")
                
                # Extract detection results if present
                if isinstance(data, dict):
                    results = data.get('results', {})
                    predictions = results.get('predictions', []) if isinstance(results, dict) else []
                    
                    if predictions:
                        print(f"[WEBHOOK RECEIVER] Processing {len(predictions)} detections")
                        for pred in predictions:
                            class_name = pred.get('class_name', 'unknown')
                            confidence = pred.get('confidence', 0)
                            track_id = pred.get('track_id')
                            print(f"[WEBHOOK RECEIVER]   - {class_name} (conf={confidence:.3f}, track_id={track_id})")
                    
                    # Log if images are included
                    if 'image' in data:
                        print(f"[WEBHOOK RECEIVER] Original image included (Base64)")
                    if 'result_image' in data:
                        print(f"[WEBHOOK RECEIVER] Result image included (Base64)")
                
                # Return success response
                return jsonify({
                    'success': True,
                    'webhook_id': webhook_id,
                    'message': 'Webhook received successfully',
                    'timestamp': datetime.now().isoformat(),
                    'received_data_keys': list(data.keys()) if isinstance(data, dict) else []
                }), 200
                
            except Exception as e:
                self.logger.error(f"Webhook receiver error: {str(e)}")
                print(f"[WEBHOOK RECEIVER] ✗ Error processing webhook: {str(e)}")
                return jsonify({
                    'success': False,
                    'error': str(e),
                    'webhook_id': webhook_id
                }), 500
        # (Was stacked onto webhook_receiver by a stray decorator: POST /api/discovery/
        # nodes/<id>/control dispatched into webhook_receiver(node_id=...) -> TypeError 500,
        # and this real handler was never registered.)
        @self.app.route('/api/discovery/nodes/<node_id>/control', methods=['POST'])
        def control_discovered_node(node_id):
            """Control discovered node operations"""
            try:
                if not self.discovery_manager:
                    return jsonify({'error': 'Discovery manager not available'}), 503
                
                data = request.get_json()
                action = data.get('action')
                
                if not action:
                    return jsonify({'error': 'Action required'}), 400
                
                result = self.discovery_manager.control_node(node_id, action)
                
                if 'error' in result:
                    return jsonify(result), 400
                
                return jsonify(result)
            except Exception as e:
                self.logger.error(f"Control discovered node error: {str(e)}")
                return jsonify({'error': str(e)}), 500
    
    def _load_settings_dict(self) -> dict:
        """PostgreSQL is authoritative for node identity, telemetry config and node-level
        publisher destinations (node_settings / publishers tables). The legacy
        node_settings.json is migrated ONCE (marker node_settings_to_postgres_v1) and then
        never read as authoritative again; it is retained read-only for rollback."""
        from InferenceNode import config_secrets, node_settings_store as nss, publisher_store as pst
        from InferenceNode.registry_migration import migrate_node_settings
        config_secrets.reload_keys()
        if not config_secrets.keys_available():
            self.logger.warning("[SECRETS] ARMYEYE_CONFIG_ENCRYPTION_KEY_FILE not loaded - publisher/telemetry "
                                "credentials cannot be stored or used until it is provided (fail-safe)")
        try:
            report = migrate_node_settings(self.settings_file)
            if report.discovered:
                self.logger.info(f"[MIGRATE] node_settings.json -> PostgreSQL: {report.as_dict()}")
        except Exception as e:  # noqa: BLE001 - never block startup; rerun resumes
            self.logger.error(f"[MIGRATE] node settings migration failed (will retry next start): {e}")
        settings = {}
        ident = nss.get_setting(nss.KEY_NODE_IDENTITY, runtime=True) or {}
        settings.update({k: v for k, v in ident.items() if k in ('node_id', 'node_name')})
        tel = nss.get_setting(nss.KEY_TELEMETRY, runtime=True)
        if tel:
            tel.pop('_secrets_ok', None)
            settings['telemetry'] = tel
        pubs = []
        for row in pst.list_publishers(kind='node_destination', runtime=True):
            if not row.get('secrets_ok', True):
                self.logger.error(f"[SECRETS] node destination {row['id']} has undecryptable credentials "
                                  f"(key missing/wrong) - NOT restored")
                continue
            pubs.append({'id': row['id'], 'type': row['type'], 'enabled': row['enabled'],
                         'config': row['config'], **{k: row['config'].get(k) for k in ('rate_limit', 'include_image_data')
                                                    if k in row['config']}})
        settings['publishers'] = pubs
        return settings

    def _audit_event(self, action: str, actor, target, detail: dict = None) -> None:
        """Audit trail for route-level artifact operations (media upload, ...). Never
        raises - the operation already happened; a failed audit write is logged. Detail
        carries structured metadata only: ids, sizes, hashes - never secrets or content."""
        try:
            from InferenceNode.auth.service import record_audit
            from flask import request as _rq
            ip = None
            try:
                ip = _rq.headers.get('X-Forwarded-For', _rq.remote_addr)
            except Exception:
                pass
            record_audit(actor=actor, action=action, target=str(target), detail=detail or {}, ip=ip)
        except Exception as e:  # noqa: BLE001
            self.logger.error(f"[AUDIT] could not record {action} for {target}: {e}")

    def _migrate_media_and_thumbnails_once(self):
        """Legacy InferenceNode/media and InferenceNode/pipelines/thumbnails ->
        ARTIFACT_ROOT/media + ARTIFACT_ROOT/thumbnails with PostgreSQL registry rows."""
        try:
            from InferenceNode.registry_migration import migrate_media_registry, migrate_thumbnails_registry
            base = self.legacy_root
            rep = migrate_media_registry(os.path.join(base, 'media'))
            if rep.discovered:
                self.logger.info(f"[MIGRATE] media -> ARTIFACT_ROOT/media: {rep.as_dict()}")
            rep = migrate_thumbnails_registry(os.path.join(base, 'pipelines', 'thumbnails'))
            if rep.discovered:
                self.logger.info(f"[MIGRATE] thumbnails -> ARTIFACT_ROOT/thumbnails: {rep.as_dict()}")
        except Exception as e:  # noqa: BLE001 - never block startup; rerun resumes
            self.logger.error(f"[MIGRATE] media/thumbnail migration failed (will retry next start): {e}")

    def _load_settings(self):
        """Load settings (PostgreSQL-backed; see _load_settings_dict)."""
        try:
            settings = self._load_settings_dict()
            if settings:
                
                # Restore node configuration
                if 'node_name' in settings and settings['node_name']:
                    self.node_name = settings['node_name']
                    self.logger.info(f"Restored node name: {self.node_name}")
                
                # Restore publisher configurations
                if 'publishers' in settings:
                    for pub_config in settings['publishers']:
                        try:
                            # Map saved type names to actual destination types
                            destination_type = pub_config['type']
                            if destination_type in ['MQTTDestination', 'mqtt']:
                                destination_type = 'mqtt'
                            elif destination_type in ['WebhookDestination', 'webhook']:
                                destination_type = 'webhook'
                            elif destination_type in ['SerialDestination', 'serial']:
                                destination_type = 'serial'
                            elif destination_type in ['FileDestination', 'file']:
                                destination_type = 'file'
                            
                            # Clean up config - remove null values and empty strings
                            config = pub_config.get('config', {})
                            cleaned_config = {}
                            for key, value in config.items():
                                if value is not None and value != '':
                                    cleaned_config[key] = value
                            
                            self.logger.info(f"Attempting to restore {destination_type} publisher with config: {cleaned_config}")
                            
                            destination = ResultDestination(destination_type)
                            
                            # Set context variables for variable substitution
                            destination.set_context_variables(
                                node_id=self.node_id,
                                node_name=self.node_name
                            )
                            
                            destination.configure(**cleaned_config)
                            
                            # Restore rate limit if it exists
                            if 'rate_limit' in pub_config and pub_config['rate_limit'] is not None:
                                destination.set_rate_limit(pub_config['rate_limit'])
                            
                            # Restore include_image_data flag if it exists
                            if 'include_image_data' in pub_config:
                                destination.include_image_data = pub_config['include_image_data']
                            
                            # Restore the ID if it exists
                            if 'id' in pub_config:
                                destination._id = pub_config['id']
                                self.result_publisher.destinations.append(destination)
                                self.logger.info(f"[OK] Successfully restored publisher: {destination_type} with ID: {pub_config['id']}")
                            else:
                                # Generate new ID for legacy publishers without IDs
                                publisher_id = self.result_publisher.add(destination)
                                self.logger.info(f"[OK] Successfully restored publisher: {destination_type} with new ID: {publisher_id}")
                        except Exception as e:
                            self.logger.error(f"[ERROR] Failed to restore publisher {pub_config.get('type', 'unknown')}: {str(e)}")
                            # Log the full config for debugging
                            self.logger.debug(f"Failed config was: {pub_config.get('config', {})}")
                
                # Restore telemetry configuration
                if 'telemetry' in settings and self.telemetry:
                    telemetry_config = settings['telemetry']
                    try:
                        # Restore MQTT configuration
                        if telemetry_config.get('mqtt_server'):
                            # Use the new format with separate server and port
                            host = telemetry_config['mqtt_server']
                            port = telemetry_config.get('mqtt_port', 1883)
                            
                            # Store MQTT configuration attributes
                            self.telemetry.mqtt_server = host
                            self.telemetry.mqtt_port = port
                            self.telemetry.mqtt_topic = telemetry_config.get('mqtt_topic', 'infernode/telemetry')
                            
                            self.telemetry.mqtt_username = telemetry_config.get('mqtt_username')
                            self.telemetry.mqtt_password = telemetry_config.get('mqtt_password')
                            self.telemetry.configure_mqtt(
                                mqtt_server=host,
                                mqtt_port=port,
                                mqtt_topic=telemetry_config.get('mqtt_topic', 'infernode/telemetry'),
                                mqtt_username=telemetry_config.get('mqtt_username'),
                                mqtt_password=telemetry_config.get('mqtt_password')
                            )
                        elif telemetry_config.get('mqtt_broker'):
                            # Legacy format for backward compatibility
                            mqtt_broker = telemetry_config['mqtt_broker']
                            if ':' in mqtt_broker:
                                host, port = mqtt_broker.split(':', 1)
                                port = int(port)
                            else:
                                host = mqtt_broker
                                port = 1883
                            
                            # Store MQTT configuration attributes
                            self.telemetry.mqtt_server = host
                            self.telemetry.mqtt_port = port
                            self.telemetry.mqtt_topic = telemetry_config.get('mqtt_topic', 'infernode/telemetry')
                            
                            self.telemetry.configure_mqtt(
                                mqtt_server=host,
                                mqtt_port=port,
                                mqtt_topic=telemetry_config.get('mqtt_topic', 'infernode/telemetry')
                            )
                        
                        # Configure publish interval
                        if hasattr(self.telemetry, 'update_interval'):
                            self.telemetry.update_interval = float(telemetry_config.get('publish_interval', 30))
                        
                        # Start or stop telemetry based on enabled flag
                        enabled = telemetry_config.get('enabled', False)
                        if enabled:
                            self.telemetry.start_telemetry()
                            self.logger.info("[OK] Telemetry started based on saved settings")
                        else:
                            self.telemetry.stop_telemetry()
                            self.logger.info("[STOP] Telemetry stopped based on saved settings")
                        
                        self.logger.info("Restored telemetry configuration")
                    except Exception as e:
                        self.logger.error(f"Failed to restore telemetry config: {str(e)}")
                
                # Favorites live in PostgreSQL (publisher_store) - nothing to restore in memory.
                self.logger.info("Settings loaded from PostgreSQL (node_settings/publishers)")
                
                # Log how many publishers were restored
                publisher_count = len(self.result_publisher.destinations) if self.result_publisher.destinations else 0
                self.logger.info(f"[LIST] Restored {publisher_count} publisher(s) from settings")
                
            else:
                self.logger.info("No settings file found, starting with default configuration")
                
        except Exception as e:
            self.logger.error(f"Failed to load settings: {str(e)}")
    
    def _save_settings(self):
        """Save current settings to file"""
        
        #TODO - check all these hard coded strings
        try:
            settings = {
                'node_id': self.node_id,
                'node_name': self.node_name,
                'publishers': [],
                'telemetry': {}
            }
            
            # Save publisher configurations
            for dest in self.result_publisher.destinations:
                # Determine destination type from class name or attributes
                dest_type = 'unknown'
                if hasattr(dest, 'server') and hasattr(dest, 'port') and hasattr(dest, 'topic'):
                    dest_type = 'mqtt'
                elif hasattr(dest, 'url'):
                    dest_type = 'webhook'
                elif hasattr(dest, 'com_port'):
                    dest_type = 'serial'
                elif hasattr(dest, 'file_path'):
                    dest_type = 'file'
                
                pub_config = {
                    'type': dest_type,
                    'config': {}
                }
                
                # Include publisher ID if it exists
                if hasattr(dest, '_id') and dest._id:
                    pub_config['id'] = dest._id
                
                # Include rate limit if it exists
                if hasattr(dest, 'rate_limit') and dest.rate_limit is not None:
                    pub_config['rate_limit'] = dest.rate_limit
                
                # Include image data flag if it exists
                if hasattr(dest, 'include_image_data'):
                    pub_config['include_image_data'] = getattr(dest, 'include_image_data', False)
                
                # Extract configuration based on destination type
                if dest_type == 'mqtt':
                    # MQTT destination - only save non-empty values
                    config = {}
                    if hasattr(dest, 'server') and getattr(dest, 'server', ''):
                        config['server'] = getattr(dest, 'server')
                    if hasattr(dest, 'port'):
                        config['port'] = getattr(dest, 'port', 1883)
                    if hasattr(dest, 'topic') and getattr(dest, 'topic', ''):
                        config['topic'] = getattr(dest, 'topic')
                    if hasattr(dest, 'username') and getattr(dest, 'username', ''):
                        config['username'] = getattr(dest, 'username')
                    if hasattr(dest, 'password') and getattr(dest, 'password', ''):
                        config['password'] = getattr(dest, 'password')
                    pub_config['config'] = config
                elif dest_type == 'webhook':
                    # Webhook destination - only save non-empty values
                    config = {}
                    if hasattr(dest, 'url') and getattr(dest, 'url', ''):
                        config['url'] = getattr(dest, 'url')
                    if hasattr(dest, 'headers') and getattr(dest, 'headers', {}):
                        config['headers'] = getattr(dest, 'headers')
                    if hasattr(dest, 'method'):
                        config['method'] = getattr(dest, 'method', 'POST')
                    pub_config['config'] = config
                elif dest_type == 'serial':
                    # Serial destination - only save non-empty values
                    config = {}
                    if hasattr(dest, 'com_port') and getattr(dest, 'com_port', ''):
                        config['com_port'] = getattr(dest, 'com_port')
                    if hasattr(dest, 'baud'):
                        config['baud'] = getattr(dest, 'baud', 9600)
                    if hasattr(dest, 'timeout'):
                        config['timeout'] = getattr(dest, 'timeout', 1)
                    pub_config['config'] = config
                elif dest_type == 'file':
                    # File destination - only save non-empty values
                    config = {}
                    if hasattr(dest, 'file_path') and getattr(dest, 'file_path', ''):
                        config['file_path'] = getattr(dest, 'file_path')
                    if hasattr(dest, 'format'):
                        config['format'] = getattr(dest, 'format', 'json')
                    pub_config['config'] = config
                
                settings['publishers'].append(pub_config)
            
            # Save telemetry configuration
            if self.telemetry:
                telemetry_config = {
                    'enabled': getattr(self.telemetry, 'running', False),
                    'publish_interval': getattr(self.telemetry, 'update_interval', 30)
                }
                
                # Add MQTT config if available
                if hasattr(self.telemetry, 'mqtt_server') and getattr(self.telemetry, 'mqtt_server', ''):
                    telemetry_config.update({
                        'mqtt_server': getattr(self.telemetry, 'mqtt_server', ''),
                        'mqtt_port': getattr(self.telemetry, 'mqtt_port', 1883),
                        'mqtt_topic': getattr(self.telemetry, 'mqtt_topic', 'infernode/telemetry')
                    })
                
                settings['telemetry'] = telemetry_config
            
            # Persist to PostgreSQL (authoritative). node_settings.json is NOT written any
            # more - no dual-write. Favorites are managed directly by publisher_store.
            from InferenceNode import node_settings_store as nss, publisher_store as pst
            from InferenceNode.config_secrets import SecretsUnavailable
            nss.set_setting(nss.KEY_NODE_IDENTITY, {'node_id': settings.get('node_id'),
                                                    'node_name': settings.get('node_name')})
            if 'telemetry' in settings:
                tel = dict(settings['telemetry'])
                for k in ('mqtt_username', 'mqtt_password'):
                    v = getattr(self.telemetry, k, None) if self.telemetry else None
                    if v:
                        tel[k] = v
                try:
                    nss.set_setting(nss.KEY_TELEMETRY, tel)
                except SecretsUnavailable:
                    self.logger.error("[SECRETS] telemetry MQTT credentials NOT persisted: encryption key unavailable")
                    tel.pop('mqtt_password', None); tel.pop('mqtt_username', None)
                    nss.set_setting(nss.KEY_TELEMETRY, tel)
            existing = {r['id']: r for r in pst.list_publishers(kind='node_destination')}
            seen = set()
            for pub in settings.get('publishers', []):
                pid = pub.get('id') or str(uuid.uuid4())
                seen.add(pid)
                cfg = dict(pub.get('config') or {})
                for k in ('rate_limit', 'include_image_data'):
                    if k in pub and pub[k] is not None:
                        cfg[k] = pub[k]
                try:
                    if pid in existing:
                        pst.update_publisher(pid, type=pub.get('type'), config=cfg, enabled=pub.get('enabled', True))
                    else:
                        pst.create_publisher(publisher_id=pid, name=pub.get('name') or pub.get('type'),
                                             type=pub.get('type'), config=cfg, kind='node_destination',
                                             enabled=pub.get('enabled', True))
                except SecretsUnavailable:
                    self.logger.error(f"[SECRETS] node destination {pid} NOT persisted: encryption key unavailable")
            for pid in set(existing) - seen:
                pst.delete_publisher(pid)          # destination removed at runtime -> row removed
            self.logger.info("Settings saved to PostgreSQL (node_settings/publishers)")
            
        except Exception as e:
            self.logger.error(f"Failed to save settings: {str(e)}")
    
    def start(self, enable_discovery: bool = True, enable_telemetry: bool = False, production: bool = False):
        """Start the inference node
        
        Args:
            enable_discovery (bool): Enable discovery manager for finding other nodes
            enable_telemetry (bool): Enable telemetry data collection
            production (bool): If True, use Waitress WSGI server for production.
                              If False, use Flask development server.
        """
        try:
            # Initialize pipeline information in node_info
            self._update_node_info_with_pipelines()
            
            # Start discovery manager (for finding other nodes)
            if enable_discovery and self.discovery_manager:
                print(f"[DISCOVER] Starting discovery manager...")
                self.discovery_manager.start_discovery()
                print(f"[OK] Discovery manager started - listening on port {self.discovery_manager.discovery_port}")
                
                # Perform initial network scan to discover existing nodes
                print(f"[SCAN] Performing initial network scan...")
                import threading
                scan_thread = threading.Thread(target=self.discovery_manager.scan_network, daemon=True)
                scan_thread.start()
                print(f"[OK] Initial network scan started")
            elif enable_discovery and not self.discovery_manager:
                print(f"[ERROR] Discovery manager requested but service not available")
            
            # Start telemetry if requested
            if enable_telemetry and self.telemetry:
                print(f"[DATA] Starting telemetry service...")
                self.telemetry.start_telemetry()
                print(f"[OK] Telemetry service started")
            elif enable_telemetry and not self.telemetry:
                print(f"[ERROR] Telemetry requested but service not available")
            
            # Start the web server
            if production:
                from waitress import serve
                print(f"[LAUNCH] Starting production web server (Waitress) on port {self.port}...")
                self.logger.info(f"Starting inference node in production mode on port {self.port}")
                serve(self.app, host='0.0.0.0', port=self.port, threads=6)
            else:
                print(f"[LAUNCH] Starting development web server (Flask) on port {self.port}...")
                self.logger.info(f"Starting inference node in development mode on port {self.port}")
                self.app.run(host='0.0.0.0', port=self.port, debug=False)
            
        except Exception as e:
            self.logger.error(f"Failed to start node: {str(e)}")
            self.stop()
    
    def stop(self):
        """Stop the inference node and cleanup resources"""
        self.logger.info("Stopping inference node...")
        
        # Stop services
        if self.discovery_manager:
            self.discovery_manager.stop_discovery()
        if self.telemetry:
            self.telemetry.stop_telemetry()
        
        # Clear publishers
        self.result_publisher.clear()
        
        self.logger.info("Inference node stopped")


if __name__ == "__main__":
    import sys
    import argparse

    # Load a repo-root .env before reading any env config (e.g. WEBHOOK_AUTH_TOKEN).
    # override=False so real shell/Docker/OS env wins; missing dotenv is non-fatal.
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
                    override=False)
    except Exception:
        pass

    # Parse command line arguments with proper argument parser
    parser = argparse.ArgumentParser(description='Start an InferenceNode')
    parser.add_argument('--port', type=int, default=5555, help='Port to run the web interface on')
    parser.add_argument('--node-id', type=str, help='Specific node ID to use (optional)')
    parser.add_argument('--node-name', type=str, help='Human-readable node name')
    parser.add_argument('--discovery', type=str, default='true', choices=['true', 'false'], help='Enable discovery service')
    parser.add_argument('--telemetry', type=str, default='true', choices=['true', 'false'], help='Enable telemetry service')
    
    args = parser.parse_args()
    
    port = args.port
    node_name = args.node_name
    node_id = args.node_id
    enable_discovery = args.discovery.lower() == 'true'
    enable_telemetry = args.telemetry.lower() == 'true'
    
    # Create and start the node
    print(f"Starting InferenceNode...")
    print(f"  Node ID: {node_id or 'Auto-generated'}")
    print(f"  Node Name: {node_name or 'Auto-generated'}")
    print(f"  Port: {port}")
    print(f"  Discovery: {'Enabled' if enable_discovery else 'Disabled'}")
    print(f"  Telemetry: {'Enabled' if enable_telemetry else 'Disabled'}")
    print(f"  Web Interface: http://localhost:{port}")
    print("-" * 50)
    
    node = InferenceNode(node_name, port=port, node_id=node_id)
    
    try:
        node.start(enable_discovery=enable_discovery, enable_telemetry=enable_telemetry)
    except KeyboardInterrupt:
        print("\nShutting down...")
        node.stop()
