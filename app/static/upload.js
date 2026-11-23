const uploadForm = document.getElementById('video-upload');
if (uploadForm) {
  const fileInput = uploadForm.querySelector('#video-file');
  const submit = uploadForm.querySelector('[type=submit]');
  const cancel = uploadForm.querySelector('#upload-cancel');
  const check = uploadForm.querySelector('#upload-check');
  const progress = uploadForm.querySelector('#upload-progress');
  const status = uploadForm.querySelector('#upload-status');
  const error = uploadForm.querySelector('[data-error]');
  const processingError = uploadForm.querySelector('#upload-processing-error');
  const manage = uploadForm.querySelector('#upload-manage');
  const watch = uploadForm.querySelector('#upload-watch');
  const fields = [...uploadForm.querySelectorAll('[name=title], [name=description], [name=visibility]')];
  const cardStatus = document.getElementById('video-status');
  const cardNote = document.getElementById('video-processing-note');
  const cardError = document.getElementById('video-processing-error');
  const cardCheck = document.getElementById('video-status-check');
  const uploadPanel = document.getElementById('video-upload-panel');
  const reportError = message => { (cardError || processingError).textContent = message; };
  let watcher = null;
  let xhr = null;
  let busy = false;
  let accepted = false;

  function updateControls() {
    for (const field of [submit, fileInput, ...fields]) field.disabled = busy || accepted;
  }
  function showCard() {
    manage.href = `/studio/videos/${uploadForm.dataset.videoId}`;
    manage.hidden = false;
    check.hidden = false;
  }
  function renderStatus(video) {
    accepted = ['processing', 'ready'].includes(video.processing_status);
    const messages = {
      uploading: 'Файл ещё не принят. Если передача была отменена, дождитесь завершения попытки и повторите её.',
      failed: 'Передача или обработка завершилась ошибкой. Можно загрузить файл повторно.',
      processing: 'Файл принят. Обработка выполняется в фоне, статус обновится автоматически.',
      ready: 'Видео готово.',
    };
    status.textContent = messages[video.processing_status] || 'Обновите страницу, чтобы проверить статус.';
    reportError(video.processing_status === 'failed' ? processingFailureMessage(video.error_code) : '');
    // A status refresh must not erase the separate error of a rejected transfer.
    // Only proof of acceptance supersedes an action/connection error.
    if (accepted) { error.textContent = ''; progress.hidden = false; progress.value = 100; }
    const canWatch = video.processing_status === 'ready' && video.moderation_status === 'visible';
    watch.href = `/watch/${uploadForm.dataset.videoId}`;
    watch.hidden = !canWatch;
    const cardWatch = document.getElementById('video-watch');
    if (cardWatch) cardWatch.hidden = !canWatch;
    if (cardStatus) {
      const labels = {uploading: 'Файл ещё не загружен', processing: 'Видео обрабатывается', ready: 'Видео готово', failed: 'Ошибка обработки'};
      cardStatus.textContent = labels[video.processing_status] || 'Статус неизвестен';
      cardStatus.dataset.processingState = video.processing_status;
      cardNote.hidden = video.processing_status !== 'processing';
      cardCheck.hidden = false;
      if (video.moderation_status) cardStatus.dataset.moderationStatus = video.moderation_status;
      if ('moderation_reason' in video) cardStatus.dataset.moderationReason = video.moderation_reason || '';
      const moderationReason = document.getElementById('video-moderation-reason');
      if (moderationReason) {
        moderationReason.hidden = cardStatus.dataset.moderationStatus !== 'hidden';
        moderationReason.textContent = 'Скрыто модератором: ' + (cardStatus.dataset.moderationReason || 'Причина не указана');
      }
      uploadPanel.hidden = accepted;
    }
    updateControls();
  }
  function getWatcher() {
    if (!watcher && uploadForm.dataset.videoId) {
      watcher = watchVideoStatus(uploadForm.dataset.videoId, renderStatus, reportError);
    }
    return watcher;
  }
  async function refreshStatus() {
    const video = await getWatcher()?.refresh();
    // Reconciliation after a lost response must resume the same polling as a 202.
    if (video?.processing_status === 'processing') getWatcher().start();
    return video;
  }
  for (const button of [check, cardCheck].filter(Boolean)) {
    button.addEventListener('click', async () => {
      button.disabled = true;
      try {
        await refreshStatus();
      } catch (failure) { if (failure.name !== 'AbortError') reportError(failure.message); }
      finally { button.disabled = false; }
    });
  }
  if (cardStatus) {
    renderStatus({processing_status: cardStatus.dataset.processingState, error_code: cardStatus.dataset.errorCode, moderation_status: cardStatus.dataset.moderationStatus, moderation_reason: cardStatus.dataset.moderationReason});
    if (accepted && cardStatus.dataset.processingState === 'processing') getWatcher().start();
  }
  window.addEventListener('pageshow', event => {
    if (event.persisted && accepted) {
      watcher?.dispose(); watcher = null;
      getWatcher().start();
    }
  });
  cancel.addEventListener('click', () => { if (xhr) xhr.abort(); });
  uploadForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || accepted) return;
    const file = fileInput.files[0];
    error.textContent = '';
    if (!file || file.size === 0) { error.textContent = 'Выберите непустой файл.'; return; }
    if (file.size > Number(uploadForm.dataset.maxBytes)) {
      error.textContent = 'Файл превышает допустимый размер.'; return;
    }
    busy = true;
    watcher?.dispose(); watcher = null;
    reportError('');
    // Keep metadata stable while the request is active; retries update the same card.
    const metadata = Object.fromEntries(fields.map(field => [field.name, field.value]));
    let transferStarted = false;
    updateControls();
    try {
      status.textContent = 'Подготовка загрузки…';
      if (uploadForm.dataset.videoId) {
        if (fields.length) await videoRequest(`/api/videos/${uploadForm.dataset.videoId}`, 'PATCH', metadata);
      } else {
        const video = await videoRequest('/api/videos', 'POST', metadata);
        uploadForm.dataset.videoId = video.id;
      }
      showCard();
      const csrf = await apiRead('/api/auth/csrf', data => typeof data?.csrf_token === 'string' && data.csrf_token.length > 0);
      await new Promise((resolve, reject) => {
        xhr = new XMLHttpRequest();
        xhr.open('PUT', `/api/videos/${uploadForm.dataset.videoId}/content`);
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.setRequestHeader('X-CSRF-Token', csrf.csrf_token);
        xhr.upload.onprogress = event => {
          progress.hidden = false;
          progress.value = event.lengthComputable ? Math.floor(event.loaded / event.total * 100) : 0;
          status.textContent = progress.value < 100 ? `Передача файла: ${progress.value}%` : 'Передано 100%. Сервер завершает сохранение…';
        };
        xhr.onload = () => {
          if (xhr.status === 202) { accepted = true; resolve(); return; }
          let message = 'Загрузка не завершена. Проверьте статус и повторите попытку.';
          try { const data = JSON.parse(xhr.responseText); if (typeof data.detail === 'string') message = data.detail; }
          catch { if (xhr.status === 413) message = 'Файл превышает допустимый размер сервера.'; }
          reject(new Error(message));
        };
        xhr.onerror = () => reject(new Error('Соединение прервано. Проверьте статус перед повтором.'));
        xhr.onabort = () => reject(new Error('Передача отменена. Проверьте статус: сервер мог уже принять файл.'));
        progress.hidden = false;
        progress.value = 0;
        cancel.hidden = false;
        transferStarted = true;
        xhr.send(file);
      });
      renderStatus({processing_status: 'processing'});
      getWatcher().start();
      progress.value = 100;
    } catch (failure) {
      error.textContent = failure instanceof TypeError ? 'Ошибка соединения. Проверьте статус и повторите попытку.' : failure.message;
      if (!transferStarted) status.textContent = 'Подготовка загрузки не завершена. Повторите попытку.';
      if (transferStarted && uploadForm.dataset.videoId) {
        try { await refreshStatus(); } catch { /* Keep the original upload error visible. */ }
      }
    } finally {
      xhr = null;
      busy = false;
      updateControls();
      cancel.hidden = true;
    }
  });
  window.addEventListener('beforeunload', event => {
    if (busy) { event.preventDefault(); event.returnValue = ''; }
  });
}
