// InferNode Web Interface JavaScript

// Global variables
let nodeInfo = null;
let engineStatus = null;

// --- Auth / CSRF helpers -------------------------------------------------
// Read the CSRF token rendered into <meta name="csrf-token"> by base.html.
function getCsrfToken() {
    const el = document.querySelector('meta[name="csrf-token"]');
    return el ? el.getAttribute('content') : null;
}

// fetch() wrapper that attaches the CSRF token to state-changing requests and
// redirects to /login on a 401. Prefer this for POST/PUT/PATCH/DELETE calls so
// the app keeps working when API CSRF enforcement is turned on.
// Same-origin CSRF for EVERY state-changing request, including pages that still use
// raw fetch()/XMLHttpRequest. Server-side, admin write routes (models, publishers,
// telemetry/node config, logs, media) now require X-CSRFToken; instead of touching
// dozens of call sites, the token is attached centrally here. Cross-origin URLs are
// left untouched so a token is never sent to another host.
(function installCsrfOnStateChangingRequests() {
    const MUTATING = ['POST', 'PUT', 'PATCH', 'DELETE'];
    const sameOrigin = (u) => {
        try { return new URL(String(u), window.location.href).origin === window.location.origin; }
        catch (e) { return false; }
    };
    const nativeFetch = window.fetch;
    if (nativeFetch && !nativeFetch.__armyeyeCsrf) {
        const patched = function (input, init) {
            const opts = init ? Object.assign({}, init) : {};
            const method = String(opts.method || (input && input.method) || 'GET').toUpperCase();
            const url = (input && input.url) || input;
            if (MUTATING.includes(method) && sameOrigin(url)) {
                const token = getCsrfToken();
                if (token) {
                    const h = new Headers(opts.headers || (input && input.headers) || undefined);
                    if (!h.has('X-CSRFToken')) h.set('X-CSRFToken', token);
                    opts.headers = h;
                }
            }
            return nativeFetch.call(this, input, opts);
        };
        patched.__armyeyeCsrf = true;
        window.fetch = patched;
    }
    const nativeOpen = XMLHttpRequest.prototype.open;
    const nativeSend = XMLHttpRequest.prototype.send;
    if (nativeOpen && !nativeOpen.__armyeyeCsrf) {
        XMLHttpRequest.prototype.open = function (method, url) {
            this.__armyeyeMutating = MUTATING.includes(String(method).toUpperCase()) && sameOrigin(url);
            return nativeOpen.apply(this, arguments);
        };
        XMLHttpRequest.prototype.open.__armyeyeCsrf = true;
        XMLHttpRequest.prototype.send = function () {
            if (this.__armyeyeMutating) {
                const token = getCsrfToken();
                if (token) { try { this.setRequestHeader('X-CSRFToken', token); } catch (e) { /* already sent */ } }
            }
            return nativeSend.apply(this, arguments);
        };
    }
})();

async function fetchJSON(url, options = {}) {
    const opts = Object.assign({}, options);
    opts.headers = Object.assign({}, options.headers || {});
    const method = (opts.method || 'GET').toUpperCase();
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
        const token = getCsrfToken();
        if (token) opts.headers['X-CSRFToken'] = token;
    }
    const response = await fetch(url, opts);
    if (response.status === 401) {
        window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
        throw new Error('Not authenticated');
    }
    return response;
}

// Initialize the application
document.addEventListener('DOMContentLoaded', function() {
    initializeApp();
});

// Initialize application
async function initializeApp() {
    try {
        await loadNodeInfo();
        await checkEngineStatus();
        if (typeof discoverNodes === 'function' && !window.__discoverNodesOwnedByPage) { /* page-owned */ }
        
        // Set up periodic updates
        setInterval(updateNodeStatus, 30000); // Every 30 seconds
        
    } catch (error) {
        console.error('Failed to initialize app:', error);
    }
}

// Load node information
async function loadNodeInfo() {
    try {
        const response = await fetch('/api/info');
        if (response.ok) {
            nodeInfo = await response.json();
            updateNodeInfoDisplay();
        } else {
            throw new Error(`HTTP ${response.status}`);
        }
    } catch (error) {
        console.error('Failed to load node info:', error);
        showAlert('error', 'Failed to load node information');
    }
}

