(() => {
  const form = document.getElementById('video-moderation-form') || document.getElementById('report-decision-form');
  if (!form) return;
  const videoForm = form.id === 'video-moderation-form';
  const error = document.getElementById('moderation-error'), success = document.getElementById('moderation-success');
  let busy = false, forbidden = false, closed = form.hidden;
  function controls() {
    for (const control of form.elements) control.disabled = busy || forbidden || closed;
    if (videoForm) for (const button of form.querySelectorAll('button[value]')) {
      button.disabled = busy || forbidden || form.dataset.deleted === 'true' || button.value === form.dataset.state;
    }
  }
  function renderVideo(video) {
    form.dataset.state = video.moderation_status; form.dataset.deleted = String(video.deleted);
    document.getElementById('moderation-state').textContent = video.moderation_status;
    document.getElementById('moderation-privacy').textContent = video.visibility;
    document.getElementById('moderation-processing').textContent = video.processing_status;
    document.getElementById('moderation-video-title').textContent = video.title || 'Название недоступно по текущим правам просмотра';
    document.getElementById('moderation-current-reason').textContent = video.moderation_reason || '';
    document.getElementById('moderation-deleted').hidden = !video.deleted;
    const watch = document.getElementById('moderation-watch');
    watch.hidden = !video.watch_url;
    if (video.watch_url) watch.href = video.watch_url; else watch.removeAttribute('href');
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy || forbidden || closed) return;
    const payload = Object.fromEntries(new FormData(form));
    if (videoForm) {
      payload.status = event.submitter?.value;
      if (!payload.status) { error.textContent = 'Выберите действие кнопкой «Скрыть» или «Восстановить».'; return; }
    }
    error.textContent = ''; success.textContent = ''; busy = true; controls();
    try {
      const path = videoForm ? `/api/moderation/videos/${form.dataset.videoId}/visibility` : `/api/moderation/reports/${form.dataset.reportId}/decision`;
      const result = await apiRequest(path, 'POST', payload);
      if (videoForm) {
        renderVideo(result); form.elements.reason.value = '';
        success.textContent = result.moderation_status === 'hidden' ? 'Видео скрыто. Действие сохранено в журнале.' : 'Видео восстановлено. Действие сохранено в журнале.';
      } else {
        closed = true; form.hidden = true;
        document.getElementById('report-state').textContent = result.status;
        document.getElementById('report-decision').textContent = result.decision_reason;
        document.getElementById('report-decider').textContent = 'Рассмотрел: ' + result.decided_by;
        success.textContent = 'Решение сохранено в журнале.';
      }
    } catch (failure) {
      error.textContent = failure instanceof TypeError ? 'Не удалось подтвердить действие. Обновите страницу и проверьте состояние перед повтором.' : failure.message;
      if ([401, 403].includes(failure.status)) forbidden = true;
    } finally { busy = false; controls(); }
  });
  controls();
})();
