
const form = document.querySelector('#server-form');
const onboarding = document.querySelector('#onboarding');
const escapeHtml = value => { const div = document.createElement('div'); div.textContent = value ?? ''; return div.innerHTML; };
const repositoryForm = document.querySelector('#repository-form');
const repositoryCard = document.querySelector('#repository-card');
const repositoryError = document.querySelector('#repository-error');
let repositoryMetadata = null;

function repositoryUrlLooksValid(value) {
  return /^https:\/\/github\.com\/[A-Za-z0-9][A-Za-z0-9_.-]{0,38}\/[A-Za-z0-9_.-]{1,100}(?:\.git)?\/?$/.test(value.trim()) ||
    /^git@github\.com:[A-Za-z0-9][A-Za-z0-9_.-]{0,38}\/[A-Za-z0-9_.-]{1,100}(?:\.git)?$/.test(value.trim()) ||
    /^ssh:\/\/git@github\.com\/[A-Za-z0-9][A-Za-z0-9_.-]{0,38}\/[A-Za-z0-9_.-]{1,100}(?:\.git)?\/?$/.test(value.trim());
}

repositoryForm.addEventListener('submit', async event => {
  event.preventDefault();
  const button = repositoryForm.querySelector('button'); const value = repositoryForm.repository_url.value.trim();
  repositoryError.classList.add('d-none'); repositoryCard.classList.add('d-none'); repositoryMetadata = null; button.disabled = true; button.textContent = 'Checking…';
  try {
    if (!repositoryUrlLooksValid(value)) throw new Error('Enter a valid GitHub HTTPS or SSH repository URL.');
    const response = await fetch('/api/repositories/metadata', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({repository_url: value})});
    const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error(data.detail || 'Could not fetch repository metadata.');
    repositoryMetadata = data;
    document.querySelector('#repository-name').textContent = `${data.owner}/${data.repo}`;
    document.querySelector('#repository-visibility').textContent = data.visibility;
    document.querySelector('#repository-details').textContent = `${data.language} · ${data.stars.toLocaleString()} stars · Last commit ${data.last_commit ? new Date(data.last_commit).toLocaleDateString() : 'unknown'}`;
    document.querySelector('#repository-flags').textContent = `${data.archived ? 'Archived' : 'Active'} · ${data.empty_repo ? 'Empty repository' : 'Has source files'}`;
    document.querySelector('#deploy-button').disabled = false; repositoryCard.classList.remove('d-none');
  } catch (error) { repositoryError.textContent = error.message; repositoryError.classList.remove('d-none'); }
  finally { button.disabled = false; button.textContent = 'Validate repository'; }
});

document.querySelector('#deploy-button').addEventListener('click', async () => {
  const button = document.querySelector('#deploy-button'); const result = document.querySelector('#job-result');
  button.disabled = true; button.textContent = 'Queueing…'; result.classList.add('d-none');
  try {
    const response = await fetch('/api/deployment-jobs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({repository_url: repositoryMetadata.url, repository_metadata: repositoryMetadata})});
    const data = await response.json().catch(() => ({})); if (!response.ok) throw new Error(data.detail || 'Could not queue deployment.');
    result.innerHTML = `Job ID: <code>${escapeHtml(data.jobId)}</code> <span class="badge text-bg-warning">${escapeHtml(data.status)}</span>`; result.classList.remove('d-none');
  } catch (error) { result.textContent = error.message; result.className = 'small mt-3 text-danger'; }
  finally { button.disabled = false; button.textContent = 'Deploy'; }
});

async function ensurePublicUrl() {
  const response = await fetch('/api/tunnel', {method: 'POST'});
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.detail || 'Could not start the Pinggy public URL');
  }
  return response.json();
}

async function loadServers() {
  // Update public tunnel link in bootstrap header
  try {
    const statusRes = await fetch('/api/tunnel');
    if (statusRes.ok) {
      const statusData = await statusRes.json();
      const statusLink = document.querySelector('#tunnel-status');
      const offlineSpan = document.querySelector('#tunnel-offline');
      if (statusData.public_url) {
        statusLink.href = statusData.public_url;
        statusLink.textContent = statusData.public_url;
        statusLink.classList.remove('d-none');
        if (offlineSpan) offlineSpan.classList.add('d-none');
      } else {
        statusLink.classList.add('d-none');
        if (offlineSpan) {
          offlineSpan.classList.remove('d-none');
          offlineSpan.textContent = 'offline';
        }
      }
    }
  } catch (err) {
    console.error('Error fetching tunnel status:', err);
  }

  const response = await fetch('/api/servers'); const servers = await response.json();
  document.querySelector('#server-list').innerHTML = servers.length ? servers.map(s => `<div class="col-md-6"><article class="card server-card"><div class="card-body"><strong>${escapeHtml(s.label)}</strong><span class="float-end fw-semibold status-${s.status === 'READY' ? 'ready' : 'waiting'}">${s.status}</span><div class="text-secondary small mt-2">${escapeHtml(s.host)}:${s.ssh_port}</div>${s.facts ? `<div class="small mt-2">${escapeHtml(s.facts.os)} · ${s.facts.ram_mb} MB RAM · ${s.facts.disk_gb} GB disk</div>` : '<div class="small mt-2">Waiting for bootstrap handshake</div>'}</div></article></div>`).join('') : '<p class="text-secondary">No servers registered yet.</p>';
}

form.addEventListener('submit', async event => {
  event.preventDefault(); const button = form.querySelector('button'); button.disabled = true;
  try { await ensurePublicUrl(); const data = Object.fromEntries(new FormData(form)); data.ssh_port = Number(data.ssh_port);
    const response = await fetch('/api/servers', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data)});
    if (!response.ok) throw new Error('Could not register server'); const server = await response.json();
    document.querySelector('#public-key').textContent = server.public_key; document.querySelector('#bootstrap-command').textContent = server.bootstrap_command;
    const publicUrl = document.querySelector('#public-url'); publicUrl.href = server.public_url; publicUrl.textContent = server.public_url;
    onboarding.classList.remove('d-none'); document.querySelector('#empty-state').classList.add('d-none'); form.reset(); await loadServers();
  } catch (error) { alert(error.message); } finally { button.disabled = false; }
});
document.querySelectorAll('.copy').forEach(button => button.addEventListener('click', () => navigator.clipboard.writeText(document.querySelector(`#${button.dataset.copy}`).textContent)));
loadServers(); setInterval(loadServers, 10000);