// Update node info display
function updateNodeInfoDisplay() {
    if (!nodeInfo) return;
    
    // Update status indicators (removed from main page, now in navbar and node info page)
    const nodeIdElement = document.getElementById('node-id'); // Legacy - might not exist
    const nodePortElement = document.getElementById('node-port'); // Legacy - might not exist
    
    // Update legacy elements if they exist (for backward compatibility)
    if (nodeIdElement) nodeIdElement.textContent = nodeInfo.node_id;
    if (nodePortElement) nodePortElement.textContent = nodeInfo.api_port;
    
    // Update node info page elements
    const nodeInfoIdElement = document.getElementById('nodeId');
    const apiPortElement = document.getElementById('apiPort');
    const nodeStatusDisplayElement = document.getElementById('nodeStatusDisplay');
    
    if (nodeInfoIdElement) nodeInfoIdElement.textContent = nodeInfo.node_id || 'N/A';
    if (apiPortElement) apiPortElement.textContent = nodeInfo.api_port || 'N/A';
    if (nodeStatusDisplayElement) {
        // Update status badge on node info page
        updateStatusBadge(nodeStatusDisplayElement);
    }
    
    // Update navbar status badge
    const navbarStatusElement = document.getElementById('node-status');
    if (navbarStatusElement) {
        updateStatusBadge(navbarStatusElement);
    }
    
    // Update system info on dashboard
    const cpuCoresElement = document.getElementById('cpu-cores');
    const memoryTotalElement = document.getElementById('memory-total');
    const platformElement = document.getElementById('platform');
    const gpuStatusElement = document.getElementById('gpu-status');
    
    if (cpuCoresElement) cpuCoresElement.textContent = nodeInfo.cpu_count || 'N/A';
    if (memoryTotalElement) memoryTotalElement.textContent = nodeInfo.memory_gb ? `${nodeInfo.memory_gb.toFixed(1)} GB` : 'N/A';
    if (platformElement) platformElement.textContent = nodeInfo.platform || 'N/A';
    if (gpuStatusElement) {
        // Count total GPUs from all vendors
        let gpuCount = 0;
        if (nodeInfo.hardware) {
            if (nodeInfo.hardware.nvidia && nodeInfo.hardware.nvidia.gpu_devices) {
                gpuCount += nodeInfo.hardware.nvidia.gpu_devices.length;
            }
            if (nodeInfo.hardware.intel && nodeInfo.hardware.intel.gpu_devices) {
                gpuCount += nodeInfo.hardware.intel.gpu_devices.length;
            }
            if (nodeInfo.hardware.amd && nodeInfo.hardware.amd.gpu_devices) {
                gpuCount += nodeInfo.hardware.amd.gpu_devices.length;
            }
            if (nodeInfo.hardware.apple && nodeInfo.hardware.apple.gpu) {
                gpuCount += 1;
            }
        }
        gpuStatusElement.textContent = gpuCount;
    }
}

// Helper function to update status badges
function updateStatusBadge(element) {
    // For now, we'll assume the node is online since we got a response
    // In the future, this could be based on actual health checks
    element.textContent = 'Online';
    element.className = 'badge bg-success';
}

// Update node status (periodic update function)
// Check engine status
async function checkEngineStatus() {
    try {
        // This would be a call to get current engine status
        // For now, simulate
        engineStatus = null;
        updateEngineStatusDisplay();
    } catch (error) {
        console.error('Failed to check engine status:', error);
    }
}

// Update engine status display
function updateEngineStatusDisplay() {
    const engineStatusElements = document.querySelectorAll('#engine-status, #currentEngine, #engineStatus');
    
    engineStatusElements.forEach(element => {
        if (!element) return;
        
        if (engineStatus && engineStatus.loaded) {
            element.innerHTML = `
                <div class="alert alert-success">
                    <i class="fas fa-check-circle me-2"></i>
                    <strong>${engineStatus.engine_type}</strong> engine loaded
                    <br><small>Model: ${engineStatus.model_id} | Device: ${engineStatus.device}</small>
                </div>
            `;
            
            // Show unload button if exists
            const unloadBtn = document.getElementById('unloadBtn');
            if (unloadBtn) unloadBtn.style.display = 'block';
            
        } else {
            element.innerHTML = `
                <div class="alert alert-warning">
                    <i class="fas fa-exclamation-triangle me-2"></i>
                    No engine loaded
                </div>
            `;
            
            // Hide unload button if exists
            const unloadBtn = document.getElementById('unloadBtn');
            if (unloadBtn) unloadBtn.style.display = 'none';
        }
    });
}

