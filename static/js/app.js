/**
 * Power BI Documentation Generator - Frontend Application
 * Handles UI interactions, API calls, and document generation
 */

// Global state
let allReports = [];
let allWorkspaces = [];
let currentWorkspaceId = null;
let currentWorkspaceName = '';

// DOM Elements
const workspaceSearch = document.getElementById('workspace-search');
const workspaceList = document.getElementById('workspace-list');
const reportSearch = document.getElementById('report-search');
const workspaceTitle = document.getElementById('workspace-title');
const loadingState = document.getElementById('loading-state');
const reportsContainer = document.getElementById('reports-container');
const reportsList = document.getElementById('reports-list');
const noReports = document.getElementById('no-reports');
const toast = document.getElementById('toast');

// ============================================================================
// INITIALIZATION
// ============================================================================

document.addEventListener('DOMContentLoaded', () => {
    loadWorkspaces();
    setupEventListeners();
});

function setupEventListeners() {
    // Workspace search input
    workspaceSearch.addEventListener('input', handleWorkspaceSearch);

    // Report search input
    reportSearch.addEventListener('input', handleReportSearch);
}

// ============================================================================
// API CALLS
// ============================================================================

/**
 * Load all workspaces from API
 */
async function loadWorkspaces() {
    try {
        const response = await fetch('/api/workspaces');
        const data = await response.json();

        if (data.success) {
            allWorkspaces = data.workspaces;
            populateWorkspaceList(allWorkspaces);
        } else {
            showToast('Failed to load workspaces: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('Error loading workspaces: ' + error.message, 'error');
    }
}

/**
 * Populate workspace list
 */
function populateWorkspaceList(workspaces) {
    if (workspaces.length === 0) {
        workspaceList.innerHTML = '<div class="pbi-loading">No workspaces found</div>';
        return;
    }

    workspaceList.innerHTML = '';

    workspaces.forEach(workspace => {
        const item = document.createElement('div');
        item.className = 'pbi-workspace-item';
        item.dataset.workspaceId = workspace.id;
        item.dataset.workspaceName = workspace.name;

        item.innerHTML = `
            <i class="fas fa-folder"></i>
            <span>${escapeHtml(workspace.name)}</span>
        `;

        item.addEventListener('click', () => selectWorkspace(workspace.id, workspace.name));

        workspaceList.appendChild(item);
    });
}

/**
 * Load reports for selected workspace
 */
async function loadReports(workspaceId) {
    try {
        showLoading();
        
        const response = await fetch(`/api/reports?workspace_id=${workspaceId}`);
        const data = await response.json();
        
        if (data.success) {
            allReports = data.reports;
            displayReports(allReports);
        } else {
            showToast('Failed to load reports: ' + data.error, 'error');
            showNoReports();
        }
    } catch (error) {
        showToast('Error loading reports: ' + error.message, 'error');
        showNoReports();
    }
}

/**
 * Generate documentation for a report
 */
async function generateDocumentation(workspaceId, reportId, datasetId, reportName, buttonElement) {
    try {
        // Update button state
        buttonElement.disabled = true;
        buttonElement.classList.add('generating');
        buttonElement.innerHTML = '<i class="fas fa-spinner"></i> Generating...';

        const response = await fetch('/api/generate', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify({
                workspace_id: workspaceId,
                report_id: reportId,
                dataset_id: datasetId,
                report_name: reportName
            })
        });
        
        if (response.ok) {
            // Download the file
            const blob = await response.blob();
            const url = window.URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `${reportName.replace(/[^a-z0-9]/gi, '_')}_Documentation.docx`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(url);
            document.body.removeChild(a);
            
            showToast('Documentation generated successfully!', 'success');
        } else {
            const data = await response.json();
            showToast('Failed to generate documentation: ' + data.error, 'error');
        }
    } catch (error) {
        showToast('Error generating documentation: ' + error.message, 'error');
    } finally {
        // Reset button state
        buttonElement.disabled = false;
        buttonElement.classList.remove('generating');
        buttonElement.innerHTML = '<i class="fas fa-file-download"></i> Generate Document';
    }
}

// ============================================================================
// EVENT HANDLERS
// ============================================================================

/**
 * Select a workspace
 */
function selectWorkspace(workspaceId, workspaceName) {
    currentWorkspaceId = workspaceId;
    currentWorkspaceName = workspaceName;

    // Update workspace title
    workspaceTitle.textContent = workspaceName;

    // Highlight selected workspace
    document.querySelectorAll('.pbi-workspace-item').forEach(item => {
        item.classList.remove('active');
    });

    const selectedItem = document.querySelector(`[data-workspace-id="${workspaceId}"]`);
    if (selectedItem) {
        selectedItem.classList.add('active');
    }

    // Load reports
    loadReports(workspaceId);
    reportSearch.value = '';
}

/**
 * Handle workspace search
 */
function handleWorkspaceSearch(event) {
    const searchTerm = event.target.value.toLowerCase();

    // Filter workspaces
    const filteredWorkspaces = allWorkspaces.filter(workspace =>
        workspace.name.toLowerCase().includes(searchTerm)
    );

    // Update workspace list
    populateWorkspaceList(filteredWorkspaces);
}

/**
 * Handle report search - FIX FOR FILTERING ISSUE
 */
function handleReportSearch(event) {
    const searchTerm = event.target.value.toLowerCase();

    if (searchTerm === '') {
        displayReports(allReports);
    } else {
        const filteredReports = allReports.filter(report =>
            report.name.toLowerCase().includes(searchTerm)
        );
        displayReports(filteredReports);
    }
}

// ============================================================================
// UI DISPLAY FUNCTIONS
// ============================================================================

/**
 * Display reports in Power BI style list
 */
function displayReports(reports) {
    if (reports.length === 0) {
        showNoReports();
        return;
    }

    loadingState.style.display = 'none';
    noReports.style.display = 'none';
    reportsContainer.style.display = 'block';

    reportsList.innerHTML = '';

    reports.forEach(report => {
        const row = createReportRow(report);
        reportsList.appendChild(row);
    });
}

/**
 * Create a Power BI style report row element
 */
function createReportRow(report) {
    const row = document.createElement('div');
    row.className = 'pbi-report-item';

    // Format last refresh time
    const lastRefresh = formatRefreshTime(report.lastRefreshTime || report.lastRefresh);

    // Format refresh status
    const status = report.refreshStatus || 'Unknown';
    const statusClass = getStatusClass(status);

    // Get owner
    const owner = report.owner || 'Unknown';

    row.innerHTML = `
        <div class="pbi-report-name">
            <div class="pbi-report-icon">
                <i class="fas fa-chart-bar"></i>
            </div>
            <span class="pbi-report-title" title="${escapeHtml(report.name)}">
                ${escapeHtml(report.name)}
            </span>
        </div>
        <div class="pbi-report-refresh">
            <i class="fas fa-clock"></i>
            ${lastRefresh}
        </div>
        <div class="pbi-report-status">
            <span class="pbi-status-badge ${statusClass}">
                ${status}
            </span>
        </div>
        <div class="pbi-report-owner" title="${escapeHtml(owner)}">
            ${escapeHtml(owner)}
        </div>
        <div class="pbi-report-action">
            <button
                class="pbi-btn"
                onclick="generateDocumentation('${currentWorkspaceId}', '${report.id}', '${report.datasetId || ''}', '${escapeHtml(report.name)}', this)"
            >
                <i class="fas fa-download"></i>
                Generate
            </button>
        </div>
    `;

    return row;
}

/**
 * Get CSS class for status badge
 */
function getStatusClass(status) {
    const statusLower = status.toLowerCase();
    if (statusLower === 'completed') return 'status-completed';
    if (statusLower === 'failed') return 'status-failed';
    if (statusLower.includes('progress')) return 'status-inprogress';
    if (statusLower === 'n/a') return 'status-na';
    return 'status-unknown';
}

/**
 * Format refresh time for display
 */
function formatRefreshTime(refreshTime) {
    if (!refreshTime || refreshTime === 'Unknown' || refreshTime === 'Never') {
        return refreshTime || 'Unknown';
    }

    try {
        const date = new Date(refreshTime);
        const now = new Date();
        const diffMs = now - date;
        const diffMins = Math.floor(diffMs / 60000);
        const diffHours = Math.floor(diffMs / 3600000);
        const diffDays = Math.floor(diffMs / 86400000);

        if (diffMins < 1) return 'Just now';
        if (diffMins < 60) return `${diffMins} min${diffMins > 1 ? 's' : ''} ago`;
        if (diffHours < 24) return `${diffHours} hour${diffHours > 1 ? 's' : ''} ago`;
        if (diffDays < 7) return `${diffDays} day${diffDays > 1 ? 's' : ''} ago`;

        return date.toLocaleDateString() + ' ' + date.toLocaleTimeString();
    } catch (e) {
        return refreshTime;
    }
}

/**
 * Show loading state
 */
function showLoading() {
    loadingState.style.display = 'flex';
    reportsContainer.style.display = 'none';
    noReports.style.display = 'none';
}

/**
 * Show no reports state
 */
function showNoReports() {
    loadingState.style.display = 'none';
    reportsContainer.style.display = 'none';
    noReports.style.display = 'flex';
}

/**
 * Show toast notification
 */
function showToast(message, type = 'success') {
    const toastMessage = document.getElementById('toast-message');
    toastMessage.textContent = message;

    toast.className = 'pbi-toast';
    if (type === 'error') {
        toast.classList.add('error');
    }

    toast.classList.add('show');

    setTimeout(() => {
        toast.classList.remove('show');
    }, 4000);
}

/**
 * Escape HTML to prevent XSS
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

