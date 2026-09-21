import logging
from logging.handlers import RotatingFileHandler
import sys
import threading
import json
from datetime import datetime
from collections import deque
from typing import List, Dict, Any, Optional


class MemoryLogHandler(logging.Handler):
    """Custom logging handler that stores logs in memory for web interface access"""
    
    def __init__(self, max_logs: int = 1000):
        super().__init__()
        self.max_logs = max_logs
        self.logs = deque(maxlen=max_logs)
        self._lock = threading.RLock()
        
        # Component mapping based on logger names
        self.component_mapping = {
            'InferenceNode': 'system',
            'InferenceEngine': 'inference',
            'InferenceEngineFactory': 'inference',
            'ResultPublisher': 'publisher',
            'NodeTelemetry': 'telemetry',
            'NodeDiscovery': 'discovery',
            'PipelineManager': 'inference',
            'werkzeug': 'web',
            'flask': 'web',
            'app': 'web',
            'web': 'web'
        }
        
        # Filter out routine static file requests
        self.filter_static_requests = True
    
    def emit(self, record):
        """Emit a log record to memory storage"""
        try:
            with self._lock:
                # Filter out routine static file requests if enabled
                if self.filter_static_requests and self._is_static_request(record):
                    return
                
                # Determine component from logger name
                component = self._determine_component(record.name)
                
                # Create log entry
                log_entry = {
                    'timestamp': datetime.fromtimestamp(record.created).isoformat(),
                    'level': record.levelname,
                    'component': component,
                    'message': record.getMessage(),
                    'logger': record.name,
                    'module': record.module,
                    'funcName': record.funcName,
                    'lineno': record.lineno
                }
                
                # Add exception info if present
                if record.exc_info:
                    log_entry['exception'] = self.format(record)
                
                # Add extra details if present
                if hasattr(record, 'details'):
                    details = getattr(record, 'details', None)
                    if details:
                        log_entry['details'] = details
                
                self.logs.append(log_entry)
                
        except Exception:
            # Don't let logging errors break the application
            self.handleError(record)
    
    def _is_static_request(self, record) -> bool:
        """Check if this is a routine static file request that should be filtered"""
        message = record.getMessage()
        
        # Filter out 304 (Not Modified) responses for static files
        if '304' in message and any(ext in message for ext in ['.css', '.js', '.png', '.jpg', '.ico', '.svg']):
            return True
        
        # Filter out successful static file requests
        if '200' in message and '/static/' in message:
            return True
            
        return False
    
    def _determine_component(self, logger_name: str) -> str:
        """Determine component name from logger name"""
        # Try exact match first
        if logger_name in self.component_mapping:
            return self.component_mapping[logger_name]
        
        # Try partial matches
        for key, component in self.component_mapping.items():
            if key.lower() in logger_name.lower():
                return component
        
        # Default to system if no match found
        return 'system'
    
    def get_logs(self, 
                 level: Optional[str] = None,
                 component: Optional[str] = None,
                 search: Optional[str] = None,
                 limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get filtered logs"""
        with self._lock:
            logs = list(self.logs)
        
        # Apply filters
        if level:
            logs = [log for log in logs if log['level'] == level.upper()]
        
        if component:
            logs = [log for log in logs if log['component'] == component]
        
        if search:
            search_lower = search.lower()
            logs = [log for log in logs 
                   if search_lower in log['message'].lower() or
                      search_lower in log.get('logger', '').lower()]
        
        # Apply limit
        if limit:
            logs = logs[-limit:]
        
        # Return in reverse chronological order (newest first)
        return list(reversed(logs))
    
    def clear_logs(self):
        """Clear all stored logs"""
        with self._lock:
            self.logs.clear()
    
    def get_log_statistics(self) -> Dict[str, Any]:
        """Get statistics about stored logs"""
        with self._lock:
            logs = list(self.logs)
        
        stats = {
            'total': len(logs),
            'by_level': {'DEBUG': 0, 'INFO': 0, 'WARNING': 0, 'ERROR': 0, 'CRITICAL': 0},
            'by_component': {}
        }
        
        for log in logs:
            # Count by level
            level = log['level']
            if level in stats['by_level']:
                stats['by_level'][level] += 1
            
            # Count by component
            component = log['component']
            if component not in stats['by_component']:
                stats['by_component'][component] = 0
            stats['by_component'][component] += 1
        
        return stats


class AgeAndSizeRotatingHandler(RotatingFileHandler):
    def __init__(self, *args, retention_days=7, **kwargs):
        super().__init__(*args, **kwargs)
        self.retention_days = retention_days
        self._last_prune = 0
        self.prune()

    def prune(self):
        import time
        from pathlib import Path
        now = time.time()
        self._last_prune = now
        base = Path(self.baseFilename)
        for path in base.parent.glob(base.name + '.*'):
            if path.name[len(base.name) + 1:].isdigit():
                try:
                    if path.stat().st_mtime < now - self.retention_days * 86400:
                        path.unlink()
                except FileNotFoundError:
                    pass

    def emit(self, record):
        import time
        if time.time() - self._last_prune >= 3600:
            self.prune()
        super().emit(record)


class LogManager:
    """Manages logging configuration and handlers"""
    
    def __init__(self):
        self.memory_handler = None
        self.file_handler = None
        self.stream_handler = None
        self.log_level = logging.INFO
        self.file_logging_enabled = True
        self.max_log_size_mb = 10
        self.retention_days = 7

    def _detach_own_handlers(self, root_logger, keep_memory: bool = False):
        """Remove ONLY handlers this LogManager attached to root - never
        third-party handlers someone else installed. Each removed handler is
        close()d: for the RotatingFileHandler that releases its file descriptor
        (no fd leak across repeated settings changes); for the stdout
        StreamHandler, Handler.close() detaches the handler object WITHOUT
        closing sys.stdout (StreamHandler never closes its stream).

        keep_memory=True preserves the MemoryLogHandler and its captured
        history - /api/logs must survive a settings-driven reconfigure.
        """
        for attr in ('memory_handler', 'file_handler', 'stream_handler'):
            if keep_memory and attr == 'memory_handler':
                continue
            handler = getattr(self, attr)
            if handler is None:
                continue
            if handler in root_logger.handlers:
                root_logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
            setattr(self, attr, None)

    def setup_logging(self, log_level: str = 'INFO',
                     enable_file_logging: bool = True,
                     max_memory_logs: int = 1000):
        """Setup logging with memory, stdout and optionally file handlers.

        Idempotent: calling this again (e.g. from the settings API on a
        log-level change) REPLACES this manager's own handlers instead of
        stacking duplicates - two RotatingFileHandlers on one path is the
        classic rotation-corruption setup. Post-condition after any number of
        calls: root carries exactly one manager-owned file handler, one stdout
        handler and one memory handler. The MemoryLogHandler (and its history,
        which backs /api/logs) is kept when one already exists.
        """

        # Convert string level to logging constant
        numeric_level = getattr(logging, log_level.upper(), logging.INFO)
        self.log_level = numeric_level

        root_logger = logging.getLogger()
        had_memory = self.memory_handler is not None
        self._detach_own_handlers(root_logger, keep_memory=had_memory)

        # Setup formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )

        # Memory handler (preserved across reconfigures once created)
        if not had_memory:
            self.memory_handler = MemoryLogHandler(max_logs=max_memory_logs)
            self.memory_handler.setLevel(logging.DEBUG)  # Capture all levels
            self.memory_handler.setFormatter(formatter)
        if self.memory_handler not in root_logger.handlers:
            root_logger.addHandler(self.memory_handler)

        # Stdout handler: container/stdout logs are the authoritative stream in
        # production - logger.* records must reach `docker logs`, not only print().
        self.stream_handler = logging.StreamHandler(sys.stdout)
        self.stream_handler.setLevel(numeric_level)
        self.stream_handler.setFormatter(formatter)
        root_logger.addHandler(self.stream_handler)

        root_logger.setLevel(numeric_level)

        # Enable Werkzeug logging for HTTP requests
        werkzeug_logger = logging.getLogger('werkzeug')
        werkzeug_logger.setLevel(logging.INFO)  # Show all HTTP requests

        # Setup file logging if enabled
        if enable_file_logging:
            self._setup_file_logging()

        self.file_logging_enabled = enable_file_logging
        
    def _setup_file_logging(self):
        """Setup file logging with rotation"""
        try:
            import logging.handlers
            import os
            
            # Create logs directory if it doesn't exist
            log_dir = os.path.join(os.path.dirname(__file__), 'logs')
            os.makedirs(log_dir, exist_ok=True)
            
            # Setup rotating file handler
            log_file = os.path.join(log_dir, 'infernode.log')
            max_bytes = self.max_log_size_mb * 1024 * 1024  # Convert MB to bytes
            
            self.file_handler = AgeAndSizeRotatingHandler(
                log_file, 
                maxBytes=max_bytes, 
                backupCount=5, retention_days=self.retention_days
            )
            
            self.file_handler.setLevel(self.log_level)
            
            # Setup formatter for file
            file_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
            )
            self.file_handler.setFormatter(file_formatter)
            
            # Add to root logger
            root_logger = logging.getLogger()
            root_logger.addHandler(self.file_handler)
            
        except Exception as e:
            print(f"Failed to setup file logging: {e}")
    
    def update_settings(self, settings: Dict[str, Any]):
        """Update logging settings"""
        try:
            if not isinstance(settings, dict):
                return False
            if 'log_level' in settings and settings['log_level'] not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
                return False
            for key in ('max_log_size_mb', 'retention_days'):
                if key in settings and (type(settings[key]) is not int or settings[key] < 1):
                    return False
            if 'enable_file_logging' in settings and type(settings['enable_file_logging']) is not bool:
                return False
            # Apply rotation settings before a new handler is created.
            if 'max_log_size_mb' in settings:
                self.max_log_size_mb = settings['max_log_size_mb']
                if self.file_handler:
                    self.file_handler.maxBytes = self.max_log_size_mb * 1024 * 1024
            # Update log level
            if 'log_level' in settings:
                level_str = settings['log_level'].upper()
                numeric_level = getattr(logging, level_str, logging.INFO)
                self.log_level = numeric_level
                
                # Update root logger level
                root_logger = logging.getLogger()
                root_logger.setLevel(numeric_level)
                
                # Update file handler level if exists
                if self.file_handler:
                    self.file_handler.setLevel(numeric_level)
            
            # Update file logging settings
            if 'enable_file_logging' in settings:
                self.file_logging_enabled = settings['enable_file_logging']
                
                if self.file_logging_enabled and not self.file_handler:
                    self._setup_file_logging()
                elif not self.file_logging_enabled and self.file_handler:
                    # Remove file handler
                    root_logger = logging.getLogger()
                    root_logger.removeHandler(self.file_handler)
                    self.file_handler.close()
                    self.file_handler = None
            
            if 'max_log_size_mb' in settings:
                self.max_log_size_mb = settings['max_log_size_mb']
            
            if 'retention_days' in settings:
                self.retention_days = settings['retention_days']
                if self.file_handler and hasattr(self.file_handler, 'prune'):
                    self.file_handler.retention_days = self.retention_days
                    self.file_handler.prune()
            
            return True
            
        except Exception as e:
            print(f"Failed to update logging settings: {e}")
            return False
    
    def get_settings(self) -> Dict[str, Any]:
        """Get current logging settings"""
        return {
            'log_level': logging.getLevelName(self.log_level),
            'file_logging_enabled': self.file_logging_enabled,
            'max_log_size_mb': self.max_log_size_mb,
            'retention_days': self.retention_days
        }
