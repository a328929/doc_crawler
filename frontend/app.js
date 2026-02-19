let eventSource = null;

function addLog(message) {
  const logWindow = document.getElementById('log-window');
  const line = document.createElement('p');
  line.textContent = `> ${message}`;
  logWindow.appendChild(line);
  logWindow.scrollTop = logWindow.scrollHeight;
}

function toggleSection(id, show) {
  document.getElementById(id).classList.toggle('hidden', !show);
}

async function startCrawl() {
  const url = document.getElementById('url').value.trim();
  const selector = document.getElementById('selector').value.trim();
  const respectRobots = document.getElementById('respectRobots').checked;
  const restrictPrefix = document.getElementById('restrictPrefix').checked;

  if (!url) {
    alert('请输入有效 URL');
    return;
  }

  toggleSection('input-section', false);
  toggleSection('progress-section', true);
  toggleSection('result-section', false);
  toggleSection('error-section', false);

  addLog(`目标: ${url}`);

  try {
    const resp = await fetch('/api/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url,
        selector,
        respect_robots: respectRobots,
        restrict_prefix: restrictPrefix,
      }),
    });

    const data = await resp.json();
    if (!resp.ok || data.status !== 'started') {
      throw new Error(data.error || '任务启动失败');
    }

    if (eventSource) eventSource.close();
    eventSource = new EventSource(`/api/stream/${data.task_id}`);

    eventSource.onmessage = (event) => {
      const progress = JSON.parse(event.data);
      document.getElementById('progress-bar').style.width = `${progress.percent}%`;
      document.getElementById('percent-text').textContent = `${progress.percent}%`;
      document.getElementById('status-text').textContent = progress.message;
      addLog(progress.message);

      if (progress.status === 'completed') {
        eventSource.close();
        document.getElementById('download-link').href = `/api/download/${data.task_id}`;
        toggleSection('progress-section', false);
        toggleSection('result-section', true);
      }

      if (progress.status === 'error') {
        eventSource.close();
        showError(progress.message);
      }
    };

    eventSource.onerror = () => {
      if (eventSource) eventSource.close();
      showError('SSE 连接中断，请重试。');
    };
  } catch (error) {
    showError(error.message);
  }
}

function showError(message) {
  toggleSection('progress-section', false);
  toggleSection('error-section', true);
  document.getElementById('error-msg').textContent = message;
}

function resetUI() {
  if (eventSource) eventSource.close();

  document.getElementById('url').value = '';
  document.getElementById('selector').value = '';
  document.getElementById('log-window').innerHTML = '<p class="muted">> 等待任务启动...</p>';
  document.getElementById('progress-bar').style.width = '0%';
  document.getElementById('percent-text').textContent = '0%';
  document.getElementById('status-text').textContent = '初始化中...';

  toggleSection('input-section', true);
  toggleSection('progress-section', false);
  toggleSection('result-section', false);
  toggleSection('error-section', false);
}