// Discover nodes on network
// discoverNodes(): implemented by the page that owns #discovered-nodes (dashboard.html);
// the former fake stub here always reported 'No other nodes found' and caused a double fetch.

// Refresh node information
async function refreshNodeInfo() {
    const button = document.querySelector('button[onclick="refreshNodeInfo()"]');
    if (button) {
        const originalHTML = button.innerHTML;
        button.innerHTML = '<i class="fas fa-spinner fa-spin me-2"></i>Refreshing...';
        button.disabled = true;
        
        try {
            await loadNodeInfo();
            await checkEngineStatus();
            showAlert('success', 'Node information refreshed');
        } catch (error) {
            showAlert('error', 'Failed to refresh node information');
        } finally {
            button.innerHTML = originalHTML;
            button.disabled = false;
        }
    }
}

// Update node status periodically
async function updateNodeStatus() {
    // Single definition (a shadowed duplicate used to make the 30s timer only recolour the
    // badge). Refresh the system-overview values AND the online badge.
    try {
        const response = await fetch('/api/info');
        if (response.ok) {
            try { nodeInfo = await response.clone().json(); updateNodeInfoDisplay(); } catch (e) { /* display refresh is best-effort */ }
            const statusElement = document.getElementById('node-status');
            if (statusElement) {
                statusElement.className = 'badge bg-success';
                statusElement.textContent = 'Online';
            }
        } else {
            throw new Error('Node not responding');
        }
    } catch (error) {
        const statusElement = document.getElementById('node-status');
        if (statusElement) {
            statusElement.className = 'badge bg-danger';
            statusElement.textContent = 'Offline';
        }
    }
}

