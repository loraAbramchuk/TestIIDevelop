const uploadForm = document.getElementById('video-upload');
if (uploadForm) {
  const fileInput = uploadForm.querySelector('#video-file');
  const submit = uploadForm.querySelector('[type=submit]');
  const cancel = uploadForm.querySelector('#upload-cancel');
  const check = uploadForm.querySelector('#upload-check');
  const progress = uploadForm.querySelector('#upload-progress');
  const status = uploadForm.querySelector('#upload-status');
  const error = uploadForm.querySelector('[data-error]');
  const manage = uploadForm.querySelector('#upload-manage');
  const fields = [...uploadForm.querySelectorAll('[name=title], [name=description], [name=visibility]')];
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
  async function refreshStatus() {
    if (!uploadForm.dataset.videoId) return;
    const response = await fetch(`/api/videos/${uploadForm.dataset.videoId}/status`, {
      credentials: 'same-origin', cache: 'no-store',
    });
    if (!response.ok) throw new Error('Не удалось проверить статус. Откройте «Мои видео» и обновите страницу.');
    const video = await response.json();
    accepted = ['processing', 'ready'].includes(video.processing_status);
    const messages = {
      uploading: 'Файл ещё не принят. Если передача была отменена, дождитесь завершения попытки и повторите её.',
      failed: 'Передача или обработка завершилась ошибкой. Можно загрузить файл повторно.',
      processing: 'Файл принят и сохранён. Ожидает обработки; воспроизведение пока недоступно.',
      ready: 'Видео готово.',
    };
    status.textContent = messages[video.processing_status] || 'Обновите страницу, чтобы проверить статус.';
    updateControls();
  }
  check.addEventListener('click', async () => {
    check.disabled = true;
    try { await refreshStatus(); error.textContent = ''; }
    catch (failure) { error.textContent = failure.message; }
    finally { check.disabled = false; }
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
    // Keep metadata stable while the request is active; retries update the same card.
    const metadata = Object.fromEntries(fields.map(field => [field.name, field.value]));
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
      const csrfResponse = await fetch('/api/auth/csrf', {credentials: 'same-origin', cache: 'no-store'});
      const csrf = await csrfResponse.json();
      if (!csrfResponse.ok || !csrf.csrf_token) throw new Error('Обновите страницу и войдите в аккаунт.');
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
        xhr.send(file);
      });
      status.textContent = 'Файл принят и сохранён. Ожидает обработки; воспроизведение пока недоступно.';
      progress.value = 100;
    } catch (failure) {
      error.textContent = failure instanceof TypeError ? 'Ошибка соединения. Проверьте статус и повторите попытку.' : failure.message;
      if (uploadForm.dataset.videoId) {
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
