/* Dashboard charts and KPI updates (Chart.js) */

const HPE_GREEN = '#01A982';
const HPE_TEAL = '#00E8CF';
const HPE_DARK = '#263040';
const isDark = () => document.documentElement.getAttribute('data-theme') === 'dark';
const gridColor = () => isDark() ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)';
const textColor = () => isDark() ? '#9ba4b4' : '#5a6577';

Chart.defaults.color = textColor();
Chart.defaults.borderColor = gridColor();

const CLASS_COLORS = [
  '#01A982', '#00E8CF', '#ffc107', '#e53e3e', '#7c3aed',
  '#3b82f6', '#f97316', '#6b7280',
];

let volumeChart, classChart, latencyChart, repoChart;

async function loadVolumeChart() {
  const res = await fetch('/dashboard/api/daily-volume?days=30');
  const json = await res.json();
  const labels = json.data.map(d => d.date.slice(5));
  const totals = json.data.map(d => d.total);
  const approved = json.data.map(d => d.approved);
  const rejected = json.data.map(d => d.rejected);

  const ctx = document.getElementById('volumeChart').getContext('2d');
  if (volumeChart) volumeChart.destroy();
  volumeChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Approved', data: approved, backgroundColor: HPE_GREEN, borderRadius: 4, barPercentage: 0.7 },
        { label: 'Rejected', data: rejected, backgroundColor: '#e53e3e', borderRadius: 4, barPercentage: 0.7 },
        { label: 'Total', data: totals, type: 'line', borderColor: HPE_TEAL, borderWidth: 2, tension: 0.4, pointRadius: 2, fill: false },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: 'top', labels: { usePointStyle: true, padding: 12 } } },
      scales: {
        x: { grid: { display: false } },
        y: { beginAtZero: true, grid: { color: gridColor() } },
      },
    },
  });
}

async function loadClassChart() {
  const res = await fetch('/dashboard/api/classifications');
  const json = await res.json();
  const entries = Object.entries(json.by_classification || {});
  const labels = entries.map(([k]) => k.replace(/_/g, ' '));
  const values = entries.map(([, v]) => v);

  const ctx = document.getElementById('classChart').getContext('2d');
  if (classChart) classChart.destroy();
  classChart = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: CLASS_COLORS.slice(0, labels.length),
        borderWidth: 0,
        hoverOffset: 8,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      cutout: '60%',
      plugins: {
        legend: { position: 'bottom', labels: { usePointStyle: true, padding: 8, font: { size: 11 } } },
      },
    },
  });
}

async function loadLatencyChart() {
  const res = await fetch('/dashboard/api/response-time?days=30');
  const json = await res.json();
  const labels = json.data.map(d => d.date.slice(5));
  const values = json.data.map(d => d.avg_ms);

  const ctx = document.getElementById('latencyChart').getContext('2d');
  if (latencyChart) latencyChart.destroy();
  latencyChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Avg Latency (ms)',
        data: values,
        borderColor: HPE_GREEN,
        backgroundColor: 'rgba(1,169,130,0.1)',
        fill: true,
        tension: 0.4,
        pointRadius: 3,
        pointBackgroundColor: HPE_GREEN,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false } },
        y: { beginAtZero: true, grid: { color: gridColor() } },
      },
    },
  });
}

async function loadRepoChart() {
  const res = await fetch('/dashboard/api/repos');
  const json = await res.json();
  const counts = json.search_counts || {};
  const configured = json.configured_repos || [];

  const labels = Object.keys(counts).map(r => r.split('/').pop()).slice(0, 10);
  const values = Object.values(counts).slice(0, 10);

  const ctx = document.getElementById('repoChart').getContext('2d');

  if (labels.length === 0) {
    document.getElementById('repoList').innerHTML =
      '<span class="text-muted">No multi-repo data yet. Configure <code>GIT_REPOS</code> env var.</span>';
    const fallbackLabels = configured.map(r => r.split('/').pop()).slice(0, 10);
    if (repoChart) {
      repoChart.data.labels = fallbackLabels;
      repoChart.data.datasets[0].data = fallbackLabels.map(() => 0);
      repoChart.update();
    } else {
      repoChart = new Chart(ctx, {
        type: 'bar',
        data: { labels: fallbackLabels, datasets: [{ label: 'Searches', data: fallbackLabels.map(() => 0), backgroundColor: HPE_GREEN }] },
        options: { responsive: true, maintainAspectRatio: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true } } },
      });
    }
    return;
  }

  if (repoChart) {
    repoChart.data.labels = labels;
    repoChart.data.datasets[0].data = values;
    repoChart.data.datasets[0].backgroundColor = CLASS_COLORS.slice(0, labels.length);
    repoChart.update();
  } else {
    repoChart = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          label: 'PR Searches',
          data: values,
          backgroundColor: CLASS_COLORS.slice(0, labels.length),
          borderRadius: 6,
        }],
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: true,
        plugins: { legend: { display: false } },
        scales: {
          x: { beginAtZero: true, grid: { color: gridColor() } },
          y: { grid: { display: false } },
        },
      },
    });
  }

  const repoHtml = configured.map(r =>
    `<span class="badge bg-light text-dark border me-1">${r}</span>`
  ).join('');
  document.getElementById('repoList').innerHTML =
    `<strong class="small">Configured repos:</strong> ${repoHtml || '<span class="text-muted">none</span>'}`;
}