// Notification system
const NotificationManager = {
    container: null,
    notifications: [],
    maxNotifications: 5, // Maximum notifications to show
    defaultDuration: 5000,
    fontAwesomeReady: false,
    
    init() {
        if (!this.container) {
            this.container = document.createElement('div');
            this.container.className = 'notification-container';
            this.container.style.cssText = `
                position: fixed;
                top: 20px;
                right: 20px;
                z-index: 9999;
                max-width: 400px;
                pointer-events: none;
            `;
            document.body.appendChild(this.container);
        }
        
        // Check if Font Awesome is ready
        this.checkFontAwesome();
    },
    
    checkFontAwesome() {
        // Only check once
        if (this.fontAwesomeReady) return;
        
        try {
            // Create a test icon to check if Font Awesome is loaded
            const testIcon = document.createElement('i');
            testIcon.className = 'fas fa-heart';
            testIcon.style.position = 'absolute';
            testIcon.style.left = '-9999px';
            testIcon.style.visibility = 'hidden';
            document.body.appendChild(testIcon);
            
            const computedStyle = window.getComputedStyle(testIcon);
            const fontFamily = computedStyle.fontFamily;
            
            // More comprehensive check for Font Awesome
            this.fontAwesomeReady = (
                fontFamily.includes('Font Awesome') || 
                fontFamily.includes('FontAwesome') ||
                fontFamily.includes('"Font Awesome 6 Free"') ||
                fontFamily.includes('"Font Awesome 6 Pro"') ||
                fontFamily.includes('fa-solid-900')
            );
            
            document.body.removeChild(testIcon);
            
            // Debug logging
            console.log('Font Awesome Check:', {
                fontFamily: fontFamily,
                fontAwesomeReady: this.fontAwesomeReady,
                testIconWidth: testIcon.offsetWidth
            });
            
        } catch (error) {
            console.warn('Font Awesome check failed:', error);
            this.fontAwesomeReady = false;
        }
    },
    
    show(type, message, duration = this.defaultDuration) {
        this.init();
        
        // Remove oldest notification if we're at the limit
        if (this.notifications.length >= this.maxNotifications) {
            this.remove(this.notifications[0].element, false);
        }
        
        // Create notification element
        const notification = document.createElement('div');
        notification.className = `alert alert-${type === 'error' ? 'danger' : type} alert-dismissible fade notification-item`;
        notification.style.cssText = `
            margin-bottom: 10px;
            opacity: 0;
            transform: translateX(100%);
            transition: all 0.3s ease-in-out;
            pointer-events: auto;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            border: none;
            max-width: 100%;
            word-wrap: break-word;
        `;
        
        notification.innerHTML = `
            <div class="d-flex align-items-start">
                <div class="flex-grow-1">
                    ${this.getIcon(type)}
                    ${message}
                </div>
                <button type="button" class="btn-close ms-2" style="font-size: 0.8rem;"></button>
            </div>
            ${duration > 0 ? '<div class="notification-progress-bar" style="width: 100%;"></div>' : ''}
        `;
        
        // Add click handler for close button
        const closeBtn = notification.querySelector('.btn-close');
        closeBtn.addEventListener('click', () => this.remove(notification));
        
        // Add to container
        this.container.appendChild(notification);
        
        // Track notification
        const notificationObj = {
            element: notification,
            timestamp: Date.now(),
            timeout: null
        };
        this.notifications.push(notificationObj);
        
        // Animate in
        requestAnimationFrame(() => {
            notification.style.opacity = '1';
            notification.style.transform = 'translateX(0)';
            notification.classList.add('show');
        });
        
        // Set auto-remove timeout
        if (duration > 0) {
            const progressBar = notification.querySelector('.notification-progress-bar');
            if (progressBar) {
                // Start progress bar animation
                progressBar.style.transition = `width ${duration}ms linear`;
                requestAnimationFrame(() => {
                    progressBar.style.width = '0%';
                });
            }
            
            notificationObj.timeout = setTimeout(() => {
                this.remove(notification);
            }, duration);
        }
        
        return notification;
    },
    
    remove(notification, animate = true) {
        const index = this.notifications.findIndex(n => n.element === notification);
        if (index === -1) return;
        
        const notificationObj = this.notifications[index];
        
        // Clear timeout
        if (notificationObj.timeout) {
            clearTimeout(notificationObj.timeout);
        }
        
        // Remove from tracking
        this.notifications.splice(index, 1);
        
        if (animate) {
            // Animate out
            notification.style.opacity = '0';
            notification.style.transform = 'translateX(100%)';
            notification.style.maxHeight = notification.offsetHeight + 'px';
            
            setTimeout(() => {
                notification.style.maxHeight = '0';
                notification.style.marginBottom = '0';
                notification.style.paddingTop = '0';
                notification.style.paddingBottom = '0';
                
                setTimeout(() => {
                    if (notification.parentNode) {
                        notification.remove();
                    }
                }, 150);
            }, 150);
        } else {
            // Immediate removal
            if (notification.parentNode) {
                notification.remove();
            }
        }
    },
    
    getIcon(type) {
        // Re-check Font Awesome if needed
        this.checkFontAwesome();
        
        // TEMPORARY: Force fallback icons for testing
        // Remove this line after testing to re-enable Font Awesome
        const useFallback = true; // Set to false to use Font Awesome
        
        if (this.fontAwesomeReady && !useFallback) {
            const icons = {
                success: '<i class="fas fa-check-circle me-2 text-success"></i>',
                error: '<i class="fas fa-exclamation-circle me-2 text-danger"></i>',
                danger: '<i class="fas fa-exclamation-circle me-2 text-danger"></i>',
                warning: '<i class="fas fa-exclamation-triangle me-2 text-warning"></i>',
                info: '<i class="fas fa-info-circle me-2 text-info"></i>',
                primary: '<i class="fas fa-info-circle me-2 text-primary"></i>'
            };
            return icons[type] || icons.info;
        } else {
            // Fallback to styled badges if Font Awesome fails
            const fallbackIcons = {
                success: '<span class="badge bg-success me-2" style="border-radius: 50%; width: 16px; height: 16px; font-size: 10px; display: inline-flex; align-items: center; justify-content: center;">✓</span>',
                error: '<span class="badge bg-danger me-2" style="border-radius: 50%; width: 16px; height: 16px; font-size: 10px; display: inline-flex; align-items: center; justify-content: center;">!</span>',
                danger: '<span class="badge bg-danger me-2" style="border-radius: 50%; width: 16px; height: 16px; font-size: 10px; display: inline-flex; align-items: center; justify-content: center;">!</span>',
                warning: '<span class="badge bg-warning text-dark me-2" style="border-radius: 50%; width: 16px; height: 16px; font-size: 10px; display: inline-flex; align-items: center; justify-content: center;">!</span>',
                info: '<span class="badge bg-info me-2" style="border-radius: 50%; width: 16px; height: 16px; font-size: 10px; display: inline-flex; align-items: center; justify-content: center;">i</span>',
                primary: '<span class="badge bg-primary me-2" style="border-radius: 50%; width: 16px; height: 16px; font-size: 10px; display: inline-flex; align-items: center; justify-content: center;">i</span>'
            };
            return fallbackIcons[type] || fallbackIcons.info;
        }
    },
    
    clear() {
        this.notifications.forEach(notification => {
            this.remove(notification.element, false);
        });
        this.notifications = [];
    },
    
    // Debug function to test icon rendering
    // testIcons() {
    //     console.log('Font Awesome status:', this.fontAwesomeReady);
    //     const types = ['success', 'info', 'warning', 'error'];
    //     types.forEach(type => {
    //         this.show(type, `Test ${type} notification with icon`);
    //     });
    // }
};

// Show alert messages - Updated to use new notification system
function showAlert(type, message, duration = 5000) {
    return NotificationManager.show(type, message, duration);
}

// View logs
// viewLogs(): pages that expose logs navigate to /logs (dashboard.html); the former
// modal stub here rendered fabricated log text.

// Shutdown node
function shutdownNode() {
    if (confirm('Are you sure you want to shutdown this node? This will stop all services.')) {
        // This would call a shutdown API endpoint
        showAlert('warning', 'Shutdown initiated. Node will stop in a few seconds.');
        
        setTimeout(() => {
            window.location.href = '/shutdown';
        }, 3000);
    }
}

// File upload helpers
function setupFileUpload(inputId, dropAreaId) {
    const input = document.getElementById(inputId);
    const dropArea = document.getElementById(dropAreaId);
    
    if (!input || !dropArea) return;
    
    // Prevent default drag behaviors
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        dropArea.addEventListener(eventName, preventDefaults, false);
        document.body.addEventListener(eventName, preventDefaults, false);
    });
    
    // Highlight drop area when item is dragged over it
    ['dragenter', 'dragover'].forEach(eventName => {
        dropArea.addEventListener(eventName, highlight, false);
    });
    
    ['dragleave', 'drop'].forEach(eventName => {
        dropArea.addEventListener(eventName, unhighlight, false);
    });
    
    // Handle dropped files
    dropArea.addEventListener('drop', handleDrop, false);
    
    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }
    
    function highlight(e) {
        dropArea.classList.add('dragover');
    }
    
    function unhighlight(e) {
        dropArea.classList.remove('dragover');
    }
    
    function handleDrop(e) {
        const dt = e.dataTransfer;
        const files = dt.files;
        
        if (files.length > 0) {
            input.files = files;
            // Trigger change event
            input.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }
}

// Format bytes
function formatBytes(bytes, decimals = 2) {
    if (bytes === 0) return '0 Bytes';
    
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

// Format duration
function formatDuration(ms) {
    if (ms < 1000) return `${ms}ms`;
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
    return `${(ms / 60000).toFixed(1)}m`;
}

// Copy to clipboard
// function copyToClipboard(text) {
//     navigator.clipboard.writeText(text).then(() => {
//         showAlert('success', 'Copied to clipboard', 2000);
//     }).catch(() => {
//         showAlert('error', 'Failed to copy to clipboard', 2000);
//     });
// }

// Export data as JSON
// function exportAsJSON(data, filename) {
//     const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
//     const url = URL.createObjectURL(blob);
    
//     const a = document.createElement('a');
//     a.href = url;
//     a.download = filename;
//     a.click();
    
//     URL.revokeObjectURL(url);
// }

// Debounce function
function debounce(func, wait, immediate) {
    let timeout;
    return function executedFunction(...args) {
        const later = () => {
            timeout = null;
            if (!immediate) func(...args);
        };
        const callNow = immediate && !timeout;
        clearTimeout(timeout);
        timeout = setTimeout(later, wait);
        if (callNow) func(...args);
    };
}

// Theme handling
function initializeTheme() {
    const theme = localStorage.getItem('infernode-theme') || 'auto';
    applyTheme(theme);
}

function applyTheme(theme) {
    if (theme === 'dark' || (theme === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
        document.documentElement.setAttribute('data-bs-theme', 'dark');
    } else {
        document.documentElement.setAttribute('data-bs-theme', 'light');
    }
    localStorage.setItem('infernode-theme', theme);
}

// Initialize theme on load
document.addEventListener('DOMContentLoaded', initializeTheme);

// Listen for system theme changes
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    const theme = localStorage.getItem('infernode-theme');
    if (theme === 'auto') {
        applyTheme('auto');
    }
});