async function loadSeverityTable() {
  const res = await fetch('/dashboard/api/severity');
  const json = await res.json();
  const items = json.items || [];

  const overrideCount = items.filter(i => i.disagrees).length;
  document.getElementById('overrideBadge').textContent = `${overrideCount} override${overrideCount !== 1 ? 's' : ''}`;
  document.getElementById('kpiRovoOverrides').textContent = overrideCount;

  const tbody = document.getElementById('severityBody');
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-3">No Rovo severity changes detected yet</td></tr>';
    return;
  }

  tbody.innerHTML = items.map(item => `
    <tr class="${item.disagrees ? 'table-danger' : ''}">
      <td><a href="/ui/drafts/${item.draft_id}" class="fw-bold">${item.issue_key}</a></td>
      <td class="text-muted small">${(item.created_at || '').slice(0, 16)}</td>
      <td>${item.rovo_from} → ${item.rovo_to}</td>
      <td><span class="badge ${item.disagrees ? 'bg-danger' : 'bg-success'}">${item.recommended}</span></td>
      <td>${item.disagrees
        ? '<i class="bi bi-exclamation-triangle text-danger"></i> Yes'
        : '<i class="bi bi-check-circle text-success"></i> No'}</td>
      <td>${(item.confidence * 100).toFixed(0)}%</td>
      <td class="small">${item.evidence_summary}</td>
    </tr>
  `).join('');
}

async function loadTopIssues() {
  const res = await fetch('/dashboard/api/top-issues');
  const json = await res.json();
  const items = json.items || [];

  const tbody = document.getElementById('topIssuesBody');
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted py-3">No data</td></tr>';
    return;
  }
  tbody.innerHTML = items.map(item => `
    <tr>
      <td><a href="/ui?issue_key=${item.issue_key}" class="fw-bold">${item.issue_key}</a></td>
      <td>${item.count}</td>
      <td>${item.approved}</td>
      <td>${(item.avg_confidence * 100).toFixed(0)}%</td>
    </tr>
  `).join('');
}

async function loadSummary() {
  const res = await fetch('/dashboard/api/summary');
  const json = await res.json();

  document.getElementById('kpiTotal').textContent = json.total_drafts;
  document.getElementById('kpiAcceptance').textContent = json.acceptance_rate_pct.toFixed(1) + '%';
  document.getElementById('kpiTimeSaved').textContent = json.estimated_time_saved_hours + 'h';
  document.getElementById('kpiPending').textContent = json.pending;
  document.getElementById('kpiAvgConf').textContent = ((json.avg_confidence || 0) * 100).toFixed(0) + '%';
  document.getElementById('kpiAvgRating').textContent = (json.avg_rating || 0).toFixed(1) + '/5';
  document.getElementById('kpiHallucinations').textContent = json.hallucination_flagged;

  const hallRate = json.total_drafts ? ((json.hallucination_flagged / json.total_drafts) * 100).toFixed(1) : '0';
  document.getElementById('qmHallRate').textContent = hallRate + '%';
  document.getElementById('qmHallBar').style.width = hallRate + '%';
  document.getElementById('qmEdited').textContent = json.drafts_edited_before_approval;
  document.getElementById('qmEditedPct').textContent = json.pct_approved_drafts_edited.toFixed(1);
  document.getElementById('qmEditedBar').style.width = json.pct_approved_drafts_edited + '%';
  document.getElementById('qmRedactions').textContent = json.total_redactions;
  document.getElementById('qmLatency').textContent = (json.avg_pipeline_duration_ms || 0).toFixed(0) + ' ms';
}

async function refreshDashboard() {
  document.getElementById('refreshBtn').disabled = true;
  await Promise.all([
    loadSummary(),
    loadVolumeChart(),
    loadClassChart(),
    loadLatencyChart(),
    loadRepoChart(),
    loadSeverityTable(),
    loadTopIssues(),
  ]);
  document.getElementById('refreshBtn').disabled = false;
}

document.addEventListener('DOMContentLoaded', refreshDashboard);
setInterval(refreshDashboard, 60000);
